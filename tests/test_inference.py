from types import SimpleNamespace

import numpy as np

from aix5_ai import config, inference
from aix5_ai.utils.postprocess_utils import NMS


def test_detector_cache_is_per_model_and_failures_can_retry(tmp_path, monkeypatch):
    paths = {}
    for name in ["yolo11n", "yolo11m"]:
        path = tmp_path / (name + ".bin")
        path.touch()
        paths[name] = {"path": str(path), "confidence_threshold": 0.35 if name == "yolo11n" else 0.45}
    monkeypatch.setattr(inference, "MODELS", paths)
    monkeypatch.setattr(config, "MODELS", paths)
    monkeypatch.setattr(inference, "_detectors", {})
    calls = []

    def load(path, score_threshold):
        calls.append((path, score_threshold))
        return SimpleNamespace(path=path, score_threshold=score_threshold)

    monkeypatch.setattr(inference, "BpuDetector", load)
    n = inference.get_detector("yolo11n")
    assert inference.get_detector("yolo11n") is n
    assert inference.get_detector("yolo11m") is not n
    assert len(calls) == 2
    assert calls[0][1] == 0.35
    assert calls[1][1] == 0.45


def test_nms_is_class_specific_and_handles_empty_results():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 9, 9], [1, 1, 9, 9]], np.float32)
    assert set(NMS(boxes, np.array([0.9, 0.8, 0.7]), np.array([0, 0, 1]))) == {0, 2}
    assert NMS(np.empty((0, 4)), np.empty(0), np.empty(0)) == []
