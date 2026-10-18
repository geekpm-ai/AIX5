from __future__ import annotations

import asyncio
import fractions
import json
import logging
import os
import struct
import time
from collections import deque
from pathlib import Path

import numpy as np
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
    VideoStreamTrack,
)
from av import VideoFrame

from .audio import CameraAudioTrack
from .codec_tuning import install_h264_tuning
from .hw_capture import manager
from .media_scheduling import (
    ProcessReadWatchdog,
    install_rtp_fairness,
    terminate_process,
)

log = logging.getLogger("aix5.webrtc")
peer_connections: set[RTCPeerConnection] = set()
peer_camera: dict[RTCPeerConnection, str] = {}
peer_created: dict[RTCPeerConnection, float] = {}
MAX_PEERS_PER_CAMERA = 2
peer_lock = asyncio.Lock()
peer_timers = {}
peer_closing = {}
# The RDK multimedia service is not always ready when the application process
# starts after a cold boot.  ``rdk_video_stream`` exits with EOF when its VPU
# context cannot be opened, so treat that as a transient startup condition for
# a short window instead of immediately poisoning the camera's encoder state.
# Keep these values module-level so board bring-up can tune them without a
# source change and tests can make retries instantaneous.
NATIVE_ENCODER_RETRY_WINDOW = float(os.getenv("AIX5_NATIVE_ENCODER_RETRY_WINDOW", "30"))
NATIVE_ENCODER_RETRY_INITIAL = float(
    os.getenv("AIX5_NATIVE_ENCODER_RETRY_INITIAL", "0.5")
)
NATIVE_ENCODER_RETRY_MAX = float(os.getenv("AIX5_NATIVE_ENCODER_RETRY_MAX", "5"))
install_h264_tuning()
install_rtp_fairness()
import aiortc.rtcdtlstransport as _dtls

_dtls.SRTP_PROFILES.sort(
    key=lambda profile: 0 if profile.openssl_profile == b"SRTP_AEAD_AES_128_GCM" else 1
)


class CameraTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, camera_id, mode="grid"):
        super().__init__()
        self.camera_id = camera_id
        self.last_image = None
        self._send_start = None
        self._send_pts = 0
        self.mode = mode
        self.data_channel = None
        self.last_meta = ""
        self._frame_demand_camera = None

    def _release_frame_demand(self):
        camera, self._frame_demand_camera = self._frame_demand_camera, None
        if camera is not None:
            count = max(0, camera.get("_full_rate_frame_consumers", 1) - 1)
            if count:
                camera["_full_rate_frame_consumers"] = count
            else:
                camera.pop("_full_rate_frame_consumers", None)

    def stop(self):
        self._release_frame_demand()
        super().stop()

    async def recv(self):
        camera = manager.cameras.get(self.camera_id, {})
        if self._frame_demand_camera is not camera:
            self._release_frame_demand()
            self._frame_demand_camera = camera
            camera["_full_rate_frame_consumers"] = (
                camera.get("_full_rate_frame_consumers", 0) + 1
            )
        native = manager.native_images.get(self.camera_id)
        sequence = manager.video_sequences.get(self.camera_id, 0)
        # Await a new captured frame rather than encoding the 5 FPS AI buffer
        # repeatedly. One event per camera wakes all active viewers.
        if getattr(self, "_last_sequence", None) == sequence and sequence:
            changed = manager.video_changed.get(self.camera_id)
            if changed:
                try:
                    await asyncio.wait_for(changed.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
            native = manager.native_images.get(self.camera_id)
            sequence = manager.video_sequences.get(self.camera_id, 0)
        self._last_sequence = sequence
        if self._send_start is None:
            self._send_start = time.monotonic()
        now = time.monotonic()
        if not sequence:
            # Compatibility for synthetic tests and startup before the first frame.
            fps = float(camera.get("source_fps") or camera.get("preview_fps") or 20)
            next_at = getattr(self, "_next_send", now)
            if next_at > now:
                await asyncio.sleep(next_at - now)
            now = time.monotonic()
            self._next_send = now + 1 / fps
        self._send_pts = max(self._send_pts + 1, int((now - self._send_start) * 90000))
        pts, time_base = self._send_pts, fractions.Fraction(1, 90000)
        source = manager.images.get(self.camera_id)
        if native is not None:
            frame = await asyncio.to_thread(
                VideoFrame.from_ndarray, native, format="nv12"
            )
            source_width, source_height = frame.width, frame.height
        else:
            image = (
                source
                if source is not None
                else np.zeros((360, 640, 3), dtype=np.uint8)
            )
            source_height, source_width = image.shape[:2]
            frame = VideoFrame.from_ndarray(image, format="bgr24")
        # Read the global setting on each frame so existing viewers update
        # without renegotiation. Fullscreen keeps the source resolution.
        width, height = map(int, manager.preview_resolution.split("x"))
        if self.mode == "grid":
            scale = min(width / source_width, height / source_height, 1.0)
            width = max(2, int(source_width * scale) // 2 * 2)
            height = max(2, int(source_height * scale) // 2 * 2)
            if (width, height) != (source_width, source_height):
                frame = await asyncio.to_thread(
                    frame.reformat, width=width, height=height, format="nv12"
                )
        frame.opaque = camera
        frame.pts = pts
        frame.time_base = time_base
        if self.data_channel and self.data_channel.readyState == "open":
            cam = manager.cameras.get(self.camera_id, {})
            meta = cam.get(
                "detection_payload",
                {
                    "camera_id": self.camera_id,
                    "detections": [],
                    "width": cam.get("width"),
                    "height": cam.get("height"),
                },
            )
            encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
            if encoded != self.last_meta:
                self.data_channel.send(encoded)
                self.last_meta = encoded
        return frame


# Encode each camera/mode once. Every viewer receives the same H.264 access
# units; aiortc only packetizes them and never repeats expensive VPU work.
video_pipelines = {}


class VideoPipeline:
    def __init__(self, camera_id, mode):
        self.camera_id = camera_id
        self.mode = mode
        self.clients = set()
        self.task = None
        self.force_keyframe = True
        self.epoch = time.monotonic()

    def subscribe(self, client):
        self.clients.add(client)
        self.force_keyframe = True
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    def unsubscribe(self, client):
        self.clients.discard(client)
        if not self.clients and self.task and not self.task.done():
            self.task.cancel()

    async def run(self):
        from .codec_tuning import FastH264Encoder

        source = CameraTrack(self.camera_id, self.mode)
        encoder = FastH264Encoder()
        try:
            binary = Path(__file__).resolve().parent.parent / "native/rdk_video_stream"
            if (
                manager.cameras.get(self.camera_id, {}).get("_native_fd") is not None
                and binary.is_file()
            ):
                try:
                    await self.run_native(binary)
                    return
                except Exception as exc:
                    if not manager.allow_software_fallback:
                        raise RuntimeError(
                            "硬件编码不可用，软件回退未开启：" + str(exc)
                        ) from exc
                    log.exception(
                        "native stream failed; allowed fallback camera=%s",
                        self.camera_id,
                    )
            while self.clients:
                frame = await source.recv()
                started_encode = time.monotonic()
                force_keyframe, self.force_keyframe = self.force_keyframe, False

                def encode():
                    work_started = time.monotonic()
                    encoded = b"".join(
                        b"\x00\x00\x00\x01" + nal
                        for nal in encoder._encode_frame(frame, force_keyframe)
                    )
                    camera = manager.cameras.get(self.camera_id)
                    if camera is not None:
                        camera["video_encode_work_ms"] = round(
                            (time.monotonic() - work_started) * 1000, 1
                        )
                    return encoded

                pending = asyncio.create_task(asyncio.to_thread(encode))
                try:
                    payload = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    await asyncio.gather(pending, return_exceptions=True)
                    raise
                if not payload:
                    continue
                camera = manager.cameras.get(self.camera_id)
                if camera is not None:
                    camera["video_encoded_frames"] = (
                        camera.get("video_encoded_frames", 0) + 1
                    )
                    camera["video_encode_ms"] = round(
                        (time.monotonic() - started_encode) * 1000, 1
                    )
                packet_data = (payload, frame.pts, frame.time_base)
                for client in list(self.clients):
                    if client.queue.full():
                        # Resume this client at a new IDR, not a broken P-frame.
                        while not client.queue.empty():
                            client.queue.get_nowait()
                        self.force_keyframe = True
                        continue
                    client.queue.put_nowait(packet_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            camera = manager.cameras.get(self.camera_id)
            if camera is not None:
                camera["encode_backend"] = "error"
                camera["encode_error"] = str(exc)
            log.exception("video pipeline camera=%s mode=%s", self.camera_id, self.mode)
            for client in list(self.clients):
                if client.queue.full():
                    client.queue.get_nowait()
                client.queue.put_nowait(exc)
        finally:
            source.stop()
            encoder.__del__()
            key = (self.camera_id, self.mode)
            if video_pipelines.get(key) is self:
                video_pipelines.pop(key, None)

    def broadcast(self, packet_data):
        for client in list(self.clients):
            if client.queue.full():
                client.stop()
                if client.queue.full():
                    client.queue.get_nowait()
                client.queue.put_nowait(RuntimeError("播放接收过慢，请重新连接"))
                continue
            client.queue.put_nowait(packet_data)

    async def run_native(self, binary):
        """Keep trying the VPU process during the board's boot warm-up.

        The media driver can accept the capture fd before the encoder service is
        ready. In that window the native process exits with an empty stdout and
        the old implementation permanently failed the WebRTC pipeline on the
        first attempt. A bounded exponential retry lets the VPU settle while
        preserving the strict hardware-only policy after the retry budget is
        exhausted.
        """
        failures = 0
        retry_started = time.monotonic()
        retry_delay = max(0.0, NATIVE_ENCODER_RETRY_INITIAL)
        while self.clients:
            self._native_attempt_had_frame = False
            try:
                await self._run_native_once(binary)
                failures = 0
                retry_started = time.monotonic()
                retry_delay = max(0.0, NATIVE_ENCODER_RETRY_INITIAL)
                # A resolution/fd change makes the one-shot worker return; let
                # it restart immediately with the new dimensions.
                if self.clients:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                camera = manager.cameras.get(self.camera_id)
                if camera is not None:
                    camera["encode_backend"] = "hardware-starting"
                    # Keep the public error clear while the VPU is warming up.
                    # The outer VideoPipeline handler sets encode_error only
                    # after this retry window is exhausted.
                    camera.pop("encode_error", None)
                if self._native_attempt_had_frame:
                    # A process which already produced video failed after a
                    # transient VPU/FD reset. Give the replacement process a
                    # fresh startup budget instead of counting the whole boot
                    # window against it.
                    retry_started = time.monotonic()
                    failures = 1
                    retry_delay = max(0.0, NATIVE_ENCODER_RETRY_INITIAL)
                elapsed = time.monotonic() - retry_started
                if elapsed >= max(0.0, NATIVE_ENCODER_RETRY_WINDOW):
                    raise RuntimeError(
                        f"硬件编码在启动阶段重试 {elapsed:.1f} 秒仍不可用：{exc}"
                    ) from exc
                log.warning(
                    "camera=%s native encoder not ready (attempt=%d, elapsed=%.1fs): %s",
                    self.camera_id,
                    failures,
                    elapsed,
                    exc,
                )
                if retry_delay:
                    await asyncio.sleep(retry_delay)
                retry_delay = min(
                    max(retry_delay * 2, NATIVE_ENCODER_RETRY_INITIAL),
                    max(NATIVE_ENCODER_RETRY_MAX, NATIVE_ENCODER_RETRY_INITIAL),
                )

    async def _run_native_once(self, binary):
        while self.clients:
            camera = manager.cameras.get(self.camera_id, {})
            fd = camera.get("_native_fd")
            if fd is None:
                raise RuntimeError("硬件视频采集已断开")
            resolution = manager.preview_resolution
            dimensions = (
                resolution
                if self.mode == "grid"
                else str(camera["width"]) + "x" + str(camera["height"])
            )
            width, height = map(int, dimensions.split("x"))
            fps = max(
                1, round(camera.get("source_fps") or camera.get("decode_fps") or 20)
            )
            proc = await asyncio.create_subprocess_exec(
                str(binary),
                str(fd),
                str(width),
                str(height),
                str(fps),
                pass_fds=(fd,),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # A 1080p IDR can exceed asyncio's 64 KiB default. Keep short
                # event-loop delays from blocking the native VPU output thread.
                limit=4 * 1024 * 1024,
            )
            started = time.monotonic()
            stderr_tail = deque(maxlen=16)

            async def drain_stderr():
                # Drain continuously: retaining stderr without a reader can
                # itself block the VPU helper. Bound retained diagnostics so a
                # noisy vendor driver cannot grow the server's memory.
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        return
                    stderr_tail.append(line.decode(errors="replace").strip()[-512:])

            stderr_task = asyncio.create_task(drain_stderr())
            watchdog = ProcessReadWatchdog(proc, 5)
            buffered_batch = 0
            failure = None
            try:
                while self.clients:
                    header = await proc.stdout.readexactly(20)
                    watchdog.reset(2)
                    pts, width, height, size = struct.unpack("<QIII", header)
                    if size <= 0 or size > 16 * 1024 * 1024:
                        raise RuntimeError("硬件编码帧大小无效")
                    payload = await proc.stdout.readexactly(size)
                    watchdog.reset(5)
                    self._native_attempt_had_frame = True
                    camera["encode_backend"] = "hardware"
                    camera.pop("encode_error", None)
                    camera["video_encoded_frames"] = (
                        camera.get("video_encoded_frames", 0) + 1
                    )
                    camera["video_encode_ms"] = None
                    self.broadcast(
                        (
                            payload,
                            max(1, int((started - self.epoch) * 90000)) + pts,
                            fractions.Fraction(1, 90000),
                        )
                    )
                    if (
                        manager.preview_resolution != resolution
                        or camera.get("_native_fd") != fd
                    ):
                        break
                    buffered_batch += 1
                    if buffered_batch % 8 == 0:
                        await asyncio.sleep(0)
            except Exception as exc:
                failure = asyncio.TimeoutError() if watchdog.expired else exc
            finally:
                await watchdog.close()
                await terminate_process(proc)
                try:
                    await asyncio.wait_for(stderr_task, 1)
                except asyncio.TimeoutError:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
            if failure is not None:
                detail = (
                    "等待硬件编码输出超时"
                    if isinstance(failure, asyncio.TimeoutError)
                    else str(failure)
                )
                diagnostic = "; ".join(stderr_tail)
                raise RuntimeError(
                    f"{detail} (native exit={proc.returncode})"
                    + (f"：{diagnostic}" if diagnostic else "")
                ) from failure


class EncodedCameraTrack(VideoStreamTrack):
    def __init__(self, camera_id, mode="grid"):
        super().__init__()
        self.camera_id = camera_id
        self.mode = mode
        self.pipeline = None
        self.queue = asyncio.Queue(maxsize=40)
        self.data_channel = None
        self.last_meta = ""

    async def recv(self):
        from av import Packet

        if self.pipeline is None or self.pipeline.mode != self.mode:
            if self.pipeline:
                self.pipeline.unsubscribe(self)
            while not self.queue.empty():
                self.queue.get_nowait()
            key = (self.camera_id, self.mode)
            pipeline = video_pipelines.get(key)
            if (
                pipeline is None
                or (pipeline.task and pipeline.task.done())
                or not pipeline.clients
            ):
                pipeline = video_pipelines[key] = VideoPipeline(*key)
            self.pipeline = pipeline
            pipeline.subscribe(self)
        data = await self.queue.get()
        if isinstance(data, Exception):
            raise data
        payload, pts, time_base = data
        if self.data_channel and self.data_channel.readyState == "open":
            camera = manager.cameras.get(self.camera_id, {})
            meta = camera.get(
                "detection_payload", {"camera_id": self.camera_id, "detections": []}
            )
            encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
            if encoded != self.last_meta:
                self.data_channel.send(encoded)
                self.last_meta = encoded
        packet = Packet(payload)
        packet.pts = packet.dts = pts
        packet.time_base = time_base
        return packet

    def stop(self):
        if self.pipeline:
            self.pipeline.unsubscribe(self)
        self.pipeline = None
        super().stop()


async def close_video_pipelines():
    pipelines = list(video_pipelines.values())
    for pipeline in pipelines:
        if pipeline.task:
            pipeline.task.cancel()
    await asyncio.gather(*(p.task for p in pipelines if p.task), return_exceptions=True)
    video_pipelines.clear()


async def _drop(pc):
    closing = peer_closing.get(pc)
    if closing is not None:
        await asyncio.shield(closing)
        return
    if pc not in peer_connections and pc not in peer_camera:
        return
    timer = peer_timers.pop(pc, None)
    if timer and timer is not asyncio.current_task():
        timer.cancel()
    closing = asyncio.create_task(pc.close())
    peer_closing[pc] = closing
    try:
        await asyncio.shield(closing)
    finally:
        peer_closing.pop(pc, None)
        peer_connections.discard(pc)
        peer_camera.pop(pc, None)
        peer_created.pop(pc, None)


class PeerLimitError(RuntimeError):
    pass


async def create_offer_answer(camera_id, offer_sdp, offer_type, mode="grid"):
    # Keep peers independent: the local Web UI and a future Home Assistant
    # camera provider may watch the same source at the same time. Peers are
    # removed by their connection-state handler, so renegotiation does not
    # require closing an unrelated viewer.
    if not manager.allow_software_fallback:
        camera = manager.cameras.get(camera_id, {})
        # ``hardware-starting`` is a transient boot state. Let the existing
        # pipeline keep retrying instead of rejecting a second offer while the
        # VPU driver is warming up. A hard error is marked explicitly after the
        # retry budget is exhausted.
        if camera.get("encode_backend") == "error" and camera.get("encode_error"):
            raise RuntimeError(camera["encode_error"])
    async with peer_lock:
        existing = [p for p, cid in peer_camera.items() if cid == camera_id]
        if len(existing) >= MAX_PEERS_PER_CAMERA:
            raise PeerLimitError("该摄像头观看连接已达上限")
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer_connections.add(pc)
        peer_camera[pc] = camera_id
        peer_created[pc] = time.monotonic()

    async def expire_unconnected():
        await asyncio.sleep(30)
        if pc.connectionState != "connected":
            await _drop(pc)

    peer_timers[pc] = asyncio.create_task(expire_unconnected())

    @pc.on("connectionstatechange")
    async def on_state_change():
        state = pc.connectionState
        if state in ("failed", "closed"):
            # close() emits another state callback. Its owner already waits
            # for transport cleanup; a recursive close would outlive shutdown.
            if pc not in peer_closing:
                await _drop(pc)
        elif state == "connected":
            timer = peer_timers.pop(pc, None)
            if timer:
                timer.cancel()
        elif state == "disconnected":
            # 'disconnected' is frequently transient on a LAN (ICE consent
            # refresh, brief jitter). Closing immediately is what made the
            # preview go black every so often; give ICE a grace window to
            # recover before tearing the stream down.
            async def _grace():
                await asyncio.sleep(8)
                if pc.connectionState in ("disconnected", "failed", "closed"):
                    await _drop(pc)

            timer = peer_timers.pop(pc, None)
            if timer:
                timer.cancel()
            peer_timers[pc] = asyncio.create_task(_grace())

    try:
        track = EncodedCameraTrack(camera_id, mode=mode)
        sender = pc.addTrack(track)
        # Older clients may offer video only; attach audio only when negotiated.
        has_audio = (
            any(t.kind == "audio" for t in pc.getTransceivers())
            or "\nm=audio " in offer_sdp
        )
        audio_source = manager.audio_sources.get(camera_id)
        if (
            has_audio
            and audio_source
            and manager.cameras.get(camera_id, {}).get("audio_present") is not False
        ):
            pc.addTrack(CameraAudioTrack(audio_source))

        # The browser creates the SCTP data channel before making its offer. The
        # server only receives that channel here; creating a new server-side
        # channel would require an application m-line that is absent from older
        # browser offers and would silently disable metadata delivery.
        @pc.on("datachannel")
        def on_datachannel(channel):
            if channel.label == "detections":
                track.data_channel = channel

                @channel.on("message")
                def on_message(message):
                    try:
                        payload = json.loads(message)
                        if payload.get("type") == "preview_mode" and payload.get(
                            "mode"
                        ) in ("grid", "fullscreen"):
                            track.mode = payload["mode"]
                    except (ValueError, TypeError, AttributeError):
                        pass

        transceiver = next(
            (item for item in pc.getTransceivers() if item.sender is sender), None
        )
        if transceiver is None:
            raise RuntimeError("WebRTC 视频 m-line 未找到")
        capabilities = RTCRtpSender.getCapabilities("video")
        h264 = [codec for codec in capabilities.codecs if codec.name.upper() == "H264"]
        if h264:
            transceiver.setCodecPreferences(h264)
        # aiortc matches codecs when applying the offer. Preferences set after
        # setRemoteDescription() do not change the already matched codec list.
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type=offer_type)
        )
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        for _ in range(200):
            if pc.iceGatheringState != "gathering":
                break
            await asyncio.sleep(0.01)
        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "codec": "H264" if h264 else "browser-default",
        }
    except BaseException:
        await _drop(pc)
        raise


async def close_peers(camera_id=None):
    peers = [
        pc
        for pc in peer_connections
        if camera_id is None or peer_camera.get(pc) == camera_id
    ]
    await asyncio.gather(*(_drop(pc) for pc in peers), return_exceptions=True)
    pending = [
        p.task for p in list(video_pipelines.values()) if not p.clients and p.task
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
