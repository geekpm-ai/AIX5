from __future__ import annotations

import asyncio
import datetime
import logging
import mmap
import os
import re
import struct
import time

import cv2
import numpy as np

from .config import CAPTURE_BIN, LEGACY_CAPTURE_BIN
from .audio import AudioSource

log = logging.getLogger("aix5.hw_capture")
HEADER = 64
FIRST_FRAME_TIMEOUT = 8
FRAME_TIMEOUT = 5
cv2.setNumThreads(1)
software_decode_slots = asyncio.Semaphore(1)


class SoftwareDecodeBusy(RuntimeError):
    pass


def safe_error(exc):
    # FFmpeg diagnostics can contain the full authenticated input URL.
    return re.sub(r"(?i)(rtsps?://)[^\s/]*@", r"\1***@", str(exc))


def make_web_preview(bgr, jpg=None, max_width=960):
    if bgr.shape[1] <= max_width and jpg is not None:
        return jpg
    if bgr.shape[1] > max_width:
        bgr = cv2.resize(
            bgr,
            (max_width, max(2, int(bgr.shape[0] * max_width / bgr.shape[1]))),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    return encoded.tobytes() if ok else None


class HardwareCapture:
    def __init__(self, camera, on_frame, on_status=None, allow_software=None):
        self.allow_software = allow_software or (lambda: False)
        self.camera = camera
        self.on_frame = on_frame
        self.on_status = on_status
        self.stop_event = asyncio.Event()
        self.proc = None
        self.path = f"/dev/shm/aix5-{camera['id']}"
        self.task = None
        self._lifecycle_lock = asyncio.Lock()

    def set_status(self, status):
        changed = self.camera.get("status") != status
        self.camera["status"] = status
        if changed and self.on_status:
            self.on_status(self.camera)

    async def start(self):
        async with self._lifecycle_lock:
            if self.task and not self.task.done():
                return
            self.stop_event.clear()
            if self.camera.get("enabled", True):
                self.set_status("connecting")
                self.task = asyncio.create_task(self.run())
            else:
                self.set_status("disabled")

    async def _stop(self):
        self.stop_event.set()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        await self._close_process()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.set_status(
            "disabled" if not self.camera.get("enabled", True) else "disconnected"
        )

    async def stop(self):
        async with self._lifecycle_lock:
            await self._stop()

    async def reconnect(self):
        async with self._lifecycle_lock:
            await self._stop()
            # A changed stream or manual retry should try the VPU again.
            self.camera.pop("force_software", None)
            self.camera.pop("software_cooldown_until", None)
            self.stop_event.clear()
            if self.camera.get("enabled", True):
                self.set_status("connecting")
                self.task = asyncio.create_task(self.run())

    async def _close_process(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def run(self):
        backoff = 1
        software_failures = 0
        try:
            while not self.stop_event.is_set() and self.camera.get("enabled", True):
                try:
                    self.set_status("connecting")
                    self.camera["_read_had_frame"] = False
                    await self.read_once()
                    if not self.stop_event.is_set():
                        raise RuntimeError("采集进程退出，准备重连")
                except SoftwareDecodeBusy:
                    self.set_status("paused")
                    await asyncio.sleep(30)
                except Exception as exc:
                    self.set_status("disconnected")
                    hardware_failed = not self.camera.get("force_software") and (
                        "首帧" in str(exc)
                        or "共享内存未创建" in str(exc)
                        or isinstance(exc, FileNotFoundError)
                    )
                    if hardware_failed and not self.allow_software():
                        self.camera["last_error"] = (
                            "硬件解码不可用，软件回退未开启：" + safe_error(exc)
                        )
                        self.set_status("error")
                        await asyncio.sleep(backoff)
                        backoff = min(60, backoff * 2)
                        continue
                    if hardware_failed and self.allow_software():
                        self.camera["force_software"] = True
                        self.camera["decode_backend"] = "software"
                        self.camera["last_error"] = "硬件解码未输出首帧，已切换软件解码"
                        log.warning(
                            "camera=%s no first frame; falling back to software",
                            self.camera["id"],
                        )
                        backoff = 1
                        continue
                    had_frame = self.camera.pop("_read_had_frame", False)
                    if had_frame:
                        backoff = 1
                        software_failures = 0
                    if self.camera.get("decode_backend") == "software":
                        software_failures += 1
                        if software_failures >= 3:
                            self.camera["last_error"] = "软件解码连续失败，30 秒后重试"
                            await asyncio.sleep(30)
                            software_failures = 0
                            continue
                    self.camera["last_error"] = safe_error(exc)
                    log.warning(
                        "camera=%s capture error=%s", self.camera["id"], safe_error(exc)
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(60, backoff * 2)
        finally:
            await self._close_process()

    async def read_once(self):
        if self.camera.get("force_software"):
            if not self.allow_software():
                raise RuntimeError("软件回退未开启")
            await self.read_software()
            return
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        env = os.environ.copy()
        env["AIX5_RTSP_URL"] = self.camera["rtsp_url"]
        env["AIX5_RTSP_TRANSPORT"] = self.camera.get("transport", "tcp")
        capture_bin = (
            LEGACY_CAPTURE_BIN
            if "/stream/main" in self.camera["rtsp_url"]
            else CAPTURE_BIN
        )
        mm = None
        try:
            self.proc = await asyncio.create_subprocess_exec(
                capture_bin,
                self.path,
                "0",
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 20
            # The file may be visible before ftruncate and header initialization.
            while self.proc.returncode is None and time.monotonic() < deadline:
                if os.path.exists(self.path) and os.stat(self.path).st_size >= HEADER:
                    break
                await asyncio.sleep(0.05)
            if not os.path.exists(self.path) or os.stat(self.path).st_size < HEADER:
                raise RuntimeError("硬件解码共享内存未创建")
            fd = os.open(self.path, os.O_RDONLY)
            try:
                mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
                self.camera["_native_fd"] = os.dup(fd)
            finally:
                os.close(fd)
            last_seq = 0
            got_frame = False
            rate_start = time.monotonic()
            rate_frames = None
            last_frame = time.monotonic()
            next_python_frame = 0.0
            while not self.stop_event.is_set():
                seq, w, h, ts, frames, codec, data_size = struct.unpack_from(
                    "<QIIQQII", mm, 0
                )
                slot = struct.unpack_from("<I", mm, 40)[0]
                slot_count = struct.unpack_from("<I", mm, 44)[0] or 2
                if seq and seq % 2 == 0 and seq != last_seq:
                    if not (
                        w >= 16
                        and h >= 16
                        and w % 2 == 0
                        and h % 2 == 0
                        and data_size == w * h * 3 // 2
                        and 2 <= slot_count <= 8
                        and slot < slot_count
                        and HEADER + slot_count * data_size <= len(mm)
                    ):
                        raise RuntimeError("硬件解码共享内存帧尺寸无效")
                    offset = HEADER + slot * data_size
                    now = time.monotonic()
                    portable = self.camera.get("_full_rate_frame_consumers", 0) > 0
                    copy_frame = portable or now >= next_python_frame
                    # Native video encoders read this mmap themselves. Python
                    # needs full frames only for analysis/JPEG, unless a portable
                    # CameraTrack explicitly requests original-rate frames.
                    payload = (
                        bytes(mm[offset : offset + data_size]) if copy_frame else None
                    )
                    if (
                        struct.unpack_from("<Q", mm, 0)[0] == seq
                        and struct.unpack_from("<I", mm, 40)[0] == slot
                    ):
                        last_seq = seq
                        last_frame = time.monotonic()
                        got_frame = True
                        if rate_frames is None:
                            rate_frames = frames
                        elapsed = time.monotonic() - rate_start
                        if elapsed >= 2:
                            # The native counter includes frames published between
                            # Python polls; polling rate is not decoder FPS.
                            self.camera["decode_fps"] = round(
                                (frames - rate_frames) / elapsed, 2
                            )
                            rate_start, rate_frames = time.monotonic(), frames
                        self.camera.update(
                            width=w,
                            height=h,
                            codec=codec,
                            decode_backend="hardware",
                            last_error=None,
                            _read_had_frame=True,
                            last_frame_at=datetime.datetime.now(
                                datetime.timezone.utc
                            ).isoformat(),
                            decode_frames=frames,
                        )
                        self.set_status("running")
                        if payload is not None:
                            rate = max(2.0, float(self.camera.get("analysis_fps") or 5))
                            # Keep the sampling cadence anchored; adding an
                            # interval to each late poll would drift below 5 FPS.
                            next_python_frame = (
                                max(next_python_frame + 1 / rate, now)
                                if next_python_frame and not portable
                                else now + 1 / rate
                            )
                            self.camera["_capture_analysis_frame"] = not portable
                            self.camera["python_frame_copies"] = (
                                self.camera.get("python_frame_copies", 0) + 1
                            )
                            nv12 = np.frombuffer(payload, np.uint8).reshape(
                                (h * 3 // 2, w)
                            )
                            await self.on_frame(self.camera, None, None, nv12=nv12)
                if self.proc.returncode is not None:
                    raise RuntimeError(
                        "硬件解码进程退出" if got_frame else "硬件解码未收到首帧"
                    )
                timeout = FRAME_TIMEOUT if got_frame else FIRST_FRAME_TIMEOUT
                if time.monotonic() - last_frame > timeout:
                    raise RuntimeError(
                        "硬件解码视频停帧超时" if got_frame else "硬件解码未收到首帧"
                    )
                await asyncio.sleep(0.01)
        finally:
            if mm is not None:
                mm.close()
            native_fd = self.camera.pop("_native_fd", None)
            if native_fd is not None:
                os.close(native_fd)
            await self._close_process()

    async def read_software(self):
        self.camera["decode_backend"] = "software"
        try:
            await asyncio.wait_for(software_decode_slots.acquire(), timeout=0.25)
        except asyncio.TimeoutError as exc:
            self.camera["last_error"] = "软件解码资源已被其他通道占用，30 秒后重试"
            raise SoftwareDecodeBusy() from exc
        stderr_task = None
        stderr_tail = []
        try:
            fps = float(self.camera.get("source_fps") or 20)
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-rtsp_transport",
                self.camera["transport"],
                "-stimeout",
                "8000000",
                "-analyzeduration",
                "1000000",
                "-probesize",
                "1000000",
                "-reorder_queue_size",
                "0",
                "-fflags",
                "+discardcorrupt+genpts",
                "-err_detect",
                "ignore_err",
                "-i",
                self.camera["rtsp_url"],
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                f"fps={fps},scale=960:-2",
                "-f",
                "mjpeg",
                "pipe:1",
            ]
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            proc = self.proc

            async def drain_stderr():
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    stderr_tail.append(
                        safe_error(line.decode("utf-8", "replace").strip())[-300:]
                    )
                    del stderr_tail[:-12]

            stderr_task = asyncio.create_task(drain_stderr())
            buf = b""
            last_frame = time.monotonic()
            while not self.stop_event.is_set():
                remaining = FIRST_FRAME_TIMEOUT - (time.monotonic() - last_frame)
                if remaining <= 0:
                    raise RuntimeError("软件解码 8 秒未输出有效帧")
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=remaining
                    )
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("软件解码 8 秒未输出有效帧") from exc
                if not chunk:
                    raise RuntimeError(
                        "软件解码流结束：" + " | ".join(stderr_tail[-3:])
                    )
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf = buf[-1:]
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        buf = buf[start:]
                        break
                    jpg, buf = buf[start : end + 2], buf[end + 2 :]
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    last_frame = time.monotonic()
                    self.camera.update(
                        _read_had_frame=True,
                        last_error=None,
                        last_frame_at=datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        width=frame.shape[1],
                        height=frame.shape[0],
                        codec=1,
                        decode_frames=self.camera.get("decode_frames", 0) + 1,
                    )
                    self.set_status("running")
                    await self.on_frame(self.camera, jpg, frame)
                if len(buf) > 16 * 1024 * 1024:
                    raise RuntimeError("软件解码 JPEG 数据超过大小限制")
        finally:
            try:
                if stderr_task:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                await self._close_process()
            finally:
                software_decode_slots.release()


class HardwareManager:
    def __init__(self):
        self.preview_resolution = "960x540"
        self.allow_software_fallback = False
        self.audio_sources = {}
        self.native_images = {}
        self.video_sequences = {}
        self.video_changed = {}
        self.analysis_latest = {}
        self.analysis_tasks = {}
        self.cameras = {}
        self.workers = {}
        self.frames = {}
        self.raw_frames = {}
        self.images = {}
        self.frame_queues = {}
        self.preview_latest = {}
        self.preview_tasks = {}
        self.preview_next = {}
        self.bgr_next = {}
        self.generations = {}
        self.status_listener = None

    def clear_frames(self, cid):
        self.generations[cid] = self.generations.get(cid, 0) + 1
        for mapping in (
            self.frames,
            self.raw_frames,
            self.images,
            self.preview_latest,
            self.preview_next,
            self.bgr_next,
            self.native_images,
            self.analysis_latest,
        ):
            mapping.pop(cid, None)
        q = self.frame_queues.get(cid)
        if q:
            while not q.empty():
                q.get_nowait()
        cam = self.cameras.get(cid)
        if cam:
            cam.update(
                detection_count=0,
                present={},
                detection_payload={"camera_id": cid, "detections": []},
            )
            cam.pop("last_inference", None)

    def _on_status(self, camera):
        if camera["status"] != "running":
            self.clear_frames(camera["id"])
        if self.status_listener:
            self.status_listener(camera)

    async def add(self, camera):
        cid = camera["id"]
        self.cameras[cid] = camera
        self.frame_queues[cid] = asyncio.Queue(maxsize=1)
        self.generations[cid] = 0
        self.video_sequences[cid] = 0
        self.video_changed[cid] = asyncio.Event()
        source = self.audio_sources[cid] = AudioSource(camera)
        if camera.get("enabled", True):
            await source.start()
        worker = HardwareCapture(
            camera, self.on_frame, self._on_status, lambda: self.allow_software_fallback
        )
        self.workers[cid] = worker
        await worker.start()

    async def remove(self, cid):
        source = self.audio_sources.pop(cid, None)
        if source:
            await source.stop()
        worker = self.workers.pop(cid, None)
        if worker:
            await worker.stop()
        analysis_task = self.analysis_tasks.pop(cid, None)
        if analysis_task:
            analysis_task.cancel()
            await asyncio.gather(analysis_task, return_exceptions=True)
        self.analysis_latest.pop(cid, None)
        task = self.preview_tasks.pop(cid, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.clear_frames(cid)
        self.frame_queues.pop(cid, None)
        self.generations.pop(cid, None)
        self.cameras.pop(cid, None)
        self.video_sequences.pop(cid, None)
        self.video_changed.pop(cid, None)

    async def reconfigure_audio(self, cid):
        source = self.audio_sources.get(cid)
        if source:
            await source.stop()
            if self.cameras[cid].get("enabled", True):
                await source.start()

    async def on_frame(self, camera, jpg, bgr, nv12=None):
        analysis_ready = camera.pop("_capture_analysis_frame", False)
        cid = camera["id"]
        if cid not in self.cameras or not camera.get("enabled", True):
            return
        now = time.monotonic()
        # Publish every decoded frame for video, regardless of AI/JPEG cadence.
        self.video_sequences[cid] = self.video_sequences.get(cid, 0) + 1
        if nv12 is not None:
            self.native_images[cid] = nv12
        else:
            self.images[cid] = bgr
        event = self.video_changed.get(cid)
        if event:
            event.set()
        self.video_changed[cid] = asyncio.Event()
        if nv12 is not None:
            # Only inference/snapshots require BGR. Full-rate WebRTC consumes NV12.
            analysis_fps = float(camera.get("analysis_fps", 5))
            if not analysis_ready and now < self.bgr_next.get(cid, 0):
                return
            self.bgr_next[cid] = max(
                self.bgr_next.get(cid, now) + 1 / analysis_fps, now
            )
            self.analysis_latest[cid] = (camera, nv12, self.generations[cid])
            task = self.analysis_tasks.get(cid)
            if task is None or task.done():
                self.analysis_tasks[cid] = asyncio.create_task(
                    self._analysis_worker(cid)
                )
            return
        if bgr is None:
            return
        self._publish_analysis(camera, jpg, bgr, now)

    async def _analysis_worker(self, cid):
        while cid in self.analysis_latest and cid in self.cameras:
            camera, nv12, generation = self.analysis_latest.pop(cid)
            bgr = await asyncio.to_thread(cv2.cvtColor, nv12, cv2.COLOR_YUV2BGR_NV12)
            if cid in self.cameras and generation == self.generations[cid]:
                self._publish_analysis(camera, None, bgr, time.monotonic())

    def _publish_analysis(self, camera, jpg, bgr, now):
        cid = camera["id"]
        self.raw_frames[cid] = jpg
        self.images[cid] = bgr
        if now >= self.preview_next.get(cid, 0):
            # JPEG is the snapshot/fallback path. Keep its encode work bounded
            # independently of WebRTC, especially for three 1080p sources.
            self.preview_next[cid] = now + 1 / min(
                float(camera.get("preview_fps", 5)), 2
            )
            self.preview_latest[cid] = (bgr, jpg, self.generations[cid])
            task = self.preview_tasks.get(cid)
            if task is None or task.done():
                self.preview_tasks[cid] = asyncio.create_task(self._preview_worker(cid))
        q = self.frame_queues[cid]
        if q.full():
            q.get_nowait()
        q.put_nowait((bgr, jpg, self.generations[cid]))

    async def _preview_worker(self, cid):
        while cid in self.preview_latest and cid in self.cameras:
            bgr, jpg, generation = self.preview_latest.pop(cid)
            resolution = self.preview_resolution
            preview = await asyncio.to_thread(
                make_web_preview, bgr, jpg, int(resolution.split("x")[0])
            )
            if resolution != self.preview_resolution:
                continue
            if preview and cid in self.cameras and self.generations[cid] == generation:
                self.frames[cid] = preview
                cam = self.cameras[cid]
                cam["preview_frames"] = cam.get("preview_frames", 0) + 1

    async def close(self):
        for cid in list(self.cameras):
            await self.remove(cid)


manager = HardwareManager()
