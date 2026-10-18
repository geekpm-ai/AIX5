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
from av import AudioFrame

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

    async def start(self):
        if self.task is None or self.task.done():
            self.camera.update(audio_status="probing", audio_level=0, audio_dbfs=-60)
            self.task = asyncio.create_task(self.run())

    async def stop(self):
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
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.last_sequence = 0
        self.pts = 0
        self.next_at = None
        self.epoch = None

    async def recv(self):
        now = time.monotonic()
        if self.epoch is None:
            self.epoch = now
        # Preserve wall-clock audio time after scheduler stalls. Resetting the
        # deadline while adding only 20 ms to PTS makes audio progressively
        # slower than video and the browser drops video to stay synchronized.
        slot = max(self.pts // SAMPLES, int((now - self.epoch) / 0.02))
        self.next_at = self.epoch + slot * 0.02
        if self.next_at > now:
            await asyncio.sleep(self.next_at - now)
        self.pts = slot * SAMPLES
        chunks = self.source.chunks
        # Start near live edge; never play a backlog after a browser stalls.
        if chunks and (
            self.last_sequence < chunks[0][0] or chunks[-1][0] - self.last_sequence > 5
        ):
            self.last_sequence = max(chunks[0][0] - 1, chunks[-1][0] - 2)
        pcm = bytes(SAMPLES * 2)
        if chunks and time.monotonic() - chunks[-1][1] < 1:
            candidate = next(
                (item for item in chunks if item[0] > self.last_sequence), None
            )
            if candidate:
                self.last_sequence = candidate[0]
                pcm = candidate[2]
        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate = RATE
        frame.time_base = Fraction(1, RATE)
        frame.pts = self.pts
        self.pts += SAMPLES
        return frame
