import asyncio
import collections
import time
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest

from aix5_ai.rtsp_output import (
    RtspOutputManager,
    _MuxWorker,
    h264_parameter_sets,
    is_keyframe,
)


def make_h264_packets(count=8):
    # Only the fixture uses software encoding: production receives pre-encoded
    # VPU packets and must remux them without opening another video encoder.
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = 320, 180, "yuv420p"
    encoder.time_base = Fraction(1, 20)
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
    packets = []
    for number in range(count):
        frame = av.VideoFrame.from_ndarray(
            np.full((180, 320, 3), number * 8, np.uint8), "rgb24"
        )
        frame.pts = number
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    return packets


def test_profile_uses_preview_dimensions_without_upscaling(tmp_path):
    manager = SimpleNamespace(
        preview_resolution="960x540", cameras={}, audio_sources={}
    )
    output = RtspOutputManager(manager, tmp_path)
    profile = output.media_profile(
        {"width": 1920, "height": 1080, "audio_present": True, "decode_fps": 20}
    )
    assert (profile["width"], profile["height"]) == (960, 540)
    assert profile["audio_codec"] == "aac" and profile["audio_sample_rate"] == 48000
    profile = output.media_profile(
        {"width": 640, "height": 480, "audio_present": False}
    )
    assert (profile["width"], profile["height"]) == (640, 480)
    assert profile["audio_codec"] is None
    assert (
        output.stream_uri("cam-1", "192.0.2.10")
        == "rtsp://192.0.2.10:8554/aix5/cam-1"
    )
    assert output.stream_uri("cam-1", "fe80::1") == "rtsp://[fe80::1]:8554/aix5/cam-1"


def test_server_auth_separates_viewer_and_local_publisher(tmp_path):
    output = RtspOutputManager(SimpleNamespace(), tmp_path)
    output.username, output.password = "viewer", "secret"
    config = output._config()
    publisher, viewer = config["authInternalUsers"]
    assert publisher["user"] != viewer["user"]
    assert publisher["pass"] != viewer["pass"]
    assert publisher["pass"].startswith("sha256:") and viewer["pass"].startswith(
        "sha256:"
    )
    assert viewer["user"].startswith("sha256:")
    assert "secret" not in str(config)
    assert publisher["ips"] == ["127.0.0.1", "::1"]
    assert publisher["permissions"] == [{"action": "publish", "path": "~^aix5/.*$"}]
    assert viewer["permissions"] == [{"action": "read", "path": "~^aix5/.*$"}]
    assert config["rtspTransports"] == ["tcp"]
    assert not config["api"] and not config["hls"]
    assert not config["moq"] and not config["playback"]


async def test_mux_copies_h264_and_encodes_aac_with_monotonic_timestamps(tmp_path):
    packets = make_h264_packets(12)
    assert all(h264_parameter_sets(bytes(packets[0])))
    assert is_keyframe(bytes(packets[0]))
    target = tmp_path / "bridge.ts"
    worker = _MuxWorker(
        str(target), packets[0], True, (320, 180), output_format="mpegts"
    )
    worker.start()
    await asyncio.wait_for(asyncio.wrap_future(worker.ready), 4)
    try:
        for number in range(30):
            if number % 3 == 0 and number // 3 + 1 < len(packets):
                worker.put("video", packets[number // 3 + 1])
            frame = av.AudioFrame(format="s16", layout="mono", samples=960)
            frame.sample_rate, frame.pts, frame.time_base = (
                48000,
                number * 960,
                Fraction(1, 48000),
            )
            tone = (np.sin(np.arange(960) * 2 * np.pi * 800 / 48000) * 10000).astype(
                "<i2"
            )
            frame.planes[0].update(tone.tobytes())
            worker.put("audio", frame)
        deadline = time.monotonic() + 4
        while not worker.items.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert worker.items.empty()
    finally:
        await worker.stop()
    with av.open(str(target)) as source:
        assert [
            (stream.type, stream.codec_context.name) for stream in source.streams
        ] == [("video", "h264"), ("audio", "aac")]
        video = source.streams.video[0]
        assert (video.width, video.height) == (320, 180)
        audio = source.streams.audio[0]
        assert audio.codec_context.sample_rate == 48000
        timestamps = collections.defaultdict(list)
        for packet in source.demux():
            if packet.pts is not None:
                timestamps[packet.stream.type].append(packet.pts)
        assert len(timestamps["video"]) >= 8
        assert len(timestamps["audio"]) >= 20
        for kind in ("video", "audio"):
            assert all(b > a for a, b in zip(timestamps[kind], timestamps[kind][1:]))


async def test_server_refuses_missing_credentials(tmp_path):
    output = RtspOutputManager(SimpleNamespace(), tmp_path, binary=tmp_path / "missing")
    with pytest.raises(RuntimeError, match="初始化 ONVIF/RTSP 认证"):
        await output._start_server()
    output.username, output.password = "viewer", "secret"
    with pytest.raises(RuntimeError, match="MediaMTX 未安装"):
        await output._start_server()


async def test_audio_probe_restarts_only_affected_publisher_and_cancellation_finishes(
    tmp_path, monkeypatch
):
    from aix5_ai import rtsp_output, webrtc
    from aix5_ai.audio import AudioSource

    packet_bytes = [bytes(packet) for packet in make_h264_packets(20)]

    class SyntheticTrack:
        def __init__(self, camera_id, mode):
            self.index = 0

        async def recv(self):
            await asyncio.sleep(0.02)
            packet = av.Packet(packet_bytes[self.index % len(packet_bytes)])
            packet.pts = packet.dts = self.index * 4500
            packet.time_base = Fraction(1, 90000)
            self.index += 1
            return packet

        def stop(self):
            pass

    workers = []

    class FileMuxWorker(_MuxWorker):
        def __init__(self, uri, first_packet, with_audio, dimensions):
            super().__init__(
                str(tmp_path / f"stream-{len(workers)}.ts"),
                first_packet,
                with_audio,
                dimensions,
                output_format="mpegts",
            )
            workers.append(self)

    cameras = {
        cid: {
            "id": cid,
            "enabled": True,
            "status": "running",
            "width": 320,
            "height": 180,
            "audio_present": None,
        }
        for cid in ("changing", "stable")
    }
    capture = SimpleNamespace(
        cameras=cameras, audio_sources={}, preview_resolution="960x540"
    )
    output = RtspOutputManager(capture, tmp_path)
    output.proc = SimpleNamespace(returncode=None)
    monkeypatch.setattr(webrtc, "EncodedCameraTrack", SyntheticTrack)
    monkeypatch.setattr(rtsp_output, "_MuxWorker", FileMuxWorker)

    async def stop_fake_server():
        output.proc = None

    monkeypatch.setattr(output, "_stop_server", stop_fake_server)

    async def wait_until(predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 5)

    await output.start()
    audio_producer = None
    try:
        await wait_until(
            lambda: all(
                c.get("rtsp_output_status") == "running" for c in cameras.values()
            )
        )
        old_publisher = output.publishers["changing"]
        stable_publisher = output.publishers["stable"]
        source = AudioSource(cameras["changing"])
        capture.audio_sources["changing"] = source

        async def produce_pcm():
            while True:
                source.sequence += 1
                source.chunks.append((source.sequence, time.monotonic(), bytes(1920)))
                source.changed.set()
                source.changed = asyncio.Event()
                await asyncio.sleep(0.02)

        audio_producer = asyncio.create_task(produce_pcm())
        cameras["changing"]["audio_present"] = True
        await wait_until(lambda: output.publishers["changing"] is not old_publisher)
        await wait_until(lambda: cameras["changing"].get("rtsp_audio_packets", 0) >= 3)
        assert old_publisher.done() and old_publisher.cancelled()
        assert output.publishers["stable"] is stable_publisher
        assert cameras["changing"]["rtsp_output_status"] == "running"
        assert workers[-1].with_audio
        assert not output.task.done()
    finally:
        if audio_producer:
            audio_producer.cancel()
            await asyncio.gather(audio_producer, return_exceptions=True)
        await asyncio.wait_for(output.stop(), 3)
    assert all(worker.finished.done() for worker in workers)


async def test_media_timeout_preserves_caller_cancel_when_recv_returns_during_cancel():
    from aix5_ai.rtsp_output import _await_with_timeout

    entered = asyncio.Event()
    cleanup = asyncio.Event()

    async def racing_recv():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A completed inner receive can win the cancellation race in the
            # board's asyncio.wait_for implementation. Reproduce that result.
            return "late frame"
        finally:
            cleanup.set()

    task = asyncio.create_task(_await_with_timeout(racing_recv(), 30))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleanup.is_set()


async def test_media_timeout_cancels_and_collects_pending_receive():
    from aix5_ai.rtsp_output import _await_with_timeout

    cleanup = asyncio.Event()

    async def idle_recv():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()

    with pytest.raises(asyncio.TimeoutError):
        await _await_with_timeout(idle_recv(), 0.01)
    assert cleanup.is_set()


async def test_media_cancel_wins_same_turn_receive_completion():
    from aix5_ai.rtsp_output import _await_with_timeout

    receive = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_await_with_timeout(receive, 30))
    await asyncio.sleep(0)
    # Python 3.10 asyncio.wait_for returns the completed frame here and loses
    # its caller's cancellation, which left the audio feed publishing forever.
    receive.set_result("completed frame")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_audio_cursor_preserves_all_pcm_after_scheduler_stall():
    from aix5_ai.rtsp_output import _PcmAudioCursor

    now = [100.0]
    source = SimpleNamespace(chunks=collections.deque(maxlen=20))

    def append(sequence):
        source.chunks.append(
            (sequence, now[0], np.full(960, sequence, dtype="<i2").tobytes())
        )

    append(100)
    append(101)
    cursor = _PcmAudioCursor(source, clock=lambda: now[0])
    first = cursor.read_available()
    assert len(first) == 1 and first[0].pts == 0
    # Audio decoded normally while the event loop could not run this consumer
    # for 300 ms. The next read must keep all fifteen chunks, not only one.
    now[0] += 0.3
    for sequence in range(102, 117):
        append(sequence)
    batch = cursor.read_available()
    assert len(batch) == 15
    assert sum(frame.samples for frame in batch) == 14400
    assert [frame.pts for frame in batch] == list(range(960, 15360, 960))
    assert [int(frame.to_ndarray()[0, 0]) for frame in batch] == list(range(102, 117))
    assert cursor.read_available() == []
    assert cursor.consumed_chunks == 16 and cursor.dropped_chunks == 0
    now[0] += 0.02
    append(117)
    assert cursor.read_available()[0].pts == 15360


def test_audio_cursor_overrun_keeps_bounded_live_edge_and_source_time():
    from aix5_ai.rtsp_output import _PcmAudioCursor

    now = [100.0]
    source = SimpleNamespace(chunks=collections.deque(maxlen=20))
    source.chunks.append((1, now[0], bytes(1920)))
    cursor = _PcmAudioCursor(source, clock=lambda: now[0])
    assert cursor.read_available()[0].pts == 0
    now[0] += 0.8
    for sequence in range(2, 42):
        source.chunks.append((sequence, now[0], bytes(1920)))
    frames = cursor.read_available()
    assert len(frames) == 20  # The source keeps at most 400 ms of audio.
    assert frames[0].pts == 21 * 960
    assert frames[-1].pts == 40 * 960
    assert cursor.consumed_chunks == 21 and cursor.dropped_chunks == 20
    assert frames[-1].pts * frames[-1].time_base == Fraction(4, 5)
