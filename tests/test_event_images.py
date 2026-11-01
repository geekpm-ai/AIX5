"""Verify the pixels saved in event JPEGs, independent of browser overlays."""

import cv2
import numpy as np
import pytest

from aix5_ai.event_images import make_event_snapshot


def decode(jpeg):
    assert jpeg.startswith(b"\xff\xd8")
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def red_pixels(image):
    b, g, r = np.moveaxis(image.astype(np.int16), -1, 0)
    return (r > 150) & (r - g > 50) & (r - b > 50)


def assert_box_edges(image, box):
    left, top, right, bottom = box
    mask = red_pixels(image)
    # Inspect the middle of all four edges, away from antialiased corners.
    assert mask[top - 1 : top + 2, left + 6 : right - 6].mean() > 0.6
    assert mask[bottom - 1 : bottom + 2, left + 6 : right - 6].mean() > 0.6
    assert mask[top + 6 : bottom - 6, left - 1 : left + 2].mean() > 0.6
    assert mask[top + 6 : bottom - 6, right - 1 : right + 2].mean() > 0.6


def test_event_jpeg_contains_each_detection_without_mutating_inference_frame():
    frame = np.full((180, 320, 3), 128, dtype=np.uint8)
    original = frame.copy()
    boxes = [[20, 30, 110, 150], [170, 45, 290, 140]]
    result = decode(make_event_snapshot(frame, [{"bbox": box} for box in boxes]))
    assert result.shape == frame.shape  # Small inputs are not enlarged.
    for box in boxes:
        assert_box_edges(result, box)
    assert not red_pixels(result[65:100, 45:85]).any()
    np.testing.assert_array_equal(frame, original)


def test_event_boxes_use_original_frame_coordinates_when_resized():
    frame = np.full((1080, 1920, 3), 128, dtype=np.uint8)
    result = decode(make_event_snapshot(frame, [{"bbox": [200, 160, 1000, 880]}]))
    assert result.shape == (540, 960, 3)
    assert_box_edges(result, [100, 80, 500, 440])
    # A box mistakenly drawn without scaling would put its edge at x=200.
    assert not red_pixels(result[150:350, 190:210]).any()
    assert np.all(frame == 128)


@pytest.mark.parametrize("detections", [[], None, [{"bbox": [0, 0, 0, 0]}]])
def test_empty_or_absence_detections_do_not_invent_a_box(detections):
    frame = np.full((100, 160, 3), 128, dtype=np.uint8)
    result = decode(make_event_snapshot(frame, detections))
    assert not red_pixels(result).any()
    np.testing.assert_array_equal(result, frame)


def test_invalid_and_fully_offscreen_boxes_are_ignored():
    frame = np.full((100, 160, 3), 128, dtype=np.uint8)
    invalid = [
        None,
        "not a detection",
        {},
        {"bbox": None},
        {"bbox": "1234"},
        {"bbox": [1, 2, 3]},
        {"bbox": [1, 2, 3, 4, 5]},
        {"bbox": ["bad", 2, 30, 40]},
        {"bbox": [float("nan"), 2, 30, 40]},
        {"bbox": [1, 2, float("inf"), 40]},
        {"bbox": [70, 20, 30, 50]},
        {"bbox": [10, 70, 30, 50]},
        {"bbox": [-80, 10, -5, 40]},
        {"bbox": [180, 10, 200, 40]},
        {"bbox": [10, -80, 50, -5]},
        {"bbox": [10, 110, 50, 180]},
    ]
    result = decode(make_event_snapshot(frame, invalid))
    np.testing.assert_array_equal(result, frame)


def test_partially_offscreen_boxes_are_clipped_to_image():
    frame = np.full((100, 160, 3), 128, dtype=np.uint8)
    result = decode(make_event_snapshot(frame, [{"bbox": [-30, -40, 90, 70]}]))
    mask = red_pixels(result)
    assert mask[0:2, 10:80].mean() > 0.6
    assert mask[10:60, 0:2].mean() > 0.6
    assert mask[69:72, 10:80].mean() > 0.6
    assert mask[10:60, 89:92].mean() > 0.6
    assert not mask[80:, 110:].any()
    assert np.all(frame == 128)
