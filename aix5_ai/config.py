"""Local-only configuration. Hardware packages come from the RDK OS image."""

import os
from pathlib import Path
from .labels import BODY_LABELS, COCO_LABELS

os.umask(0o077)
ROOT = Path(os.getenv("AIX5_DATA_DIR", "./data")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
EVENTS = ROOT / "events"
EVENTS.mkdir(exist_ok=True)
DB_PATH = ROOT / "app.db"
HOST = os.getenv("AIX5_HOST", "0.0.0.0")
PORT = int(os.getenv("AIX5_PORT", "8080"))
CAPTURE_BIN = os.getenv(
    "AIX5_CAPTURE_BIN",
    str(Path(__file__).resolve().parent.parent / "native/rdk_capture_mm"),
)
LEGACY_CAPTURE_BIN = os.getenv(
    "AIX5_LEGACY_CAPTURE_BIN",
    str(Path(__file__).resolve().parent.parent / "native/rdk_capture"),
)
DEFAULT_DETECTION_CONFIDENCE = 0.35
MODELS = {
    "body": {
        "name": "人体检测与跟踪 · 人体/人头/人脸/人手/17点关键点",
        "path": "/opt/tros/humble/lib/mono2d_body_detection/config/multitask_body_head_face_hand_kps_960x544.hbm",
        "labels": BODY_LABELS,
        "confidence_threshold": 0.25,
    },
    "yolo11n": {
        "name": "YOLO11n · COCO 80 类",
        "path": os.getenv(
            "AIX5_MODEL_PATH",
            "/app/pydev_demo/02_detection_sample/02_ultralytics_yolo11/yolo11n_detect_bayese_640x640_nv12.bin",
        ),
        "labels": COCO_LABELS,
        "confidence_threshold": DEFAULT_DETECTION_CONFIDENCE,
    },
    "yolo11m": {
        "name": "YOLO11m · COCO 80 类",
        "path": "/opt/hobot/model/x5/basic/yolo11m_detect_bayese_640x640_nv12_modified.bin",
        "labels": COCO_LABELS,
        "confidence_threshold": DEFAULT_DETECTION_CONFIDENCE,
    },
    "fire_smoke": {
        "name": "烟火 / 烟雾 · 尚无已验证模型",
        "path": "",
        "labels": ["fire", "smoke"],
    },
}


def model_confidence_threshold(model_id: str) -> float:
    """The score floor applied before detections reach any camera rule."""

    return float(MODELS[model_id].get("confidence_threshold", DEFAULT_DETECTION_CONFIDENCE))


DEFAULT_SETTINGS = {
    "timezone": "Asia/Shanghai",
    "preview_resolution": "960x540",
    "allow_software_fallback": False,
    "retention_days": 7,
    "max_event_storage_mb": 2048,
    "base_url": os.getenv("AIX5_BASE_URL", f"http://127.0.0.1:{PORT}"),
    "remote_access": {
        "enabled": False,
        "provider": "cloudflare",
        "hostname": "",
        "ipv4": True,
        "ipv6": True,
        "ipv4_address": "",
        "ipv6_address": "",
        "access_key_id": "",
        "access_key_secret": "",
        "interval_seconds": 300,
    },
}
