from types import SimpleNamespace

import pytest

from aix5_ai.body_bridge import (
    PerCameraTracker,
    _body_message,
    _decode_packet,
    _encode_packet,
    _scale_result,
)


def roi(kind, x, y, width, height):
    return SimpleNamespace(
        type=kind,
        rect=SimpleNamespace(x_offset=x, y_offset=y, width=width, height=height),
        confidence=0.0,
    )


def test_body_message_keeps_body_parts_keypoints_and_track_id():
    target = SimpleNamespace(
        type="person",
        track_id=42,
        rois=[roi("body", 10, 20, 100, 180), roi("head", 30, 25, 40, 45), roi("face", 35, 30, 24, 28), roi("hand", 80, 100, 18, 20)],
        points=[SimpleNamespace(type="body_kps", point=[SimpleNamespace(x=20, y=30), SimpleNamespace(x=40, y=50)], confidence=[.9, .8])],
    )
    message = SimpleNamespace(header=SimpleNamespace(frame_id="7"), targets=[target])
    result = _body_message(message)
    assert result["frame_id"] == "7"
    assert [item["label"] for item in result["detections"]] == ["body", "head", "face", "hand"]
    assert all(item["track_id"] == 42 for item in result["detections"])
    assert len(result["detections"][0]["keypoints"]) == 2
    assert result["detections"][1]["keypoints"] == []


def test_packet_keeps_camera_identity_and_sequence():
    packet = _encode_packet("camera-a", 17, b"jpeg")
    assert _decode_packet(packet) == ("camera-a", 17, 0, b"jpeg")


def test_packet_supports_raw_bgr_frames():
    packet = _encode_packet("camera-a", 17, b"bgr", encoding=1)
    assert _decode_packet(packet) == ("camera-a", 17, 1, b"bgr")


def test_tracker_state_is_independent_per_bridge_camera():
    tracker_a = PerCameraTracker()
    tracker_b = PerCameraTracker()
    body = lambda x: {"label": "body", "bbox": [x, 10, x + 100, 190], "track_id": 999}
    part = lambda x: {"label": "face", "bbox": [x + 20, 30, x + 60, 70], "track_id": 999}

    first_a = tracker_a.assign([body(10), part(10)])
    second_a = tracker_a.assign([body(12), part(12)])
    first_b = tracker_b.assign([body(10), part(10)])

    assert [item["track_id"] for item in first_a] == [1, 1]
    assert [item["track_id"] for item in second_a] == [1, 1]
    assert [item["track_id"] for item in first_b] == [1, 1]


def test_scale_result_restores_original_camera_coordinates():
    result = {
        "detections": [
            {
                "label": "body",
                "bbox": [10, 20, 110, 220],
                "keypoints": [{"x": 30, "y": 40, "score": 0.9}],
            }
        ]
    }
    _scale_result(result, 1920, 1080, 960, 544)
    assert result["detections"][0]["bbox"] == pytest.approx([20, 39.705882352941174, 220, 436.7647058823529])
    assert result["detections"][0]["keypoints"][0]["x"] == pytest.approx(60)
    assert result["detections"][0]["keypoints"][0]["y"] == pytest.approx(79.41176470588235)
