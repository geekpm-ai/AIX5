import asyncio
import struct

import numpy as np
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from aix5_ai import codec_tuning, webrtc
from aix5_ai.hw_capture import HardwareManager


class _NativeTestStdout:
    def __init__(self, fail, on_payload=None):
        self.fail = fail
        self.on_payload = on_payload
        self.sent_header = False

    async def read(self, size=-1):
        return b""

    async def readexactly(self, size):
        if self.fail:
            raise asyncio.IncompleteReadError(partial=b"", expected=size)
        if size == 20 and not self.sent_header:
            self.sent_header = True
            return struct.pack("<QIII", 90000, 640, 360, 1)
        if size == 1:
            if self.on_payload:
                self.on_payload()
            return b"\x00"
        raise AssertionError(f"unexpected native read size {size}")


class _NativeTestProcess:
    def __init__(self, fail, on_payload=None):
        self.returncode = 1 if fail else None
        self.stdout = _NativeTestStdout(fail, on_payload)
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


async def test_failed_negotiation_does_not_leak_peer():
    with pytest.raises(Exception):
        await webrtc.create_offer_answer("c", "not-an-sdp", "offer")
    assert not webrtc.peer_connections
    assert not webrtc.peer_camera
    assert not webrtc.peer_timers


async def test_local_webrtc_h264_video_and_detection_channel(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    manager = HardwareManager()
    manager.cameras["c"] = {
        "id": "c",
        "status": "running",
        "preview_fps": 5,
        "detection_payload": {
            "camera_id": "c",
            "width": 640,
            "height": 360,
            "detections": [],
        },
    }
    manager.images["c"] = np.zeros((360, 640, 3), np.uint8)
    monkeypatch.setattr(webrtc, "manager", manager)
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver.addTransceiver("video", direction="recvonly")
    channel = receiver.createDataChannel("detections")
    got_frame, got_metadata = asyncio.Future(), asyncio.Future()
    readers = []

    @receiver.on("track")
    def on_track(track):
        async def read():
            try:
                frame = await track.recv()
                if not got_frame.done():
                    got_frame.set_result(frame)
            except Exception as exc:
                if not got_frame.done():
                    got_frame.set_exception(exc)

        readers.append(asyncio.create_task(read()))

    @channel.on("message")
    def on_message(message):
        if not got_metadata.done():
            got_metadata.set_result(message)

    try:
        offer = await receiver.createOffer()
        await receiver.setLocalDescription(offer)
        answer = await webrtc.create_offer_answer(
            "c", receiver.localDescription.sdp, "offer"
        )
        await receiver.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        frame = await asyncio.wait_for(got_frame, 10)
        assert (frame.width, frame.height) == (640, 360)
        assert answer["codec"] == "H264"
        assert "H264/90000" in answer["sdp"]
        assert "VP8/90000" not in answer["sdp"]
        assert "camera_id" in await asyncio.wait_for(got_metadata, 5)
        channel.send('{"type":"preview_mode","mode":"fullscreen"}')
        await asyncio.sleep(0.05)
        assert next(iter(webrtc.peer_connections)).connectionState == "connected"
    finally:
        await receiver.close()
        await webrtc.close_peers("c")
        await asyncio.gather(*readers, return_exceptions=True)
    assert not webrtc.peer_connections
    assert not webrtc.peer_timers


async def test_video_follows_source_frames_not_analysis_fps(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    manager = HardwareManager()
    manager.cameras["c"] = {
        "id": "c",
        "analysis_fps": 5,
        "preview_fps": 5,
        "source_fps": 20,
        "enabled": True,
    }
    manager.generations["c"] = 0
    manager.frame_queues["c"] = asyncio.Queue(maxsize=1)
    monkeypatch.setattr(webrtc, "manager", manager)
    track = webrtc.CameraTrack("c")
    native = np.full((24, 16), 128, np.uint8)
    await manager.on_frame(manager.cameras["c"], None, None, nv12=native)
    first = await track.recv()
    assert manager.cameras["c"]["_full_rate_frame_consumers"] == 1
    waiting = asyncio.create_task(track.recv())
    await asyncio.sleep(0.02)
    assert not waiting.done()
    await manager.on_frame(manager.cameras["c"], None, None, nv12=native.copy())
    second = await asyncio.wait_for(waiting, 0.1)
    assert second.pts > first.pts
    assert manager.video_sequences["c"] == 2
    track.stop()
    assert "_full_rate_frame_consumers" not in manager.cameras["c"]
    for task in manager.preview_tasks.values():
        task.cancel()
    await asyncio.gather(*manager.preview_tasks.values(), return_exceptions=True)


async def test_native_encoder_retries_cold_boot_eof_before_reporting_error(monkeypatch):
    """A VPU process may exit empty while the board multimedia service warms up."""

    camera = {
        "id": "c",
        "_native_fd": 7,
        "width": 640,
        "height": 360,
        "source_fps": 20,
    }
    native_manager = type(
        "NativeManager",
        (),
        {
            "cameras": {"c": camera},
            "preview_resolution": "640x360",
        },
    )()
    monkeypatch.setattr(webrtc, "manager", native_manager)
    monkeypatch.setattr(webrtc, "NATIVE_ENCODER_RETRY_WINDOW", 1.0)
    monkeypatch.setattr(webrtc, "NATIVE_ENCODER_RETRY_INITIAL", 0.0)
    monkeypatch.setattr(webrtc, "NATIVE_ENCODER_RETRY_MAX", 0.0)

    pipeline = webrtc.VideoPipeline("c", "grid")
    client = object()
    pipeline.clients = {client}
    packets = []

    def publish(packet):
        packets.append(packet)
        pipeline.clients.clear()

    pipeline.broadcast = publish
    attempts = []

    async def spawn(*args, **kwargs):
        attempts.append(args)
        # The first cold-boot process exits before writing its 20-byte frame
        # header. The next process starts normally and publishes one packet.
        return _NativeTestProcess(
            fail=len(attempts) == 1,
            on_payload=lambda: pipeline.clients.clear(),
        )

    monkeypatch.setattr(webrtc.asyncio, "create_subprocess_exec", spawn)
    await pipeline.run_native("/tmp/rdk_video_stream")

    assert len(attempts) == 2
    assert packets and packets[0][0] == b"\x00"
    assert camera["encode_backend"] == "hardware"
    assert camera.get("encode_error") is None


async def test_native_pipeline_retries_transient_vpu_startup_failure(monkeypatch):
    manager = HardwareManager()
    manager.cameras["c"] = {"id": "c", "encode_error": None}
    monkeypatch.setattr(webrtc, "manager", manager)
    pipeline = webrtc.VideoPipeline("c", "grid")
    client = object()
    pipeline.clients.add(client)
    attempts = []

    async def flaky_worker(binary):
        attempts.append(binary)
        if len(attempts) < 3:
            raise RuntimeError("VPU 尚未就绪")
        manager.cameras["c"].update(encode_backend="hardware", encode_error=None)
        pipeline.clients.clear()

    monkeypatch.setattr(pipeline, "_run_native_once", flaky_worker)
    original_sleep = webrtc.asyncio.sleep
    monkeypatch.setattr(webrtc.asyncio, "sleep", lambda delay: original_sleep(0))
    await pipeline.run_native("/tmp/rdk_video_stream")
    assert len(attempts) == 3
    assert manager.cameras["c"]["encode_error"] is None


async def test_native_failure_reports_exit_code_and_bounded_stderr(monkeypatch):
    manager = HardwareManager()
    manager.cameras["c"] = {"id": "c", "_native_fd": 7, "source_fps": 20}
    monkeypatch.setattr(webrtc, "manager", manager)
    pipeline = webrtc.VideoPipeline("c", "grid")
    pipeline.clients.add(object())
    proc = _NativeTestProcess(fail=True)
    proc.returncode = 26
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_data(b"vendor early diagnostic\n" * 20)
    proc.stderr.feed_data(b"AIX5_STREAM_ERROR stage=submit code=-7\n")
    proc.stderr.feed_eof()

    async def spawn(*args, **kwargs):
        assert kwargs["stderr"] == asyncio.subprocess.PIPE
        return proc

    monkeypatch.setattr(webrtc.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="native exit=26.*stage=submit code=-7"):
        await pipeline._run_native_once("/tmp/rdk_video_stream")


async def test_close_peers_waits_for_close_already_started_by_state_callback():
    from unittest.mock import AsyncMock

    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()

    class Peer:
        pass

    pc = Peer()
    pc.close = AsyncMock(side_effect=close)
    webrtc.peer_connections.add(pc)
    webrtc.peer_camera[pc] = "c"
    dropping = asyncio.create_task(webrtc._drop(pc))
    await entered.wait()
    shutdown = asyncio.create_task(webrtc.close_peers("c"))
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await asyncio.gather(dropping, shutdown)
    pc.close.assert_awaited_once()
    assert not webrtc.peer_closing
    assert not webrtc.peer_connections


async def test_preview_resolution_updates_existing_track_without_reconnecting(
    monkeypatch,
):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    manager = HardwareManager()
    manager.cameras["c"] = {"preview_fps": 10}
    manager.images["c"] = np.zeros((1080, 1920, 3), np.uint8)
    monkeypatch.setattr(webrtc, "manager", manager)
    track = webrtc.CameraTrack("c")
    try:
        frame = await track.recv()
        assert (frame.width, frame.height) == (960, 540)
        manager.preview_resolution = "1920x1080"
        manager.images["c"] = manager.images["c"].copy()
        frame = await track.recv()
        assert (frame.width, frame.height) == (1920, 1080)
        manager.preview_resolution = "960x540"
        manager.images["c"] = manager.images["c"].copy()
        frame = await track.recv()
        assert (frame.width, frame.height) == (960, 540)
    finally:
        track.stop()


async def test_h264_receiver_observes_live_resolution_changes(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    # Portable codec protocol test; hardware integration is exercised separately.
    monkeypatch.setattr(
        codec_tuning,
        "HardwareH264Encoder",
        lambda *args: (_ for _ in ()).throw(RuntimeError("not installed")),
    )
    manager = HardwareManager()
    manager.cameras["c"] = {"preview_fps": 10}
    manager.images["c"] = np.zeros((1080, 1920, 3), np.uint8)
    monkeypatch.setattr(webrtc, "manager", manager)
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver.addTransceiver("video", direction="recvonly")
    channel = receiver.createDataChannel("detections")
    received_sizes = []
    readers = []

    @receiver.on("track")
    def on_track(track):
        async def read():
            try:
                while True:
                    frame = await track.recv()
                    received_sizes.append((frame.width, frame.height))
            except Exception:
                pass

        readers.append(asyncio.create_task(read()))

    async def wait_size(size):
        while not received_sizes or received_sizes[-1] != size:
            await asyncio.sleep(0.01)

    try:
        await receiver.setLocalDescription(await receiver.createOffer())
        answer = await webrtc.create_offer_answer(
            "c", receiver.localDescription.sdp, "offer"
        )
        await receiver.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        await asyncio.wait_for(wait_size((960, 540)), 5)
        manager.preview_resolution = "1920x1080"
        await asyncio.wait_for(wait_size((1920, 1080)), 5)
        manager.preview_resolution = "960x540"
        await asyncio.wait_for(wait_size((960, 540)), 5)
        channel.send('{"type":"preview_mode","mode":"fullscreen"}')
        await asyncio.wait_for(wait_size((1920, 1080)), 5)
        channel.send('{"type":"preview_mode","mode":"grid"}')
        await asyncio.wait_for(wait_size((960, 540)), 5)
        assert len(webrtc.peer_connections) == 1
        assert receiver.connectionState == "connected"
    finally:
        await receiver.close()
        await webrtc.close_peers("c")
        await asyncio.gather(*readers, return_exceptions=True)


async def test_preview_never_upscales_degraded_source(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    manager = HardwareManager()
    manager.preview_resolution = "1920x1080"
    manager.cameras["c"] = {"preview_fps": 5, "decode_backend": "software"}
    manager.images["c"] = np.zeros((540, 960, 3), np.uint8)
    monkeypatch.setattr(webrtc, "manager", manager)
    track = webrtc.CameraTrack("c")
    try:
        frame = await track.recv()
        assert (frame.width, frame.height) == (960, 540)
    finally:
        track.stop()


async def test_viewers_share_one_video_pipeline_and_cleanup(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    monkeypatch.setattr(
        codec_tuning,
        "HardwareH264Encoder",
        lambda *args: (_ for _ in ()).throw(RuntimeError("not installed")),
    )
    manager = HardwareManager()
    manager.cameras["c"] = {"id": "c", "source_fps": 20}
    manager.images["c"] = np.zeros((360, 640, 3), np.uint8)
    monkeypatch.setattr(webrtc, "manager", manager)
    one = webrtc.EncodedCameraTrack("c")
    two = webrtc.EncodedCameraTrack("c")
    try:
        a, b = await asyncio.wait_for(asyncio.gather(one.recv(), two.recv()), 2)
        assert one.pipeline is two.pipeline
        assert len(webrtc.video_pipelines) == 1
        assert bytes(a) == bytes(b)
        assert a.pts == b.pts
    finally:
        one.stop()
        two.stop()
        await webrtc.close_video_pipelines()
    assert not webrtc.video_pipelines
