import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from aix5_ai import hw_capture as capture


def camera(**overrides):
    return {
        "id": "c",
        "enabled": True,
        "transport": "tcp",
        "rtsp_url": "rtsp://localhost/test",
        "analysis_fps": 5,
        "preview_fps": 5,
        **overrides,
    }


async def test_reconnect_waits_for_old_task_and_clears_stop_flag(tmp_path):
    worker = capture.HardwareCapture(camera(), AsyncMock())
    worker.path = str(tmp_path / "capture")
    started, exited = asyncio.Event(), asyncio.Event()
    runs = []

    async def read():
        runs.append(worker.stop_event.is_set())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    worker.read_once = read
    await worker.start()
    await started.wait()
    old = worker.task
    await worker.reconnect()
    await asyncio.sleep(0)
    assert old.done() and exited.is_set()
    assert runs == [False, False]
    await worker.stop()
    assert worker.task is None


async def test_disabled_camera_has_no_capture_task(tmp_path):
    worker = capture.HardwareCapture(camera(enabled=False), AsyncMock())
    worker.path = str(tmp_path / "capture")
    worker.read_once = AsyncMock()
    await worker.start()
    assert worker.camera["status"] == "disabled"
    assert worker.task is None
    worker.camera["enabled"] = True
    worker.run = AsyncMock()
    await worker.reconnect()
    await worker.task
    worker.run.assert_awaited_once()
    await worker.stop()


async def test_software_slot_released_if_ffmpeg_cannot_start(monkeypatch):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(capture, "software_decode_slots", semaphore)
    monkeypatch.setattr(
        capture.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("ffmpeg")),
    )
    worker = capture.HardwareCapture(camera(), AsyncMock())
    with pytest.raises(FileNotFoundError):
        await worker.read_software()
    await asyncio.wait_for(semaphore.acquire(), 0.1)
    semaphore.release()


@pytest.mark.parametrize("slot_count,slot", [(2, 1), (8, 6)])
async def test_shared_memory_reader_detects_stalled_stream(
    tmp_path, monkeypatch, slot_count, slot
):
    worker = capture.HardwareCapture(camera(), AsyncMock())
    worker.path = str(tmp_path / "capture")
    proc = SimpleNamespace(returncode=None)

    def terminate():
        proc.returncode = 0

    proc.terminate = terminate
    proc.kill = terminate
    proc.wait = AsyncMock(return_value=0)

    async def spawn(*args, **kwargs):
        width, height = 16, 16
        size = width * height * 3 // 2
        header = bytearray(64)
        struct.pack_into(
            "<QIIQQIIII", header, 0, 2, width, height, 1, 1, 1, size, slot, slot_count
        )
        from pathlib import Path

        slots = bytearray(size * slot_count)
        slots[slot * size : (slot + 1) * size] = bytes([128]) * size
        Path(worker.path).write_bytes(header + slots)
        return proc

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(capture, "FRAME_TIMEOUT", 0.02)
    with pytest.raises(RuntimeError, match="停帧"):
        await worker.read_once()
    worker.on_frame.assert_awaited_once()
    assert worker.on_frame.call_args.kwargs["nv12"].shape == (24, 16)
    assert worker.proc is None
    proc.wait.assert_awaited_once()


async def test_preview_finishing_after_invalidation_cannot_restore_old_frame(
    monkeypatch,
):
    manager = capture.HardwareManager()
    manager.cameras["c"] = camera()
    manager.generations["c"] = 0
    manager.frame_queues["c"] = asyncio.Queue(maxsize=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def encode(*args):
        entered.set()
        await release.wait()
        return b"old-jpeg"

    monkeypatch.setattr(capture.asyncio, "to_thread", encode)
    await manager.on_frame(manager.cameras["c"], None, np.zeros((16, 16, 3), np.uint8))
    await entered.wait()
    manager.clear_frames("c")
    release.set()
    await manager.preview_tasks["c"]
    assert "c" not in manager.frames
    assert manager.frame_queues["c"].empty()


def test_ffmpeg_errors_redact_rtsp_credentials():
    assert "secret" not in capture.safe_error(
        "failed rtsp://admin:secret@192.168.1.2/live"
    )


def test_jpeg_preview_respects_configured_width():
    import cv2

    image = np.zeros((1080, 1920, 3), np.uint8)
    for width, height in [(960, 540), (1920, 1080)]:
        jpeg = capture.make_web_preview(image, max_width=width)
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert (frame.shape[1], frame.shape[0]) == (width, height)


async def test_hardware_failure_cannot_start_software_without_setting(tmp_path):
    worker = capture.HardwareCapture(
        camera(), AsyncMock(), allow_software=lambda: False
    )
    worker.path = str(tmp_path / "shm")
    worker.read_once = AsyncMock(side_effect=RuntimeError("硬件解码未收到首帧"))
    worker.read_software = AsyncMock()
    await worker.start()
    await asyncio.sleep(0.01)
    assert worker.camera["status"] == "error"
    assert "软件回退未开启" in worker.camera["last_error"]
    worker.read_software.assert_not_awaited()
    await worker.stop()


async def test_native_capture_copies_analysis_frames_and_restores_portable_video_demand(
    tmp_path, monkeypatch
):
    import mmap
    from pathlib import Path

    on_frame = AsyncMock()
    config = camera()
    worker = capture.HardwareCapture(config, on_frame)
    worker.path = str(tmp_path / "capture")
    proc = SimpleNamespace(returncode=None)
    proc.terminate = lambda: setattr(proc, "returncode", 0)
    proc.kill = proc.terminate
    proc.wait = AsyncMock(return_value=0)
    published = asyncio.Event()
    counts = {}

    async def spawn(*args, **kwargs):
        Path(worker.path).write_bytes(bytes(64 + 2 * 384))
        published.set()
        return proc

    async def source():
        await published.wait()
        with (
            open(worker.path, "r+b") as stream,
            mmap.mmap(stream.fileno(), 0) as shared,
        ):
            for number in range(1, 31):
                slot = number % 2
                struct.pack_into(
                    "<QIIQQIIII",
                    shared,
                    0,
                    number * 2 - 1,
                    16,
                    16,
                    number,
                    number,
                    1,
                    384,
                    slot,
                    2,
                )
                shared[64 + slot * 384 : 64 + (slot + 1) * 384] = bytes([128]) * 384
                struct.pack_into("<Q", shared, 0, number * 2)
                await asyncio.sleep(0.05)
                if number == 10:
                    counts["analysis"] = on_frame.await_count
                    config["_full_rate_frame_consumers"] = 1
                elif number == 20:
                    counts["portable_end"] = on_frame.await_count
                    config.pop("_full_rate_frame_consumers")
            worker.stop_event.set()

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", spawn)
    await asyncio.wait_for(asyncio.gather(worker.read_once(), source()), 3)
    assert 2 <= counts["analysis"] <= 4
    assert counts["portable_end"] - counts["analysis"] == 10
    assert 2 <= on_frame.await_count - counts["portable_end"] <= 4
    assert config["decode_frames"] == 30
    assert config["python_frame_copies"] == on_frame.await_count
