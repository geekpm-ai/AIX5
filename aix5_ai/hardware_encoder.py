"""RDK VPU H.264 encoder loaded lazily inside aiortc's encoder thread."""

import ctypes
import os
from pathlib import Path

import av

LIBRARY_PATH = os.getenv(
    "AIX5_ENCODER_LIB",
    str(Path(__file__).resolve().parent.parent / "native/librdk_h264_encoder.so"),
)


class HardwareH264Encoder:
    def __init__(self, width, height, fps, bitrate):
        self.library = ctypes.CDLL(LIBRARY_PATH)
        self.library.aix5_encoder_open.argtypes = [ctypes.c_int] * 4
        self.library.aix5_encoder_open.restype = ctypes.c_void_p
        self.library.aix5_encoder_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int64,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.library.aix5_encoder_close.argtypes = [ctypes.c_void_p]
        self.handle = self.library.aix5_encoder_open(width, height, fps, bitrate)
        if not self.handle:
            raise RuntimeError("RDK VPU H.264 编码器初始化失败")
        self.width, self.height = width, height
        self.output = ctypes.create_string_buffer(width * height * 3 // 2)

    def encode(self, frame: av.VideoFrame, force_keyframe):
        image = frame.to_ndarray(format="nv12")
        size = self.library.aix5_encoder_encode(
            self.handle,
            image.ctypes.data,
            image.nbytes,
            int(frame.pts * frame.time_base * 90000),
            int(force_keyframe),
            self.output,
            len(self.output),
        )
        if size <= 0:
            raise RuntimeError(f"RDK VPU H.264 编码失败 ({size})")
        return ctypes.string_at(self.output, size)

    def close(self):
        if getattr(self, "handle", None):
            self.library.aix5_encoder_close(self.handle)
            self.handle = None

    def __del__(self):
        self.close()
