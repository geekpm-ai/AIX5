"""Board smoke test: burst input must retain 20 FPS and exact 960x540 output.

Run on RDK X5: python tools/verify_native_stream.py [--binary PATH]
Uses synthetic frames only; never opens cameras or saves recordings.
"""

import argparse
from pathlib import Path
import os, mmap, struct, tempfile, threading, time, subprocess, json, select
import av

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--binary",
    default=str(Path(__file__).resolve().parent.parent / "native/rdk_video_stream"),
)
args = parser.parse_args()
w, h = 1920, 1080
slots = 8
size = w * h * 3 // 2
f = tempfile.TemporaryFile()
f.truncate(64 + size * slots + slots * 8)
mm = mmap.mmap(f.fileno(), 0)
struct.pack_into("<QIIQQIIII", mm, 0, 0, w, h, 0, 0, 1, size, 0, slots)
proc = subprocess.Popen(
    [args.binary, str(f.fileno()), "960", "540", "20"],
    pass_fds=(f.fileno(),),
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
)
stop = threading.Event()
origin = time.monotonic_ns()
frame = bytes([128]) * size


def write():
    time.sleep(0.3)
    for group in range(15):
        if stop.is_set():
            break
        for offset in range(4):
            i = group * 4 + offset
            slot = i % slots
            struct.pack_into("<Q", mm, 0, i * 2 + 1)
            mm[64 + slot * size : 64 + (slot + 1) * size] = frame
            stamp = origin + i * 50000000
            struct.pack_into("<Q", mm, 16, stamp)
            struct.pack_into("<Q", mm, 64 + size * slots + slot * 8, stamp)
            struct.pack_into("<I", mm, 40, slot)
            struct.pack_into("<Q", mm, 0, i * 2 + 2)
        time.sleep(0.2)


thread = threading.Thread(target=write)
thread.start()
pts = []
decoder = av.CodecContext.create("h264", "r")
dimensions = set()


def read(n):
    out = bytearray()
    deadline = time.monotonic() + 8
    while len(out) < n:
        if time.monotonic() > deadline:
            raise TimeoutError("native stream stalled")
        if not select.select([proc.stdout], [], [], 0.5)[0]:
            continue
        data = os.read(proc.stdout.fileno(), n - len(out))
        if not data:
            raise RuntimeError("native stream ended")
        out.extend(data)
    return bytes(out)


try:
    for i in range(40):
        p, x, y, n = struct.unpack("<QIII", read(20))
        data = read(n)
        pts.append(p)
        for packet in decoder.parse(data):
            for decoded in decoder.decode(packet):
                dimensions.add((decoded.width, decoded.height))
    assert all(b - a == 4500 for a, b in zip(pts, pts[1:])), pts
    assert dimensions == {(960, 540)}, dimensions
    print(
        json.dumps(
            {
                "frames": len(pts),
                "fps": 39 * 90000 / (pts[-1] - pts[0]),
                "dimensions": list(dimensions),
                "burst_size": 4,
                "monotonic_contiguous_pts": True,
            }
        ),
        flush=True,
    )
finally:
    stop.set()
    thread.join()
    proc.terminate()
    try:
        proc.wait(3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    mm.close()
    f.close()
