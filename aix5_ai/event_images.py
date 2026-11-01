"""Render detection boxes into persisted event screenshots only."""

from __future__ import annotations

import math
from collections.abc import Mapping

import cv2


def make_event_snapshot(bgr, detections, max_width=960):
    """Encode the inference frame with its boxes, leaving shared pixels intact.

    Detection coordinates refer to the original BGR frame. Drawing after
    resizing keeps thin boxes readable in event thumbnails, while the separate
    canvas prevents annotations leaking into live preview or later inference.
    """
    if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3 or not bgr.size:
        return None
    height, width = bgr.shape[:2]
    if width > max_width:
        output_width = max_width
        output_height = max(1, round(height * output_width / width))
        canvas = cv2.resize(
            bgr, (output_width, output_height), interpolation=cv2.INTER_AREA
        )
    else:
        canvas = bgr.copy()
        output_height, output_width = height, width
    scale_x, scale_y = output_width / width, output_height / height
    thickness = max(2, round(output_width / 480))
    color = (48, 59, 255)  # Red #ff3b30, reserved for persisted event boxes.

    for detection in detections or ():
        if not isinstance(detection, Mapping):
            continue
        box = detection.get("bbox")
        if isinstance(box, (str, bytes, Mapping)):
            continue
        try:
            x1, y1, x2, y2 = (float(value) for value in box)
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
            continue
        if x2 <= x1 or y2 <= y1:
            continue
        if x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height:
            continue
        left = round(max(0, x1) * scale_x)
        top = round(max(0, y1) * scale_y)
        right = min(output_width - 1, round(min(width, x2) * scale_x))
        bottom = min(output_height - 1, round(min(height, y2) * scale_y))
        if right <= left or bottom <= top:
            continue
        # A narrow dark outline keeps the red box visible on bright scenes.
        cv2.rectangle(
            canvas, (left, top), (right, bottom), (0, 0, 0), thickness + 2, cv2.LINE_AA
        )
        cv2.rectangle(
            canvas, (left, top), (right, bottom), color, thickness, cv2.LINE_AA
        )

    ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return encoded.tobytes() if ok else None
