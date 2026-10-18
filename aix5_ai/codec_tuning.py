"""Small runtime codec tuning for the board's WebRTC sender.

aiortc deliberately uses libx264's default preset. On the X5 that leaves
unnecessary latency in a live preview, so use the ultrafast preset while
keeping WebRTC's low-latency and baseline-profile settings.
"""

from __future__ import annotations

import fractions
import logging
import os
import time

from .hardware_encoder import HardwareH264Encoder

allow_software_fallback = False
encoder_states = {}
log = logging.getLogger("aix5.encoder")
# The VPU can become ready shortly after the capture decoder during a cold
# boot.  Keep opening the context retryable rather than caching one failed
# dimensions forever.  Environment overrides make board bring-up tunable while
# keeping unit tests fast.
HARDWARE_ENCODER_OPEN_ATTEMPTS = int(
    os.getenv("AIX5_ENCODER_OPEN_ATTEMPTS", "4")
)
HARDWARE_ENCODER_RETRY_INITIAL = float(
    os.getenv("AIX5_ENCODER_RETRY_INITIAL", "0.1")
)
HARDWARE_ENCODER_RETRY_MAX = float(
    os.getenv("AIX5_ENCODER_RETRY_MAX", "0.5")
)
HARDWARE_ENCODER_RETRY_COOLDOWN = float(
    os.getenv("AIX5_ENCODER_RETRY_COOLDOWN", "2")
)
import av
from aiortc.codecs import h264 as h264_codec


class FastH264Encoder(h264_codec.H264Encoder):
    def _open_hardware(self, dimensions, fps, bitrate):
        """Open the RDK encoder with short retries for cold-boot races."""

        attempts = max(1, int(HARDWARE_ENCODER_OPEN_ATTEMPTS))
        delay = max(0.0, HARDWARE_ENCODER_RETRY_INITIAL)
        last_error = None
        for attempt in range(attempts):
            try:
                return HardwareH264Encoder(*dimensions, fps, bitrate)
            except Exception as exc:  # noqa: BLE001 - ctypes ABI exposes arbitrary errors
                last_error = exc
                if attempt + 1 < attempts and delay:
                    time.sleep(delay)
                    delay = min(
                        max(delay * 2, HARDWARE_ENCODER_RETRY_INITIAL),
                        max(HARDWARE_ENCODER_RETRY_MAX, HARDWARE_ENCODER_RETRY_INITIAL),
                    )
        raise RuntimeError(str(last_error or "硬件编码器初始化失败")) from last_error

    def _encode_frame(self, frame: av.VideoFrame, force_keyframe: bool):
        camera = frame.opaque if isinstance(frame.opaque, dict) else None
        previous_pts = getattr(self, "_previous_frame_pts", None)
        fps = getattr(self, "_source_fps", 20)
        if previous_pts is not None and frame.pts > previous_pts:
            fps = max(
                1,
                min(
                    30, round(1 / (float(frame.time_base) * (frame.pts - previous_pts)))
                ),
            )
        self._previous_frame_pts = frame.pts
        self._source_fps = fps
        pixels = frame.width * frame.height
        recommended = 4_000_000 if pixels >= 1920 * 1080 else 1_500_000
        bitrate = max(self.target_bitrate, recommended)
        hardware = getattr(self, "_hardware", None)
        dimensions = (frame.width, frame.height)
        if hardware and getattr(self, "_hardware_size", None) != dimensions:
            hardware.close()
            self._hardware = hardware = None
        retry_at = getattr(self, "_hardware_retry_at", {}).get(dimensions, 0)
        if hardware is None and time.monotonic() >= retry_at:
            try:
                self._hardware = hardware = self._open_hardware(
                    dimensions, fps, bitrate
                )
                self._hardware_size = dimensions
                getattr(self, "_hardware_retry_at", {}).pop(dimensions, None)
                # A software fallback codec may have been created while the
                # VPU was warming up. Drop it as soon as hardware recovers.
                self.codec = None
                self.buffer_data = b""
                self.buffer_pts = None
                # Switching encoders mid-stream must begin with an IDR; a
                # hardware P-frame cannot reference the previous software
                # encoder's decoded picture.
                self._hardware_force_keyframe = True
            except Exception as exc:  # noqa: BLE001 - VPU failures must trigger fallback
                self._hardware_error = str(exc)
                retry_map = getattr(self, "_hardware_retry_at", None)
                if retry_map is None:
                    retry_map = self._hardware_retry_at = {}
                retry_map[dimensions] = time.monotonic() + max(
                    0.0, HARDWARE_ENCODER_RETRY_COOLDOWN
                )
        if hardware is not None:
            try:
                hardware_keyframe = force_keyframe or getattr(
                    self, "_hardware_force_keyframe", False
                )
                data = hardware.encode(frame, hardware_keyframe)
                self._hardware_force_keyframe = False
                encoder_states[id(self)] = {
                    "backend": "hardware",
                    "width": frame.width,
                    "height": frame.height,
                }
                if camera is not None:
                    camera["encode_backend"] = "hardware"
                    camera["encode_error"] = None
                yield from self._split_bitstream(data)
                return
            except Exception as exc:  # noqa: BLE001 - encoder backend may raise arbitrary errors
                hardware.close()
                self._hardware = None
                self._hardware_error = str(exc)
                retry_map = getattr(self, "_hardware_retry_at", None)
                if retry_map is None:
                    retry_map = self._hardware_retry_at = {}
                retry_map[dimensions] = time.monotonic() + max(
                    0.0, HARDWARE_ENCODER_RETRY_COOLDOWN
                )
        if not allow_software_fallback:
            if camera is not None:
                camera["encode_backend"] = "error"
                camera["encode_error"] = "硬件编码不可用，软件回退未开启"
            encoder_states[id(self)] = {
                "backend": "error",
                "error": getattr(self, "_hardware_error", "硬件编码不可用"),
            }
            raise RuntimeError(
                "硬件编码不可用，软件回退未开启："
                + getattr(self, "_hardware_error", "")
            )
        if camera is not None:
            camera["encode_backend"] = "software"
            camera["encode_error"] = None
        encoder_states[id(self)] = {
            "backend": "software",
            "width": frame.width,
            "height": frame.height,
        }
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.2
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            self.codec = av.CodecContext.create("libx264", "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = bitrate
            self.codec.pix_fmt = "yuv420p"
            self.codec.framerate = fractions.Fraction(fps, 1)
            self.codec.time_base = fractions.Fraction(1, 90000)
            self.codec.options = {
                "level": "31",
                "tune": "zerolatency",
                "preset": "ultrafast",
                # libx264 otherwise auto-selects a large worker pool on the
                # eight-core X5. One encoder per viewer/camera then consumes
                # most CPU even though the source is already hardware-decoded.
                "threads": "1",
                "lookahead": "0",
            }
            self.codec.profile = "Baseline"

        data_to_send = b""
        for package in self.codec.encode(frame):
            data_to_send += bytes(package)
        if data_to_send:
            yield from self._split_bitstream(data_to_send)

    def __del__(self):
        hardware = getattr(self, "_hardware", None)
        if hardware:
            hardware.close()
        encoder_states.pop(id(self), None)


def install_h264_tuning() -> None:
    # aiortc's get_encoder function closes over a module global named
    # H264Encoder. Replace that binding so every newly-created sender uses the
    # tuned subclass without modifying the installed virtualenv package.
    import aiortc.codecs

    aiortc.codecs.get_encoder.__globals__["H264Encoder"] = FastH264Encoder
