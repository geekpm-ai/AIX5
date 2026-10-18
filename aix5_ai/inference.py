from __future__ import annotations
import logging, time
from pathlib import Path
import cv2, numpy as np

try:
    import hbm_runtime
except Exception:
    hbm_runtime = None
from .config import DEFAULT_DETECTION_CONFIDENCE, MODELS, model_confidence_threshold
from .utils import preprocess_utils as preprocess
from .utils import postprocess_utils as postprocess

log = logging.getLogger("aix5.inference")
cv2.setNumThreads(1)
DEFAULT_MODEL = MODELS["yolo11n"]["path"]


class BpuDetector:
    def __init__(self, model_path: str = DEFAULT_MODEL, score_threshold: float = DEFAULT_DETECTION_CONFIDENCE):
        if hbm_runtime is None:
            raise RuntimeError("hbm_runtime 不可用")
        self.model = hbm_runtime.HB_HBMRuntime(model_path)
        self.model_name = self.model.model_names[0]
        self.input_names = self.model.input_names[self.model_name]
        self.output_names = self.model.output_names[self.model_name]
        self.input_shapes = self.model.input_shapes[self.model_name]
        self.output_quants = self.model.output_quants[self.model_name]
        self.input_h = self.input_shapes[self.input_names[0]][2]
        self.input_w = self.input_shapes[self.input_names[0]][3]
        self.score_threshold = score_threshold
        self.resize_type = 1
        self.classes_num = 80
        self.reg = 16
        self.strides = [8, 16, 32]
        self.anchor_sizes = [80, 40, 20]
        self.weights_static = np.arange(self.reg, dtype=np.float32)[
            np.newaxis, np.newaxis, :
        ]
        self.model.set_scheduling_params(
            priority={self.model_name: 128}, bpu_cores={self.model_name: [0]}
        )
        log.info(
            "BPU 模型已加载 path=%s input=%sx%s", model_path, self.input_w, self.input_h
        )

    def prepare(self, bgr: np.ndarray):
        img = preprocess.resized_image(
            bgr, self.input_w, self.input_h, self.resize_type
        )
        y, uv = preprocess.bgr_to_nv12_planes(img)
        nv12 = np.concatenate(
            (y.astype(np.uint8).reshape(-1), uv.astype(np.uint8).reshape(-1)), axis=0
        )
        return {
            self.model_name: {
                self.input_names[0]: nv12.reshape(
                    (1, self.input_h * 3 // 2, self.input_w, 1)
                )
            }
        }

    def infer(self, bgr: np.ndarray):
        started = time.monotonic()
        outputs = self.model.run(self.prepare(bgr))[self.model_name]
        result = {"latency_ms": (time.monotonic() - started) * 1000, "detections": []}
        try:
            fp = postprocess.dequantize_outputs(outputs, self.output_quants)
            boxes_all = []
            scores_all = []
            ids_all = []
            raw = -np.log(1 / self.score_threshold - 1)
            for i, (stride, anchor) in enumerate(zip(self.strides, self.anchor_sizes)):
                scores, ids, valid = postprocess.filter_classification(
                    fp[self.output_names[2 * i]], raw
                )
                boxes = postprocess.decode_boxes(
                    fp[self.output_names[2 * i + 1]],
                    valid,
                    anchor,
                    stride,
                    self.weights_static,
                )
                boxes_all.append(boxes)
                scores_all.append(scores)
                ids_all.append(ids)
            boxes = np.concatenate(boxes_all)
            scores = np.concatenate(scores_all)
            ids = np.concatenate(ids_all)
            keep = postprocess.NMS(boxes, scores, ids, 0.45)
            scaled = postprocess.scale_coords_back(
                boxes[keep],
                bgr.shape[1],
                bgr.shape[0],
                self.input_w,
                self.input_h,
                self.resize_type,
            )
            result["detections"] = [
                {
                    "class_id": int(i),
                    "confidence": float(s),
                    "bbox": [float(v) for v in b],
                }
                for b, i, s in zip(scaled, ids[keep], scores[keep])
            ]
        except Exception as exc:
            raise RuntimeError("YOLO 后处理失败") from exc
        return result


_detectors = {}


def get_detector(model_id="yolo11n"):
    """Called inside the application's BPU semaphore, including model loading."""
    if model_id not in MODELS or not MODELS[model_id]["path"]:
        raise ValueError("不支持的模型：" + model_id)
    if model_id not in _detectors:
        path = MODELS[model_id]["path"]
        if not Path(path).is_file():
            raise RuntimeError("模型文件未安装：" + model_id)
        _detectors[model_id] = BpuDetector(path, score_threshold=model_confidence_threshold(model_id))
    return _detectors[model_id]


def prepare_inference_process(bgr):
    """Small 640x640 IPC payload instead of copying every 1080p frame."""
    return preprocess.resized_image(bgr, 640, 640, 1)


def infer_in_process(model_id, prepared, source_width, source_height):
    result = get_detector(model_id).infer(prepared)
    detections = result.get("detections", [])
    if detections:
        boxes = np.array([d["bbox"] for d in detections], dtype=np.float32)
        boxes = postprocess.scale_coords_back(
            boxes, source_width, source_height, 640, 640, 1
        )
        for detection, box in zip(detections, boxes):
            detection["bbox"] = [float(value) for value in box]
    return result
