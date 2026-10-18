"""RDK X5 smoke test for encoder recovery and shutdown under pipe backpressure.

Run: python3 tools/verify_native_backpressure.py --binary /tmp/rdk_video_stream
Add --shutdown-while-blocked to test SIGTERM while stdout is full.
Uses only deterministic synthetic NV12 pixels and temporary anonymous files.
Does not open cameras, access credentials, change services, or retain video.
"""

import argparse
import hashlib
import json
import mmap
import os
import random
import select
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def verify(binary, pause_seconds=2.6, frames_after_pause=65, shutdown=False):
    width, height, fps = 960, 540, 20
    size = width * height * 3 // 2
    result = {
        "binary": Path(binary).name,
        "binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
        "resolution": [width, height],
        "input_fps": fps,
        "pause_seconds": pause_seconds,
        "shutdown_while_blocked": shutdown,
    }
    stop = threading.Event()
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as log:
        source.truncate(64 + size * 2)
        with mmap.mmap(source.fileno(), 0) as shared:
            struct.pack_into(
                "<QIIQQIII", shared, 0, 0, width, height, 0, 0, 1, size, 0
            )
            pixels = random.Random(7).randbytes(size)
            for slot in range(2):
                shared[64 + slot * size : 64 + (slot + 1) * size] = pixels

            def publish():
                index = 0
                while not stop.is_set():
                    index += 1
                    struct.pack_into("<Q", shared, 0, index * 2 + 1)
                    struct.pack_into("<Q", shared, 16, time.monotonic_ns())
                    struct.pack_into("<Q", shared, 24, index)
                    struct.pack_into("<I", shared, 40, index & 1)
                    struct.pack_into("<Q", shared, 0, index * 2 + 2)
                    stop.wait(1 / fps)

            thread = threading.Thread(target=publish)
            proc = subprocess.Popen(
                [str(binary), str(source.fileno()), str(width), str(height), str(fps)],
                pass_fds=(source.fileno(),),
                stdout=subprocess.PIPE,
                stderr=log,
                bufsize=0,
            )
            deadline = time.monotonic() + 20

            def read_exact(count):
                data = bytearray()
                while len(data) < count:
                    if time.monotonic() > deadline:
                        raise TimeoutError("encoder did not recover within 20 seconds")
                    if not select.select([proc.stdout], [], [], 0.1)[0]:
                        continue
                    chunk = os.read(proc.stdout.fileno(), count - len(data))
                    if not chunk:
                        raise EOFError(f"EOF expected={count} got={len(data)}")
                    data.extend(chunk)
                return data

            def read_frame():
                pts, actual_width, actual_height, count = struct.unpack(
                    "<QIII", read_exact(20)
                )
                if (actual_width, actual_height) != (width, height):
                    raise ValueError("unexpected encoder output dimensions")
                if not 0 < count < 16 * 1024 * 1024:
                    raise ValueError("invalid encoder payload length")
                read_exact(count)
                return pts

            thread.start()
            try:
                first_pts = read_frame()
                time.sleep(pause_seconds)
                if shutdown:
                    result["survived_backpressure"] = proc.poll() is None
                else:
                    pts = [first_pts]
                    resumed_at = time.monotonic()
                    for _ in range(frames_after_pause):
                        pts.append(read_frame())
                    result.update(
                        survived_backpressure=True,
                        frames_after_pause=len(pts) - 1,
                        elapsed_after_pause=round(time.monotonic() - resumed_at, 3),
                        monotonic_pts=all(a < b for a, b in zip(pts, pts[1:])),
                    )
            except (EOFError, TimeoutError, ValueError) as exc:
                result.update(survived_backpressure=False, error=str(exc))
            finally:
                stop.set()
                thread.join()
                stopping_at = time.monotonic()
                if proc.poll() is None:
                    proc.terminate()
                result["forced_kill"] = False
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    result["forced_kill"] = True
                    proc.kill()
                    proc.wait()
                result["shutdown_seconds"] = round(time.monotonic() - stopping_at, 3)
                result["exit_code"] = proc.returncode
                proc.stdout.close()
                log.seek(max(0, log.tell() - 3000))
                result["native_log"] = log.read().decode(errors="replace")
    result["passed"] = (
        result["survived_backpressure"]
        and result.get("monotonic_pts", shutdown)
        and not result["forced_kill"]
        and result["exit_code"] == 0
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--shutdown-while-blocked", action="store_true")
    args = parser.parse_args()
    result = verify(args.binary, shutdown=args.shutdown_while_blocked)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
