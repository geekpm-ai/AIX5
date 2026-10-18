"""Authenticated RTSP output for ONVIF/Home Assistant.

The video packets come from the same hardware H.264 pipeline as WebRTC. No
video encoder is opened here. Audio alone is encoded to AAC for HA's stream
integration. MediaMTX provides the RTSP server and authenticates the application's
single account; the upstream camera URL and its credentials never leave AIX5.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import io
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

import av

from .audio import RATE, SAMPLES

log = logging.getLogger("aix5.rtsp_output")
VIDEO_TIME_BASE = Fraction(1, 90000)
IO_TIMEOUT = 4.0


async def _await_with_timeout(awaitable, timeout):
    """Preserve caller cancellation on the board's Python 3.10.

    asyncio.wait_for() can swallow cancellation when its input completes in the
    same event-loop turn. A media feed then keeps running while its supervisor
    waits forever for cleanup. wait() never transfers that cancellation to the
    input implicitly; always cancel and collect the input explicitly below.
    """
    pending = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({pending}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def h264_parameter_sets(payload: bytes) -> tuple[bytes, bytes]:
    """Return Annex-B SPS/PPS, accepting three and four byte start codes."""
    parts = re.split(b"\x00\x00\x00?\x01", payload)
    sps = next((part for part in parts if part and part[0] & 31 == 7), b"")
    pps = next((part for part in parts if part and part[0] & 31 == 8), b"")
    return sps, pps


def is_keyframe(payload: bytes) -> bool:
    return any(
        part and part[0] & 31 == 5 for part in re.split(b"\x00\x00\x00?\x01", payload)
    )


class _PcmAudioCursor:
    """Drain retained source PCM without tying audio rate to event-loop cadence."""

    def __init__(self, source, clock=time.monotonic):
        self.source = source
        self.clock = clock
        self.epoch = clock()
        self.last_sequence = None
        self.origin_sequence = None
        self.origin_pts = 0
        self.consumed_chunks = 0
        self.dropped_chunks = 0

    def read_available(self):
        chunks = tuple(self.source.chunks)
        if not chunks or self.clock() - chunks[-1][1] > 1:
            return []
        if self.last_sequence is None:
            # A newly opened RTSP session begins at the live edge. Subsequent
            # reads keep every chunk retained during a scheduling delay.
            self.last_sequence = chunks[-1][0] - 1
            self.origin_sequence = chunks[-1][0]
            self.origin_pts = max(
                0, int((self.clock() - self.epoch) * RATE) // SAMPLES * SAMPLES
            )
        frames = []
        for sequence, _received_at, pcm in chunks:
            if sequence <= self.last_sequence:
                continue
            self.dropped_chunks += max(0, sequence - self.last_sequence - 1)
            self.consumed_chunks += 1
            frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES)
            frame.planes[0].update(pcm)
            frame.sample_rate = RATE
            frame.time_base = Fraction(1, RATE)
            # Continuous while all chunks fit in the source's 400 ms ring. On
            # overrun retain source time instead of stretching old sound to fill
            # the gap; neither memory nor playback lag can grow without bound.
            frame.pts = self.origin_pts + (sequence - self.origin_sequence) * SAMPLES
            frames.append(frame)
            self.last_sequence = sequence
        return frames


class _MuxWorker:
    """One bounded network/mux thread; capture and the web event loop never block."""

    def __init__(
        self, uri, first_packet, with_audio, dimensions, *, output_format="rtsp"
    ):
        self.uri = uri
        self.first_packet = first_packet
        self.with_audio = with_audio
        self.dimensions = dimensions
        self.output_format = output_format
        self.items = queue.Queue(maxsize=100)
        self.stopping = threading.Event()
        self.ready = concurrent.futures.Future()
        self.finished = concurrent.futures.Future()
        self.thread = threading.Thread(
            target=self._run, name="aix5-rtsp-mux", daemon=True
        )
        self.video_packets = 0
        self.audio_packets = 0

    def start(self):
        self.thread.start()

    def put(self, kind, item):
        # Fail and reconnect at the live edge instead of accumulating stale media.
        self.items.put_nowait((kind, item))

    async def stop(self):
        self.stopping.set()
        try:
            self.items.put_nowait(("stop", None))
        except queue.Full:
            pass
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    asyncio.shield(asyncio.wrap_future(self.finished)),
                    return_exceptions=True,
                ),
                IO_TIMEOUT * 2 + 1,
            )
        except asyncio.TimeoutError:
            # PyAV applies its interrupt callback to inputs only. The owner
            # closes the local MediaMTX server to unblock a stalled RTSP socket.
            log.warning("RTSP mux shutdown exceeded the network timeout")
            return False
        return True

    def _run(self):
        # Libav errors may include the authenticated publishing URI. Capture
        # thread-local diagnostics; the owning task reports only exception types.
        with av.logging.Capture(local=True) as captured:
            self._mux(captured)

    def _mux(self, captured):
        output = None
        template = None
        failure = None
        try:
            first = self.first_packet
            sps, pps = h264_parameter_sets(bytes(first))
            # Feed only parameter sets to the demuxer. No compressed picture is
            # submitted to a software decoder, even for stream probing.
            headers = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
            template = av.open(io.BytesIO(headers), format="h264")
            template_video = template.streams.video[0]
            template_video.codec_context.width, template_video.codec_context.height = (
                self.dimensions
            )
            template_video.codec_context.pix_fmt = "yuv420p"
            # Keep a missing audio source from holding video in the interleaver
            # for libav's default ten seconds.
            options = {"max_interleave_delta": "100000"}
            if self.output_format == "rtsp":
                options["rtsp_transport"] = "tcp"
            output = av.open(
                self.uri,
                "w",
                format=self.output_format,
                options=options,
                timeout=(IO_TIMEOUT, IO_TIMEOUT),
            )
            # Remux mode copies existing codec parameters and never opens or invokes
            # a software video encoder. The template parser only reads SPS/PPS.
            video = output.add_stream_from_template(template.streams.video[0])
            video.codec_context.width, video.codec_context.height = self.dimensions
            video.codec_context.pix_fmt = "yuv420p"
            video.time_base = VIDEO_TIME_BASE
            audio = None
            resampler = None
            if self.with_audio:
                audio = output.add_stream("aac", rate=48000)
                audio.layout = "mono"
                audio.bit_rate = 64000
                audio.codec_context.thread_count = 1
                resampler = av.AudioResampler(format="fltp", layout="mono", rate=48000)
            output.start_encoding()
            if video.codec_context.is_open:
                raise RuntimeError("RTSP video remux unexpectedly opened an encoder")
            self.ready.set_result(True)
            origin = first.pts * first.time_base
            last_video_pts = -1

            def write_video(packet):
                nonlocal last_video_pts
                timestamp = int(
                    (packet.pts * packet.time_base - origin) / VIDEO_TIME_BASE
                )
                timestamp = max(last_video_pts + 1, timestamp)
                packet.pts = packet.dts = timestamp
                packet.time_base = VIDEO_TIME_BASE
                packet.stream = video
                packet.is_keyframe = is_keyframe(bytes(packet))
                output.mux(packet)
                last_video_pts = timestamp
                self.video_packets += 1

            write_video(first)
            while not self.stopping.is_set():
                captured.clear()
                try:
                    kind, item = self.items.get(timeout=1)
                except queue.Empty:
                    continue
                if kind == "stop":
                    break
                if kind == "video":
                    write_video(item)
                elif kind == "audio" and audio is not None:
                    for frame in resampler.resample(item):
                        for packet in audio.encode(frame):
                            output.mux(packet)
                            self.audio_packets += 1
            # Live streams can stop without flushing delayed AAC; a short trailer
            # closes RTSP cleanly while avoiding a backlog after reconnecting.
        except Exception as exc:  # noqa: BLE001 - transfer worker failures to its owner
            if not self.ready.done():
                self.ready.set_exception(exc)
            failure = exc
        finally:
            if output is not None:
                try:
                    output.close()
                except (av.FFmpegError, OSError):
                    log.debug("RTSP transport already closed")
            if template is not None:
                template.close()
            if not self.finished.done():
                if failure is not None:
                    self.finished.set_exception(failure)
                else:
                    self.finished.set_result(None)


class RtspOutputManager:
    """Manage MediaMTX and one shared H.264/AAC publication per enabled camera."""

    def __init__(self, capture_manager, data_dir, binary=None, port=8554):
        self.manager = capture_manager
        self.data_dir = Path(data_dir)
        self.binary = Path(
            binary
            or os.getenv(
                "AIX5_MEDIAMTX_BIN",
                str(Path(__file__).resolve().parent.parent / "native/mediamtx"),
            )
        )
        self.port = int(port)
        self.username = None
        self.password = None
        self.publisher_user = "aix5_publish_" + secrets.token_hex(8)
        self.publisher_password = secrets.token_urlsafe(32)
        self.proc = None
        self.task = None
        self.publishers = {}
        self._signatures = {}
        self._lock = asyncio.Lock()
        self.status = "stopped"
        self.error = None

    def stream_uri(self, camera_id, public_host):
        # Accept a hostname/IP, never source credentials or a source URL.
        host = public_host
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        return f"rtsp://{host}:{self.port}/aix5/{quote(str(camera_id), safe='')}"

    def media_profile(self, camera):
        width, height = (
            int(camera.get("width") or 1920),
            int(camera.get("height") or 1080),
        )
        target_width, target_height = map(
            int, self.manager.preview_resolution.split("x")
        )
        ratio = min(1.0, target_width / width, target_height / height)
        audio = camera.get("audio_present") is True
        return {
            "width": max(2, int(width * ratio) // 2 * 2),
            "height": max(2, int(height * ratio) // 2 * 2),
            "fps": camera.get("decode_fps") or camera.get("source_fps") or 20,
            "audio_present": audio,
            "audio_codec": "aac" if audio else None,
            "audio_sample_rate": 48000 if audio else None,
        }

    async def configure(self, username, password):
        async with self._lock:
            if (self.username, self.password) == (username, password):
                return
            self.username, self.password = username, password
            was_running = self.task is not None
            await self._stop()
            if was_running:
                await self._start()

    async def start(self):
        async with self._lock:
            await self._start()

    async def _start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._supervise())

    async def stop(self):
        async with self._lock:
            await self._stop()

    async def _stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self._stop_publishers()
        await self._stop_server()
        self.status = "stopped"

    async def refresh(self):
        # The supervisor observes the live capture dictionary every second; this
        # hook allows callers to request immediate pruning after camera deletion.
        for cid in list(self.publishers):
            if not self.manager.cameras.get(cid, {}).get("enabled", False):
                self.publishers[cid].cancel()

    @staticmethod
    def _task_state(task):
        if task is None:
            return {"state": "absent"}
        state = {
            "state": "cancelled"
            if task.cancelled()
            else "done"
            if task.done()
            else "running",
            "cancelling": getattr(task, "cancelling", lambda: 0)(),
            "await": [],
        }
        if task.done() and not task.cancelled():
            error = task.exception()
            if error is not None:
                state["error_type"] = type(error).__name__
        current = task.get_coro()
        for _ in range(12):
            frame = getattr(current, "cr_frame", None) or getattr(
                current, "gi_frame", None
            )
            if frame is not None:
                state["await"].append(f"{frame.f_code.co_name}:{frame.f_lineno}")
            current = getattr(current, "cr_await", None) or getattr(
                current, "gi_yieldfrom", None
            )
            if current is None:
                break
        return state

    def snapshot(self):
        return {
            "status": self.status,
            "error": self.error,
            "port": self.port,
            "supervisor": self._task_state(self.task),
            "publishers": {
                cid: self._task_state(task) for cid, task in self.publishers.items()
            },
            "streams": {
                cid: camera.get("rtsp_output_status", "waiting")
                for cid, camera in self.manager.cameras.items()
                if camera.get("enabled", True)
            },
        }

    def _config(self):
        def hashed(secret):
            return (
                "sha256:"
                + base64.b64encode(hashlib.sha256(secret.encode()).digest()).decode()
            )

        return {
            "logLevel": "warn",
            "rtspAddress": f":{self.port}",
            "rtspTransports": ["tcp"],
            "rtspAuthMethods": ["basic"],
            "rtmp": False,
            "hls": False,
            "webrtc": False,
            "srt": False,
            "moq": False,
            "playback": False,
            "api": False,
            "metrics": False,
            "pprof": False,
            "authMethod": "internal",
            "authInternalUsers": [
                {
                    "user": self.publisher_user,
                    "pass": hashed(self.publisher_password),
                    "ips": ["127.0.0.1", "::1"],
                    "permissions": [{"action": "publish", "path": "~^aix5/.*$"}],
                },
                {
                    # MediaMTX interprets the literal name "any" as an
                    # anonymous wildcard. Hashing preserves exact account
                    # matching even for names reserved by its configuration.
                    "user": hashed(self.username),
                    "pass": hashed(self.password),
                    "permissions": [{"action": "read", "path": "~^aix5/.*$"}],
                },
            ],
            "paths": {"~^aix5/[A-Za-z0-9_-]+$": {"source": "publisher"}},
        }

    async def _start_server(self):
        if not self.username or not self.password:
            raise RuntimeError("请先使用系统账号登录，以初始化 ONVIF/RTSP 认证")
        if not self.binary.is_file():
            raise RuntimeError("MediaMTX 未安装，无法启动 ONVIF RTSP 输出")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        config = self.data_dir / "mediamtx.json"
        temporary = self.data_dir / (".mediamtx-" + secrets.token_hex(4))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self._config(), stream)
            os.replace(temporary, config)
        finally:
            temporary.unlink(missing_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            str(self.binary),
            str(config),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.proc.returncode is not None:
                raise RuntimeError(
                    f"MediaMTX 启动失败（退出码 {self.proc.returncode}）"
                )
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self.port), 0.3
                )
                writer.close()
                await writer.wait_closed()
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.1)
        raise RuntimeError("MediaMTX RTSP 端口启动超时")

    async def _stop_server(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def _stop_publishers(self):
        tasks = list(self.publishers.values())
        self.publishers.clear()
        self._signatures.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise(self):
        try:
            while True:
                try:
                    if self.proc is None or self.proc.returncode is not None:
                        await self._stop_publishers()
                        await self._stop_server()
                        self.status = "starting"
                        await self._start_server()
                    self.status, self.error = "running", None
                    desired = {
                        cid: camera
                        for cid, camera in self.manager.cameras.items()
                        if camera.get("enabled", True)
                    }
                    for cid, task in list(self.publishers.items()):
                        signature = (
                            self._signature(desired[cid]) if cid in desired else None
                        )
                        if task.done() or self._signatures.get(cid) != signature:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            self.publishers.pop(cid, None)
                            self._signatures.pop(cid, None)
                    for cid, camera in desired.items():
                        if cid not in self.publishers:
                            self._signatures[cid] = self._signature(camera)
                            self.publishers[cid] = asyncio.create_task(
                                self._publish_loop(cid)
                            )
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - supervisor retries infrastructure failures
                    self.status, self.error = "error", str(exc)
                    log.warning("RTSP output unavailable: %s", exc)
                    await self._stop_publishers()
                    await self._stop_server()
                    await asyncio.sleep(5)
        finally:
            await self._stop_publishers()
            await self._stop_server()

    def _signature(self, camera):
        profile = self.media_profile(camera)
        return (
            profile["width"],
            profile["height"],
            profile["audio_present"],
            id(self.manager.audio_sources.get(camera["id"])),
        )

    async def _publish_loop(self, cid):
        delay = 0.5
        while True:
            camera = self.manager.cameras.get(cid)
            if not camera or not camera.get("enabled", True):
                return
            try:
                camera["rtsp_output_status"] = "starting"
                camera.pop("rtsp_output_error", None)
                # Wait for actual input, never publish the placeholder black frame.
                if camera.get("status") != "running" or not camera.get("width"):
                    await asyncio.sleep(0.5)
                    continue
                await self._publish_once(cid)
                delay = 0.5
            except asyncio.CancelledError:
                camera["rtsp_output_status"] = "stopped"
                raise
            except Exception as exc:  # noqa: BLE001 - isolate failures of one camera
                # Exceptions from libav can contain the publisher URL/secret. Keep
                # detailed transport messages out of the API and ordinary logs.
                camera["rtsp_output_status"] = "retrying"
                camera["rtsp_output_error"] = "RTSP 输出暂时中断，正在重新连接"
                log.warning(
                    "RTSP publisher retry camera=%s type=%s", cid, type(exc).__name__
                )
                await asyncio.sleep(delay)
                delay = min(10, delay * 2)

    async def _publish_once(self, cid):
        from .webrtc import EncodedCameraTrack

        video = EncodedCameraTrack(cid, mode="grid")
        source = self.manager.audio_sources.get(cid)
        camera = self.manager.cameras[cid]
        has_audio = camera.get("audio_present") is True and source is not None
        worker = None
        pending = []
        stopping = asyncio.Event()
        try:
            deadline = time.monotonic() + 45
            while True:
                packet = await _await_with_timeout(
                    video.recv(), max(0.1, deadline - time.monotonic())
                )
                sps, pps = h264_parameter_sets(bytes(packet))
                if sps and pps and is_keyframe(bytes(packet)):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("等待硬件 H.264 关键帧超时")
            uri = (
                f"rtsp://{quote(self.publisher_user, safe='')}:{quote(self.publisher_password, safe='')}"
                f"@127.0.0.1:{self.port}/aix5/{quote(cid, safe='')}"
            )
            profile = self.media_profile(camera)
            worker = _MuxWorker(
                uri, packet, has_audio, (profile["width"], profile["height"])
            )
            worker.start()
            await _await_with_timeout(
                asyncio.shield(asyncio.wrap_future(worker.ready)), IO_TIMEOUT + 2
            )
            camera["rtsp_output_status"] = "running"
            camera.pop("rtsp_output_error", None)

            last_video_at = time.monotonic()

            async def feed(track, kind):
                nonlocal last_video_at
                batch = 0
                while not stopping.is_set():
                    frame = await track.recv()
                    if stopping.is_set():
                        return
                    last_video_at = time.monotonic()
                    worker.put(kind, frame)
                    camera["rtsp_video_packets"] = worker.video_packets
                    camera["rtsp_audio_packets"] = worker.audio_packets
                    batch += 1
                    if batch % 8 == 0:
                        await asyncio.sleep(0)

            async def watch_video():
                while not stopping.is_set():
                    await asyncio.sleep(0.5)
                    if time.monotonic() - last_video_at >= 45:
                        raise asyncio.TimeoutError("RTSP video source idle")

            async def feed_audio():
                cursor = _PcmAudioCursor(source)
                while not stopping.is_set():
                    changed = source.changed
                    frames = cursor.read_available()
                    if frames:
                        for frame in frames:
                            if stopping.is_set():
                                return
                            worker.put("audio", frame)
                        camera["rtsp_audio_packets"] = worker.audio_packets
                        camera["rtsp_audio_pcm_chunks"] = cursor.consumed_chunks
                        camera["rtsp_audio_pcm_samples"] = (
                            cursor.consumed_chunks * SAMPLES
                        )
                        camera["rtsp_audio_dropped_chunks"] = cursor.dropped_chunks
                        camera["rtsp_audio_source_sequence"] = cursor.last_sequence
                        camera["rtsp_mux_queued_frames"] = worker.items.qsize()
                    else:
                        # No extra task per PCM wake-up. Source process timeout
                        # and publisher cancellation already bound this wait.
                        await changed.wait()

            pending = [
                asyncio.create_task(feed(video, "video")),
                asyncio.create_task(watch_video()),
            ]
            if has_audio:
                pending.append(asyncio.create_task(feed_audio()))
            completion = asyncio.wrap_future(worker.finished)
            done, _ = await asyncio.wait(
                [*pending, completion], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            stopping.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            video.stop()
            if worker and not await worker.stop():
                # All publisher sockets target our own loopback server. Terminating
                # it forcibly closes blocked writes; the supervisor restarts it.
                await self._stop_server()
                await worker.stop()
