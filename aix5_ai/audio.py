"""Probe RTSP audio and decode to bounded PCM for meters / WebRTC Opus.

Source codec is retained in metadata. AAC is the preferred future RTSP/ONVIF
output for HA stream compatibility; Opus is only the browser transport.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import json
import math
import time
from fractions import Fraction

import numpy as np
from aiortc import AudioStreamTrack
from aiortc.codecs.opus import OpusEncoder
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame, Packet

from .media_scheduling import ProcessReadWatchdog, terminate_process

RATE = 48000
SAMPLES = 960  # 20 ms, mono


def parse_rate(value):
    try:
        rate = float(Fraction(value))
        return rate if 0 < rate <= 120 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def audio_level(pcm):
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0
    db = max(-60.0, min(0.0, 20 * math.log10(max(rms, 1e-9))))
    return round(db, 1), round((db + 60) / 60 * 100, 1)


class AudioSource:
    def __init__(self, camera):
        self.camera = camera
        self.task = None
        self.proc = None
        self.chunks = collections.deque(maxlen=20)
        self.sequence = 0
        self.changed = asyncio.Event()
        self.started = None
        self.rate_samples = 0
        self.opus_pipeline = None
        self._opus_tasks = set()

    async def start(self):
        if self.task is None or self.task.done():
            self.camera.update(audio_status="probing", audio_level=0, audio_dbfs=-60)
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        pipeline, self.opus_pipeline = self.opus_pipeline, None
        if pipeline is not None:
            await pipeline.close()
        # A final viewer may already have detached a pipeline whose codec
        # thread is still finishing its batch. Join that cleanup as well.
        if self._opus_tasks:
            await asyncio.gather(*tuple(self._opus_tasks), return_exceptions=True)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.chunks.clear()
        self.camera.update(audio_level=0, audio_dbfs=-60)
        self.camera["audio_status"] = "disabled"

    async def close_process(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            await terminate_process(proc)

    async def probe(self):
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-rtsp_transport",
            self.camera.get("transport", "tcp"),
            "-stimeout",
            "5000000",
            "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels,r_frame_rate,avg_frame_rate",
            "-of",
            "json",
            self.camera["rtsp_url"],
        ]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await asyncio.wait_for(self.proc.communicate(), 12)
            if self.proc.returncode:
                raise RuntimeError("媒体信息探测失败")
            streams = json.loads(out).get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            # r_frame_rate is sometimes a camera's nominal 10 while the actual
            # average is 20. Native capture also measures delivered frame rate.
            self.camera["source_fps"] = (
                parse_rate(video.get("avg_frame_rate"))
                or parse_rate(video.get("r_frame_rate"))
                or 20
            )
            audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
            self.camera.update(
                audio_present=audio is not None,
                audio_codec=audio.get("codec_name") if audio else None,
                audio_sample_rate=int(audio.get("sample_rate") or 0) if audio else None,
                audio_channels=audio.get("channels") if audio else None,
                audio_browser_codec="opus",
                audio_ha_codec="aac",
            )
            return audio
        finally:
            await self.close_process()

    async def decode(self):
        # FFmpeg handles AAC, G.711 A/u-law, G.726, PCM, MP3, Opus, etc.
        # Audio is CPU decoded/resampled; the hardware-only policy concerns
        # the video VPU pipeline, not the browser's required audio conversion.
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-rtsp_transport",
            self.camera.get("transport", "tcp"),
            "-stimeout",
            "5000000",
            "-i",
            self.camera["rtsp_url"],
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-acodec",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self.started = time.monotonic()
        self.rate_samples = 0
        self.camera["audio_decode_starts"] = (
            self.camera.get("audio_decode_starts", 0) + 1
        )
        self.camera["audio_status"] = "running"
        self.camera["audio_error"] = None
        watchdog = ProcessReadWatchdog(self.proc, 8)
        try:
            while True:
                pcm = await self.proc.stdout.readexactly(SAMPLES * 2)
                watchdog.reset(8)
                self.sequence += 1
                self.camera["audio_decoded_chunks"] = self.sequence
                self.camera["audio_decoded_samples"] = self.sequence * SAMPLES
                now = time.monotonic()
                self.chunks.append((self.sequence, now, pcm))
                db, level = audio_level(pcm)
                self.camera.update(
                    audio_dbfs=db,
                    audio_level=level,
                    audio_last_at=datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                )
                self.changed.set()
                self.changed = asyncio.Event()
                if self.sequence % 8 == 0:
                    # Let PCM consumers drain their 20-chunk ring before a
                    # buffered FFmpeg burst can overwrite unread audio.
                    await asyncio.sleep(0)
        finally:
            await watchdog.close()
            if watchdog.expired:
                self.camera["audio_read_timeouts"] = (
                    self.camera.get("audio_read_timeouts", 0) + 1
                )
            await self.close_process()

    async def run(self):
        try:
            while True:
                try:
                    audio = await self.probe()
                    if not audio:
                        self.camera.update(
                            audio_status="absent", audio_level=0, audio_dbfs=-60
                        )
                        # Re-probe if the camera is reconfigured to enable audio.
                        await asyncio.sleep(60)
                        continue
                    await self.decode()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.camera.update(
                        audio_status="error",
                        audio_error="音频流读取失败，正在重试",
                        audio_level=0,
                        audio_dbfs=-60,
                    )
                    await asyncio.sleep(5)
        finally:
            await self.close_process()


class CameraAudioTrack(AudioStreamTrack):
    # RTSP and FFmpeg deliver PCM in bursts. Waiting across normal bursts lets
    # the receiver's jitter buffer play the complete source sample sequence.
    # Only a real input interruption should introduce locally generated silence.
    idle_timeout = 0.5
    stale_timeout = 1.0

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.last_sequence = None
        self.pts = 0
        self.epoch = None
        self.origin_sequence = None
        self.origin_pts = 0
        self._resync = False
        self._stopped = asyncio.Event()

    def stop(self):
        super().stop()
        self._stopped.set()

    async def _wait_for_input(self, changed, timeout):
        # wait() preserves cancellation on the board's Python 3.10 even when
        # input arrives in the same event-loop turn as a peer is being closed.
        waiting = {
            asyncio.create_task(changed.wait()),
            asyncio.create_task(self._stopped.wait()),
        }
        try:
            await asyncio.wait(
                waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in waiting:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)

    async def recv(self):
        if self.epoch is None:
            self.epoch = time.monotonic()
        while self.readyState == "live":
            # Capture the wake-up event before inspecting PCM to avoid losing
            # a notification between checking an empty ring and waiting.
            changed = self.source.changed
            chunks = self.source.chunks
            now = time.monotonic()
            candidate = None
            if chunks and now - chunks[-1][1] < self.stale_timeout:
                if self.last_sequence is None:
                    # A new viewer starts live; once started, consume every
                    # retained chunk, including an entire 200-400 ms burst.
                    candidate = chunks[-1]
                else:
                    candidate = next(
                        (
                            item
                            for item in chunks
                            if item[0] > self.last_sequence
                            and now - item[1] < self.stale_timeout
                        ),
                        None,
                    )
            if candidate:
                sequence = candidate[0]
                if self.origin_sequence is None or self._resync:
                    self.origin_sequence = sequence
                    self.origin_pts = max(
                        self.pts, int((now - self.epoch) * RATE) // SAMPLES * SAMPLES
                    )
                    self._resync = False
                # Sequence gaps only occur when the bounded source ring was
                # actually overwritten. Preserve their duration in RTP time.
                self.pts = self.origin_pts + (
                    sequence - self.origin_sequence
                ) * SAMPLES
                self.last_sequence = sequence
                pcm = candidate[2]
                break
            last_input = chunks[-1][1] if chunks else self.epoch
            deadline = max(
                last_input + self.idle_timeout, self.epoch + self.pts / RATE
            )
            if now < deadline:
                await self._wait_for_input(changed, deadline - now)
                continue
            # Keep an unavailable/disabled source alive with paced silence, but
            # never spend ordinary packetization gaps inserting fake samples.
            self.pts = max(
                self.pts, int((now - self.epoch) * RATE) // SAMPLES * SAMPLES
            )
            self._resync = True
            pcm = bytes(SAMPLES * 2)
            break
        else:
            raise MediaStreamError
        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate = RATE
        frame.time_base = Fraction(1, RATE)
        frame.pts = self.pts
        self.pts += SAMPLES
        return frame


class _OpusPipeline:
    """Encode a camera's PCM once, in batches, for all browser viewers."""

    def __init__(self, source):
        self.source = source
        self.subscribers = set()
        self.task = None
        self._stopping = False

    def subscribe(self, track):
        self.subscribers.add(track)
        if self.task is None:
            self.task = asyncio.create_task(self.run())
            self.source._opus_tasks.add(self.task)
            self.task.add_done_callback(self.source._opus_tasks.discard)

    def request_stop(self):
        if not self._stopping:
            self._stopping = True
            if self.task and not self.task.done():
                self.task.cancel()

    def unsubscribe(self, track):
        self.subscribers.discard(track)
        if not self.subscribers:
            if self.source.opus_pipeline is self:
                self.source.opus_pipeline = None
            self.request_stop()

    async def close(self):
        for track in tuple(self.subscribers):
            track.stop()
        if self.task:
            self.request_stop()
            await asyncio.gather(self.task, return_exceptions=True)

    @staticmethod
    def encode_batch(encoder, frames):
        packets = []
        for frame in frames:
            payloads, timestamp = encoder.encode(frame)
            packets.extend(
                (payload, timestamp + index * SAMPLES)
                for index, payload in enumerate(payloads)
            )
        return packets

    async def run(self):
        cursor = CameraAudioTrack(self.source)
        encoder = OpusEncoder()
        try:
            while self.subscribers:
                frames = [await cursor.recv()]
                # A single executor round-trip per available batch avoids a
                # video-loaded event loop delaying every individual audio frame.
                # The source ring already bounds a batch to 400 ms of PCM.
                while (
                    len(frames) < 20
                    and self.source.chunks
                    and cursor.last_sequence is not None
                    and self.source.chunks[-1][0] > cursor.last_sequence
                    and time.monotonic() - self.source.chunks[-1][1]
                    < cursor.stale_timeout
                ):
                    frames.append(await cursor.recv())
                encoding = asyncio.create_task(
                    asyncio.to_thread(self.encode_batch, encoder, frames)
                )
                try:
                    packets = await asyncio.shield(encoding)
                except asyncio.CancelledError:
                    # The codec belongs to this worker until the batch exits.
                    # Never free it while its executor thread is still running.
                    await encoding
                    raise
                for packet in packets:
                    for track in tuple(self.subscribers):
                        track.enqueue(packet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for track in tuple(self.subscribers):
                track.enqueue(exc)
        finally:
            cursor.stop()
            if self.source.opus_pipeline is self:
                self.source.opus_pipeline = None


class EncodedCameraAudioTrack(AudioStreamTrack):
    """A bounded per-viewer Opus packet cursor; the camera owns encoding."""

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.pipeline = None
        self.queue = asyncio.Queue(maxsize=20)
        self.first_pts = None

    def enqueue(self, packet):
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(packet)

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self.pipeline is None:
            pipeline = self.source.opus_pipeline
            if pipeline is None or (pipeline.task and pipeline.task.done()):
                pipeline = self.source.opus_pipeline = _OpusPipeline(self.source)
            self.pipeline = pipeline
            pipeline.subscribe(self)
        data = await self.queue.get()
        if isinstance(data, Exception):
            self.stop()
            raise data
        payload, pts = data
        if self.first_pts is None:
            self.first_pts = pts
        packet = Packet(payload)
        packet.pts = packet.dts = pts - self.first_pts
        packet.time_base = Fraction(1, RATE)
        return packet

    def stop(self):
        super().stop()
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            pipeline.unsubscribe(self)
        # Wake a sender awaiting input even when the source is disconnected.
        while not self.queue.empty():
            self.queue.get_nowait()
        self.enqueue(MediaStreamError())
