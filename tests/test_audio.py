import asyncio
import math
import time
from unittest.mock import AsyncMock

import numpy as np
import pytest

from aix5_ai.audio import AudioSource, CameraAudioTrack, audio_level, parse_rate


def test_audio_meter_silence_and_tone():
    assert audio_level(bytes(1920)) == (-60, 0)
    tone = (
        (np.sin(np.arange(960) * 2 * math.pi * 1000 / 48000) * 16384)
        .astype("<i2")
        .tobytes()
    )
    db, level = audio_level(tone)
    assert -10 < db < -8
    assert 80 < level < 90
    assert parse_rate("20/1") == 20
    assert parse_rate("0/0") is None


async def test_audio_track_returns_live_pcm_with_monotonic_clock():
    source = AudioSource({"id": "c"})
    tone = np.full(960, 1000, np.int16).tobytes()
    source.chunks.append((1, time.monotonic(), tone))
    track = CameraAudioTrack(source)
    try:
        first = await track.recv()
        second = await track.recv()
        assert first.sample_rate == 48000 and first.samples == 960
        assert second.pts - first.pts == 960
        assert np.any(first.to_ndarray())
        assert not np.any(second.to_ndarray())
    finally:
        track.stop()


async def test_audio_clock_skips_stalled_time_instead_of_slowing_video():
    source = AudioSource({"id": "c"})
    track = CameraAudioTrack(source)
    try:
        first = await track.recv()
        track.epoch -= 0.2
        second = await track.recv()
        assert second.pts - first.pts >= 9 * 960
        third = await track.recv()
        assert third.pts - second.pts == 960
    finally:
        track.stop()


async def test_no_audio_probe_sets_absent_without_starting_decoder(monkeypatch):
    source = AudioSource({"id": "c"})
    monkeypatch.setattr(source, "probe", AsyncMock(return_value=None))
    decode = AsyncMock()
    monkeypatch.setattr(source, "decode", decode)
    await source.start()
    await asyncio.sleep(0.01)
    assert source.camera["audio_status"] == "absent"
    decode.assert_not_awaited()
    await source.stop()
    assert source.proc is None and source.task is None


async def test_webRTC_negotiates_opus_and_receives_audio(monkeypatch):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aix5_ai import webrtc, codec_tuning
    from aix5_ai.hw_capture import HardwareManager

    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    manager = HardwareManager()
    manager.cameras["c"] = {"id": "c", "source_fps": 20}
    manager.images["c"] = np.zeros((360, 640, 3), np.uint8)
    source = AudioSource(manager.cameras["c"])
    source.chunks.append((1, time.monotonic(), np.full(960, 1000, np.int16).tobytes()))
    manager.audio_sources["c"] = source
    monkeypatch.setattr(webrtc, "manager", manager)
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver.addTransceiver("video", direction="recvonly")
    receiver.addTransceiver("audio", direction="recvonly")
    received = asyncio.Future()
    readers = []

    @receiver.on("track")
    def on_track(track):
        if track.kind == "audio":

            async def read():
                try:
                    frame = await track.recv()
                    if not received.done():
                        received.set_result(frame)
                except Exception as exc:
                    if not received.done():
                        received.set_exception(exc)

            readers.append(asyncio.create_task(read()))

    try:
        await receiver.setLocalDescription(await receiver.createOffer())
        answer = await webrtc.create_offer_answer(
            "c", receiver.localDescription.sdp, "offer"
        )
        assert "opus/48000" in answer["sdp"]
        await receiver.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        frame = await asyncio.wait_for(received, 5)
        assert frame.sample_rate == 48000
        assert frame.samples > 0
    finally:
        await receiver.close()
        await webrtc.close_peers("c")
        await asyncio.gather(*readers, return_exceptions=True)


def test_common_audio_codecs_decode_to_pcm():
    import av
    from fractions import Fraction

    # Exercise independent codec paths used by FFmpeg/PyAV, not aliases of AAC.
    for codec, rate in [
        ("aac", 16000),
        ("pcm_alaw", 8000),
        ("pcm_mulaw", 8000),
        ("libopus", 48000),
    ]:
        encoder = av.CodecContext.create(codec, "w")
        encoder.sample_rate = rate
        encoder.layout = "mono"
        encoder.format = "fltp" if codec == "aac" else "s16"
        decoder = av.CodecContext.create("opus" if codec == "libopus" else codec, "r")
        if codec.startswith("pcm_"):
            decoder.sample_rate = rate
            decoder.layout = "mono"
        decoded = []
        try:
            encoder.open()
            decoder.extradata = encoder.extradata
            for index in range(4):
                count = encoder.frame_size or 960
                frame = av.AudioFrame(
                    format=encoder.format.name, layout="mono", samples=count
                )
                frame.sample_rate = rate
                frame.pts = index * count
                frame.time_base = Fraction(1, rate)
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                for packet in encoder.encode(frame):
                    decoded.extend(decoder.decode(packet))
            for packet in encoder.encode(None):
                decoded.extend(decoder.decode(packet))
            assert decoded, codec
            resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            assert any(resampler.resample(frame) for frame in decoded)
        finally:
            pass


async def test_pcm_decoder_drains_buffered_chunks_before_fair_yield(monkeypatch):
    from types import SimpleNamespace

    reader = asyncio.StreamReader()
    reader.feed_data(bytes(16 * 960 * 2))
    reader.feed_eof()
    proc = SimpleNamespace(stdout=reader, returncode=None)
    proc.terminate = lambda: setattr(proc, "returncode", 0)
    proc.kill = proc.terminate
    proc.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    source = AudioSource({"id": "c", "rtsp_url": "rtsp://localhost/generated"})
    observed = []

    async def decode():
        with pytest.raises(asyncio.IncompleteReadError):
            await source.decode()

    async def consumer():
        observed.append(source.sequence)
        await asyncio.sleep(0)
        observed.append(source.sequence)

    await asyncio.gather(decode(), consumer())
    # A per-read wait_for task used to yield before even the first buffered
    # chunk. Now source consumption stays fast, while consumers run every8.
    assert observed == [8, 16]
    assert source.camera["audio_decoded_chunks"] == 16
    assert source.camera["audio_decoded_samples"] == 16 * 960
    assert source.proc is None


async def test_audio_process_cleanup_drains_paused_stdout_without_hanging():
    import sys

    source = AudioSource({"id": "c"})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os,time; os.write(1, b'x' * 4194304); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        limit=1024,
    )
    source.proc = proc
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(source.close_process(), 2)
        assert proc.returncode is not None
        assert source.proc is None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
