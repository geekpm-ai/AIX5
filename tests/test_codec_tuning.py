import fractions

import av
import numpy as np
import pytest

from aix5_ai import codec_tuning
from aix5_ai.codec_tuning import FastH264Encoder


def frame(width, height, index, fps=5):
    # Fixed textured frame exposes rate-control problems hidden by black-frame tests.
    pixels = np.random.default_rng(7).integers(
        0, 256, (height, width, 3), dtype=np.uint8
    )
    image = av.VideoFrame.from_ndarray(pixels, format="bgr24")
    image.time_base = fractions.Fraction(1, 90000)
    image.pts = index * 90000 // fps
    return image


def test_h264_resolution_budget_does_not_recreate_codec_on_every_frame(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    monkeypatch.setattr(
        codec_tuning,
        "HardwareH264Encoder",
        lambda *args: (_ for _ in ()).throw(RuntimeError("not installed")),
    )
    encoder = FastH264Encoder()
    encoder.encode(frame(1920, 1080, 0))
    codec = encoder.codec
    assert codec.bit_rate == 4_000_000
    assert codec.framerate == 20
    assert codec.time_base == fractions.Fraction(1, 90000)
    for i in range(1, 4):
        payload, _ = encoder.encode(frame(1920, 1080, i))
        assert payload
        assert encoder.codec is codec
    encoder.encode(frame(960, 540, 4))
    assert encoder.codec is not codec
    codec = encoder.codec
    assert codec.bit_rate == 1_500_000
    encoder.encode(frame(960, 540, 5))
    assert encoder.codec is codec


def test_hardware_only_policy_rejects_missing_encoder(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", False)
    monkeypatch.setattr(
        codec_tuning,
        "HardwareH264Encoder",
        lambda *args: (_ for _ in ()).throw(RuntimeError("hardware unsupported")),
    )
    encoder = FastH264Encoder()
    with pytest.raises(RuntimeError, match="软件回退未开启"):
        encoder.encode(frame(640, 360, 0))


def test_hardware_encoder_retries_transient_open_failure(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", False)
    monkeypatch.setattr(codec_tuning, "HARDWARE_ENCODER_OPEN_ATTEMPTS", 3)
    monkeypatch.setattr(codec_tuning, "HARDWARE_ENCODER_RETRY_INITIAL", 0)
    calls = []

    class FakeHardware:
        def __init__(self, *args):
            self.closed = False
            self.keyframes = []

        def encode(self, _frame, force_keyframe):
            self.keyframes.append(force_keyframe)
            return b"\x00\x00\x00\x01\x65"

        def close(self):
            self.closed = True

    def open_hardware(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("VPU 尚未就绪")
        return FakeHardware(*args)

    monkeypatch.setattr(codec_tuning, "HardwareH264Encoder", open_hardware)
    encoder = FastH264Encoder()
    encoded = list(encoder._encode_frame(frame(640, 360, 0), True))
    assert encoded == [b"\x65"]
    assert len(calls) == 2


def test_hardware_failure_is_retryable_after_cooldown(monkeypatch):
    monkeypatch.setattr(codec_tuning, "allow_software_fallback", True)
    monkeypatch.setattr(codec_tuning, "HARDWARE_ENCODER_OPEN_ATTEMPTS", 1)
    monkeypatch.setattr(codec_tuning, "HARDWARE_ENCODER_RETRY_COOLDOWN", 0)
    calls = []
    hardware_instances = []

    class FakeHardware:
        def __init__(self):
            self.keyframes = []

        def encode(self, _frame, force_keyframe):
            self.keyframes.append(force_keyframe)
            return b"\x00\x00\x00\x01\x65"

        def close(self):
            pass

    def open_hardware(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("VPU 尚未就绪")
        hardware = FakeHardware()
        hardware_instances.append(hardware)
        return hardware

    monkeypatch.setattr(codec_tuning, "HardwareH264Encoder", open_hardware)
    encoder = FastH264Encoder()
    # The first frame uses the explicitly allowed software fallback.
    assert list(encoder._encode_frame(frame(640, 360, 0), False))
    # The failed dimensions are tried again on the next frame and recover.
    assert list(encoder._encode_frame(frame(640, 360, 1), False)) == [b"\x65"]
    assert len(calls) == 2
    assert hardware_instances[0].keyframes == [True]
