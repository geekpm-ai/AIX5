import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from aix5_ai.audio import (
    AudioSource,
    CameraAudioTrack,
    EncodedCameraAudioTrack,
    _OpusPipeline,
    audio_level,
    parse_rate,
)


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
        source.chunks.append((2, time.monotonic(), tone))
        second = await track.recv()
        assert first.sample_rate == 48000 and first.samples == 960
        assert second.pts - first.pts == 960
        assert np.any(first.to_ndarray())
        assert np.any(second.to_ndarray())
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


def publish_pcm(source, sequence, received_at=None):
    """Give each input chunk a distinguishable, non-silent waveform."""
    pcm = np.full(960, sequence, dtype="<i2").tobytes()
    source.chunks.append(
        (sequence, time.monotonic() if received_at is None else received_at, pcm)
    )
    source.changed.set()
    source.changed = asyncio.Event()


async def test_webrtc_audio_preserves_continuous_pcm_delivered_in_200ms_bursts():
    source = AudioSource({"id": "c"})
    publish_pcm(source, 1)
    track = CameraAudioTrack(source)
    first = await track.recv()

    async def produce():
        for batch in range(3):
            await asyncio.sleep(0.2)
            for sequence in range(2 + batch * 10, 12 + batch * 10):
                publish_pcm(source, sequence)

    async def consume():
        return [await track.recv() for _ in range(30)]

    try:
        _, received = await asyncio.wait_for(asyncio.gather(produce(), consume()), 2)
        assert [int(frame.to_ndarray()[0, 0]) for frame in received] == list(
            range(2, 32)
        )
        assert [frame.pts - first.pts for frame in received] == [
            sequence * 960 for sequence in range(1, 31)
        ]
    finally:
        track.stop()


async def test_bursty_pcm_remains_continuous_one_khz_audio_after_opus_roundtrip():
    from aiortc.codecs.opus import OpusDecoder, OpusEncoder
    from aiortc.jitterbuffer import JitterFrame

    source = AudioSource({"id": "c"})
    track = CameraAudioTrack(source)
    encoder, decoder = OpusEncoder(), OpusDecoder()
    tone = (np.sin(np.arange(960) * 2 * np.pi * 1000 / 48000) * 12000).astype("<i2")

    def publish(sequence):
        source.chunks.append((sequence, time.monotonic(), tone.tobytes()))
        source.changed.set()
        source.changed = asyncio.Event()

    async def produce():
        for batch in range(3):
            await asyncio.sleep(0.2)
            for sequence in range(2 + batch * 10, 12 + batch * 10):
                publish(sequence)

    async def roundtrip():
        timestamps, decoded = [], []
        for _ in range(31):
            frame = await track.recv()
            payloads, timestamp = encoder.encode(frame)
            for payload in payloads:
                timestamps.append(timestamp)
                decoded.extend(decoder.decode(JitterFrame(payload, timestamp)))
        return timestamps, decoded

    publish(1)
    try:
        _, (timestamps, decoded) = await asyncio.wait_for(
            asyncio.gather(produce(), roundtrip()), 3
        )
        assert len(timestamps) == 31
        assert np.all(np.diff(timestamps) == 960)
        assert all(frame.sample_rate == 48000 and frame.samples == 960 for frame in decoded)
        # Opus has a short codec look-ahead at startup. Inspect steady audio,
        # using one channel from its interleaved stereo PCM output.
        samples = np.concatenate([
            frame.to_ndarray().reshape(-1, len(frame.layout.channels))[:, 0]
            for frame in decoded[2:]
        ]).astype(np.float64)
        assert samples.size == 29 * 960
        rms = np.sqrt(np.mean(samples.reshape(-1, 960) ** 2, axis=1))
        assert np.min(rms) > 5000  # No artificial silent 20 ms frames.
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        frequencies = np.fft.rfftfreq(len(samples), 1 / 48000)
        assert abs(frequencies[np.argmax(spectrum)] - 1000) < 5
        assert np.max(rms) / np.min(rms) < 1.25
    finally:
        track.stop()


async def test_webrtc_audio_catches_up_after_a_scheduler_pause(monkeypatch):
    from aix5_ai import audio

    now = [100.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
    source = AudioSource({"id": "c"})
    publish_pcm(source, 1, now[0])
    track = CameraAudioTrack(source)
    try:
        first = await track.recv()
        # The receiver cannot run for 300 ms, but the bounded ring still holds
        # all of the source audio. Its content and duration must be preserved.
        now[0] += 0.3
        for sequence in range(2, 17):
            publish_pcm(source, sequence, now[0])
        resumed = await track.recv()
        assert int(resumed.to_ndarray()[0, 0]) == 2
        rest = [await track.recv() for _ in range(14)]
        frames = [resumed, *rest]
        assert [int(frame.to_ndarray()[0, 0]) for frame in frames] == list(range(2, 17))
        assert [frame.pts - first.pts for frame in frames] == list(range(960, 15360, 960))
    finally:
        track.stop()


async def test_webrtc_audio_ring_overrun_preserves_remaining_content_and_gap(monkeypatch):
    from aix5_ai import audio
    from aiortc.codecs.opus import OpusDecoder, OpusEncoder
    from aiortc.jitterbuffer import JitterFrame

    now = [100.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
    source = AudioSource({"id": "c"})
    publish_pcm(source, 1, now[0])
    track = CameraAudioTrack(source)
    try:
        first = await track.recv()
        now[0] += 0.8
        for sequence in range(2, 42):
            publish_pcm(source, sequence, now[0])
        resumed = await track.recv()
        assert int(resumed.to_ndarray()[0, 0]) == 22
        assert resumed.pts - first.pts == 21 * 960
        frames = [resumed, *[await track.recv() for _ in range(19)]]
        assert [int(frame.to_ndarray()[0, 0]) for frame in frames] == list(range(22, 42))
        assert frames[-1].pts - first.pts == 40 * 960
        assert len(source.chunks) == 20
        # RTP timestamps must retain the actual dropped interval through the
        # real Opus encoder; the decoder must accept audio after that gap.
        encoder, decoder = OpusEncoder(), OpusDecoder()
        timestamps = []
        for frame in [first, *frames]:
            payloads, timestamp = encoder.encode(frame)
            assert payloads
            timestamps.append(timestamp)
            for payload in payloads:
                assert decoder.decode(JitterFrame(payload, timestamp))
        # libopus retains a 312-sample look-ahead from the preceding frame.
        # The first packet spanning a discontinuity can therefore keep the
        # preceding frame's timestamp. By the next packet the full source
        # interval must be restored, with no accumulated audio/video drift.
        assert np.all(np.diff(timestamps) > 0)
        assert timestamps[2] - timestamps[0] == 22 * 960
        assert timestamps[-1] - timestamps[0] == 40 * 960
        assert np.all(np.diff(timestamps[2:]) == 960)
    finally:
        track.stop()


async def test_webrtc_audio_discards_stale_pcm_and_recovers_after_input_loss(monkeypatch):
    from aix5_ai import audio

    now = [100.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0]))
    source = AudioSource({"id": "c"})
    publish_pcm(source, 1, now[0])
    track = CameraAudioTrack(source)
    try:
        first = await track.recv()
        publish_pcm(source, 2, now[0])
        now[0] += 1.1
        silence = await track.recv()
        assert not np.any(silence.to_ndarray())
        assert silence.pts - first.pts >= 48000
        # The decoder has reconnected; never replay the stale queued chunk.
        publish_pcm(source, 3, now[0])
        resumed = await track.recv()
        assert int(resumed.to_ndarray()[0, 0]) == 3
        assert resumed.pts >= silence.pts + 960
        publish_pcm(source, 4, now[0])
        assert (await track.recv()).pts == resumed.pts + 960
    finally:
        track.stop()


async def test_webrtc_audio_empty_source_wait_is_cancellable_and_stop_wakes_it():
    from aiortc.mediastreams import MediaStreamError

    source = AudioSource({"id": "c"})
    track = CameraAudioTrack(source)
    waiting = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    waiting = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    track.stop()
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(waiting, 0.1)


async def test_webrtc_audio_input_loss_generates_paced_silence():
    source = AudioSource({"id": "c"})
    track = CameraAudioTrack(source)
    track.idle_timeout = 0.03
    try:
        first = await track.recv()
        started = time.monotonic()
        frames = [await track.recv() for _ in range(3)]
        assert time.monotonic() - started >= 0.035
        assert all(not np.any(frame.to_ndarray()) for frame in frames)
        assert all(frame.pts > first.pts for frame in frames)
        assert frames[-1].pts - frames[0].pts >= 2 * 960
    finally:
        track.stop()


async def test_opus_encoding_is_shared_and_batched_despite_100ms_worker_delays(monkeypatch):
    source = AudioSource({"id": "c"})
    tracks = [EncodedCameraAudioTrack(source) for _ in range(2)]
    publish_pcm(source, 1)
    batches = []
    encode_batch = _OpusPipeline.encode_batch

    def delayed_encode(encoder, frames):
        time.sleep(0.1)
        batches.append(len(frames))
        return encode_batch(encoder, frames)

    monkeypatch.setattr(_OpusPipeline, "encode_batch", staticmethod(delayed_encode))

    async def produce():
        for sequence in range(2, 32):
            await asyncio.sleep(0.02)
            publish_pcm(source, sequence)

    async def consume(track):
        return [await track.recv() for _ in range(31)]

    pipeline = None
    try:
        _, first, second = await asyncio.wait_for(
            asyncio.gather(produce(), *(consume(track) for track in tracks)), 3
        )
        pipeline = source.opus_pipeline
        assert pipeline is not None
        assert tracks[0].pipeline is tracks[1].pipeline
        assert [bytes(packet) for packet in first] == [bytes(packet) for packet in second]
        assert [packet.pts for packet in first] == list(range(0, 31 * 960, 960))
        assert [packet.pts for packet in second] == [packet.pts for packet in first]
        assert sum(batches) == 31  # Two viewers share one encode per source frame.
        assert len(batches) < 15  # Executor scheduling overhead is paid per batch.
    finally:
        for track in tracks:
            track.stop()
        if pipeline:
            await asyncio.wait_for(asyncio.gather(pipeline.task, return_exceptions=True), 1)
    assert source.opus_pipeline is None
    assert not pipeline.subscribers
    assert pipeline.task.done()


async def test_slow_opus_viewer_has_bounded_queue_without_stalling_other_viewers():
    source = AudioSource({"id": "c"})
    fast, slow = EncodedCameraAudioTrack(source), EncodedCameraAudioTrack(source)
    publish_pcm(source, 1)
    pipeline = None
    try:
        await asyncio.gather(fast.recv(), slow.recv())
        pipeline = source.opus_pipeline

        async def produce():
            for sequence in range(2, 42):
                publish_pcm(source, sequence)
                await asyncio.sleep(0.005)

        async def consume():
            return [await fast.recv() for _ in range(40)]

        _, packets = await asyncio.wait_for(asyncio.gather(produce(), consume()), 2)
        assert [packet.pts for packet in packets] == list(range(960, 41 * 960, 960))
        assert slow.queue.qsize() == slow.queue.maxsize == 20
        first_retained = await slow.recv()
        assert first_retained.pts == packets[-20].pts
        assert bytes(first_retained) == bytes(packets[-20])
        slow.stop()
        assert source.opus_pipeline is pipeline and not pipeline.task.done()
    finally:
        fast.stop()
        slow.stop()
        await source.stop()
        if pipeline is not None:
            await asyncio.gather(pipeline.task, return_exceptions=True)


async def test_opus_source_stop_closes_waiting_viewers_and_encoder():
    from aiortc.mediastreams import MediaStreamError

    source = AudioSource({"id": "c"})
    track = EncodedCameraAudioTrack(source)
    waiting = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    pipeline = source.opus_pipeline
    assert pipeline is not None
    await source.stop()
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(waiting, 0.1)
    assert track.readyState == "ended"
    assert pipeline.task.done()
    assert not pipeline.subscribers
    assert source.opus_pipeline is None


async def test_opus_last_viewer_stop_then_source_stop_joins_encoding_thread(monkeypatch):
    import threading
    from aiortc.mediastreams import MediaStreamError

    source = AudioSource({"id": "c"})
    track = EncodedCameraAudioTrack(source)
    publish_pcm(source, 1)
    started, release = threading.Event(), threading.Event()
    encode_batch = _OpusPipeline.encode_batch

    def blocked_encode(encoder, frames):
        started.set()
        assert release.wait(2)
        return encode_batch(encoder, frames)

    monkeypatch.setattr(_OpusPipeline, "encode_batch", staticmethod(blocked_encode))
    receiving = asyncio.create_task(track.recv())
    closing = None
    try:
        async def wait_for_worker():
            while not started.is_set():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_worker(), 1)
        pipeline = source.opus_pipeline
        track.stop()
        await asyncio.sleep(0)  # Let cancellation start awaiting codec cleanup.
        closing = asyncio.create_task(source.stop())
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert not pipeline.task.done()
        release.set()
        await asyncio.wait_for(closing, 1)
        with pytest.raises(MediaStreamError):
            await receiving
        assert pipeline.task.done()
        assert not source._opus_tasks
    finally:
        release.set()
        track.stop()
        await asyncio.gather(receiving, *([closing] if closing else []), return_exceptions=True)


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


@pytest.mark.parametrize("audio_codec", ["opus", "PCMU"])
async def test_webRTC_negotiates_audio_and_receives_it(monkeypatch, audio_codec):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
    from aiortc.sdp import SessionDescription
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
    audio_transceiver = receiver.addTransceiver("audio", direction="recvonly")
    audio_transceiver.setCodecPreferences([
        codec for codec in RTCRtpSender.getCapabilities("audio").codecs
        if codec.name == audio_codec
    ])
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
        negotiated = next(
            media for media in SessionDescription.parse(answer["sdp"]).media
            if media.kind == "audio"
        )
        assert [codec.name for codec in negotiated.rtp.codecs] == [audio_codec]
        server = next(pc for pc, cid in webrtc.peer_camera.items() if cid == "c")
        audio_track = next(sender.track for sender in server.getSenders() if sender.kind == "audio")
        assert isinstance(
            audio_track,
            EncodedCameraAudioTrack if audio_codec == "opus" else CameraAudioTrack,
        )
        await receiver.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )
        frame = await asyncio.wait_for(received, 5)
        assert frame.sample_rate == (48000 if audio_codec == "opus" else 8000)
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
