"""Bridge the official TROS mono2d body detector into the AIX5 loop.

The body HBM declares an NV12_SEPARATE input, which the Python HBM runtime
cannot submit directly. TROS already provides the supported DNN node and MOT
implementation, so this small localhost bridge publishes BGR frames to that
node and converts its PerceptionTargets message into AIX5 JSON detections.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import signal
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

BODY_MODEL = "/opt/tros/humble/lib/mono2d_body_detection/config/multitask_body_head_face_hand_kps_960x544.hbm"
_HEADER = struct.Struct("!II")
_LENGTH = struct.Struct("!I")
_PACKET_META = struct.Struct("!HI")
BODY_RESULT_TIMEOUT = float(os.getenv("AIX5_BODY_RESULT_TIMEOUT", "10"))
_BODY_PREPROCESS_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="aix5-body-preprocess"
)


def _body_message(message):
    detections = []
    label_names = {"body": "人体", "head": "人头", "face": "人脸", "hand": "人手"}
    for target in message.targets:
        if target.type != "person":
            continue
        keypoints = []
        for point_group in target.points:
            if point_group.type != "body_kps":
                continue
            # The X5 message currently exposes 19 slots; the model contract
            # defines the first 17 as body keypoints. The final two are
            # implementation-specific auxiliary points and are not rendered.
            for index, point in enumerate(point_group.point[:17]):
                score = point_group.confidence[index] if index < len(point_group.confidence) else 0.0
                keypoints.append({"x": float(point.x), "y": float(point.y), "score": float(score)})
        for roi in target.rois:
            if roi.type not in label_names:
                continue
            detections.append(
                {
                    "label": roi.type,
                    "label_zh": label_names[roi.type],
                    # The official message uses the ROI confidence field for
                    # internal grouping and emits 0 for this model. The model
                    # has already applied its threshold; expose valid rule
                    # confidence while retaining the raw value for diagnostics.
                    "confidence": 1.0,
                    "model_confidence": float(roi.confidence),
                    "bbox": [
                        float(roi.rect.x_offset),
                        float(roi.rect.y_offset),
                        float(roi.rect.x_offset + roi.rect.width),
                        float(roi.rect.y_offset + roi.rect.height),
                    ],
                    "track_id": int(target.track_id),
                    "keypoints": keypoints if roi.type == "body" else [],
                }
            )
    return {"frame_id": message.header.frame_id, "detections": detections}


def _scale_result(result, source_width: int, source_height: int, input_width: int, input_height: int):
    """Map detector coordinates back to the original camera frame."""

    scale_x = float(source_width) / float(input_width)
    scale_y = float(source_height) / float(input_height)
    if scale_x == 1.0 and scale_y == 1.0:
        return result
    for detection in result.get("detections", []):
        x1, y1, x2, y2 = detection.get("bbox", [0, 0, 0, 0])
        detection["bbox"] = [x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y]
        for point in detection.get("keypoints", []):
            point["x"] *= scale_x
            point["y"] *= scale_y
    return result


def _prepare_body_frame(bgr: np.ndarray, input_width: int = 960, input_height: int = 544) -> bytes:
    """Resize to the HBM input dimensions and return raw BGR bytes."""

    if bgr.shape[1] != input_width or bgr.shape[0] != input_height:
        bgr = cv2.resize(bgr, (input_width, input_height), interpolation=cv2.INTER_AREA)
    return bgr.tobytes()


def _iou(first, second):
    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


class PerCameraTracker:
    """Keep MOT identity state separate while the BPU model is shared."""

    def __init__(self):
        self.next_id = 1
        self.previous = []

    def assign(self, detections):
        bodies = [item for item in detections if item["label"] == "body"]
        used = set()
        current = []
        for body in bodies:
            best, best_score = None, 0.30
            for index, (track_id, previous_box) in enumerate(self.previous):
                if index in used:
                    continue
                score = _iou(body["bbox"], previous_box)
                if score > best_score:
                    best, best_score = index, score
            if best is None:
                track_id = self.next_id
                self.next_id += 1
            else:
                used.add(best)
                track_id = self.previous[best][0]
            body["track_id"] = track_id
            current.append((track_id, body["bbox"]))
        self.previous = current
        for item in detections:
            if item["label"] == "body":
                continue
            center = ((item["bbox"][0] + item["bbox"][2]) / 2, (item["bbox"][1] + item["bbox"][3]) / 2)
            containing = [pair for pair in current if pair[1][0] <= center[0] <= pair[1][2] and pair[1][1] <= center[1] <= pair[1][3]]
            item["track_id"] = min(containing, key=lambda pair: _iou(item["bbox"], pair[1]))[0] if containing else 0
        return detections


def _frame_id(source_id: str, sequence: int) -> str:
    """Build the identity carried through ROS and back to the caller."""

    source = str(source_id).strip()
    if not source or ":" in source or any(ord(char) < 32 for char in source):
        raise ValueError("人体检测通道 ID 无效")
    return f"{source}:{int(sequence)}"


def _encode_packet(source_id: str, sequence: int, frame: bytes, encoding: int = 0) -> bytes:
    source = str(source_id).encode("utf-8")
    if not source or len(source) > 65535:
        raise ValueError("人体检测通道 ID 过长")
    if encoding not in (0, 1, 2):
        raise ValueError("人体检测帧编码类型无效")
    return _PACKET_META.pack(len(source), int(sequence)) + source + bytes([encoding]) + frame


def _decode_packet(packet: bytes):
    if len(packet) < _PACKET_META.size:
        raise ValueError("人体检测请求包不完整")
    source_len, sequence = _PACKET_META.unpack_from(packet)
    start = _PACKET_META.size
    end = start + source_len
    if not source_len or end > len(packet):
        raise ValueError("人体检测请求包通道 ID 无效")
    source_id = packet[start:end].decode("utf-8")
    if end >= len(packet):
        raise ValueError("人体检测请求包缺少帧编码类型")
    return source_id, sequence, packet[end], packet[end + 1 :]


def worker_main(args):
    # ROS is installed outside the application venv. The parent starts this
    # worker from a shell which sources /opt/tros/humble/setup.bash.
    import rclpy
    from ai_msgs.msg import PerceptionTargets
    from rclpy.node import Node
    from sensor_msgs.msg import Image

    safe_id = re.sub(r"[^A-Za-z0-9_]", "_", args.camera_id)
    topic_root = "/aix5/body/" + safe_id
    image_topic, output_topic = topic_root + "/image", topic_root + "/out"
    detector = subprocess.Popen(
        [
            "ros2", "run", "mono2d_body_detection", "mono2d_body_detection",
            "--ros-args", "-r", f"__node:=aix5_body_{safe_id}",
            "-p", "is_shared_mem_sub:=0", "-p", f"model_file_name:={args.model}",
            "-p", f"ros_img_topic_name:={image_topic}",
            "-p", f"ai_msg_pub_topic_name:={output_topic}",
            # A shared detector must not carry MOT state from one camera into
            # another. Track IDs are assigned by PerCameraTracker below.
            "-p", "track_mode:=0", "-p", "model_type:=0",
        ],
        cwd="/opt/tros/humble/lib/mono2d_body_detection",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
            start_new_session=True,
    )

    debug_path = Path("/tmp/aix5-body-bridge-" + re.sub(r"[^A-Za-z0-9_]", "_", args.camera_id) + ".log")
    def debug(message):
        try:
            with debug_path.open("a") as stream:
                stream.write(f"{time.time():.3f} {message}\n")
        except OSError:
            pass

    class BridgeNode(Node):
        def __init__(self):
            super().__init__("aix5_body_bridge_" + safe_id)
            self.publisher = self.create_publisher(Image, image_topic, 10)
            self.connection = None
            self.send_lock = threading.Lock()
            self.create_subscription(PerceptionTargets, output_topic, self.on_result, 10)

        def on_result(self, message):
            if self.connection is None:
                return
            payload = json.dumps(_body_message(message), separators=(",", ":")).encode()
            try:
                debug(f"result frame={message.header.frame_id} targets={len(message.targets)}")
                with self.send_lock:
                    self.connection.sendall(struct.pack("!I", len(payload)) + payload)
            except OSError:
                pass

    rclpy.init()
    node = BridgeNode()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.2)
    # Do not announce readiness until the official node has created both its
    # image subscription and result publisher; otherwise the first frame can
    # be dropped while ROS is still loading the HBM model.
    ros_deadline = time.monotonic() + 60
    while (node.count_subscribers(image_topic) == 0 or node.count_publishers(output_topic) == 0) and time.monotonic() < ros_deadline:
        if detector.poll() is not None:
            raise RuntimeError("mono2d_body_detection 进程启动失败")
        rclpy.spin_once(node, timeout_sec=0.1)
    if node.count_subscribers(image_topic) == 0:
        raise RuntimeError("人体检测节点未订阅图像")
    debug(f"ready topic={image_topic} subscribers={node.count_subscribers(image_topic)}")
    print(f"READY {server.getsockname()[1]}", flush=True)
    connection = None
    try:
        deadline = time.monotonic() + 45
        while connection is None and time.monotonic() < deadline:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                rclpy.spin_once(node, timeout_sec=0.05)
                continue
        if connection is None:
            raise RuntimeError("body bridge parent connection timeout")
        node.connection = connection
        connection.settimeout(0.02)
        # Reuse the ROS message object. On RDK X5, constructing a fresh Python
        # Image message for every 1920x1080 frame triggers a multi-second
        # allocator/serialization stall in the TROS subscriber. Reusing the
        # object follows the official publisher pattern and keeps the stream
        # at the detector's normal throughput.
        message = Image()

        def recv_exact(size):
            data = bytearray()
            while len(data) < size and rclpy.ok():
                # Keep the ROS executor alive while the parent is idle or a
                # packet is arriving. A blocking socket loop plus a separate
                # rclpy.spin thread caused multi-second callback stalls on X5.
                rclpy.spin_once(node, timeout_sec=0.005)
                try:
                    chunk = connection.recv(size - len(data))
                except socket.timeout:
                    continue
                if not chunk:
                    return None
                data.extend(chunk)
            return bytes(data) if len(data) == size else None

        while rclpy.ok():
            debug("waiting")
            data = recv_exact(4)
            debug(f"header bytes={len(data)}")
            if not data:
                break
            length = struct.unpack("!I", data)[0]
            packet = recv_exact(length)
            if packet is None:
                return
            source_id, sequence, encoding, frame = _decode_packet(packet)
            if encoding == 2:
                if len(frame) < 8:
                    raise ValueError("人体检测 NV12 帧缺少尺寸")
                width, height = struct.unpack("!II", frame[:8])
                frame = frame[8:]
                expected = width * height * 3 // 2
                if width < 16 or height < 16 or len(frame) != expected:
                    raise ValueError("人体检测 NV12 帧长度无效")
                image = np.frombuffer(frame, np.uint8).reshape((height * 3 // 2, width))
                image_encoding = "nv12"
                image_height, image_width, image_step = height, width, width
            elif encoding == 1:
                expected = 960 * 544 * 3
                if len(frame) != expected:
                    raise ValueError("人体检测原始帧长度无效")
                image = np.frombuffer(frame, np.uint8).reshape((544, 960, 3))
                image_encoding = "bgr8"
                image_height, image_width, image_step = 544, 960, 960 * 3
            else:
                image = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
                image_encoding = "bgr8"
                image_height, image_width, image_step = image.shape[0], image.shape[1], image.shape[1] * 3 if image is not None else 0
            if image is None:
                continue
            debug(f"input seq={sequence} shape={image.shape}")
            message.header.frame_id = _frame_id(source_id, sequence)
            message.header.stamp = node.get_clock().now().to_msg()
            message.height, message.width = image_height, image_width
            message.encoding = image_encoding
            message.is_bigendian = 0
            message.step = image_step
            raw = image.tobytes()
            # sensor_msgs/Image.data is an array('B') on the RDK Python
            # bindings. Replacing it with a new bytes object for every 6 MB
            # 1080p frame makes publish() block for seconds. Mutate the
            # existing sequence in place so the ROS serializer can reuse its
            # allocation while still accepting a new decoded frame.
            if len(message.data) != len(raw):
                message.data = array("B", raw)
            else:
                message.data[:] = array("B", raw)
            node.publisher.publish(message)
    finally:
        if connection is not None:
            connection.close()
        server.close()
        node.destroy_node()
        rclpy.shutdown()
        try:
            os.killpg(detector.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            detector.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(detector.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            detector.wait()


class BodyInferenceBridge:
    """Async client for one official TROS body detector + MOT process."""

    def __init__(self, camera_id: str, model: str = BODY_MODEL):
        self.camera_id = camera_id
        self.model = model
        self.process = None
        self.reader = None
        self.writer = None
        self.sequence = {}
        self._write_lock = asyncio.Lock()
        self._reader_task = None
        self._waiters = {}
        self._pending = {}
        self._trackers = {}
        self._closed = False

    async def start(self):
        script = str(Path(__file__).resolve())
        command = (
            "source /opt/tros/humble/setup.bash; "
            f"exec /usr/bin/python3 {__import__('shlex').quote(script)} --worker "
            f"--camera-id {__import__('shlex').quote(self.camera_id)} "
            f"--model {__import__('shlex').quote(self.model)} "
            f"2>/tmp/aix5-body-bridge-{__import__('re').sub(r'[^A-Za-z0-9_]', '_', self.camera_id)}.log"
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash", "-lc", command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            line = await asyncio.wait_for(self.process.stdout.readline(), 60)
            if not line.startswith(b"READY "):
                raise RuntimeError("人体检测桥接进程启动失败")
            port = int(line.split()[1])
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
            self._closed = False
            self._reader_task = asyncio.create_task(self._read_results())
        except BaseException:
            await self.close()
            raise

    async def _read_results(self):
        """Read every ROS result and deliver it by its exact frame identity."""

        try:
            while self.reader is not None:
                header = await self.reader.readexactly(_LENGTH.size)
                length, = _LENGTH.unpack(header)
                if length <= 0 or length > 32 * 1024 * 1024:
                    raise RuntimeError("人体检测结果包长度无效")
                result = json.loads((await self.reader.readexactly(length)).decode("utf-8"))
                frame_id = str(result.get("frame_id", ""))
                if not frame_id:
                    continue
                waiter = self._waiters.pop(frame_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(result)
                else:
                    # Keep late results briefly so a reader scheduling delay
                    # cannot turn a valid result into a false timeout.
                    self._pending[frame_id] = result
                    if len(self._pending) > 128:
                        self._pending.pop(next(iter(self._pending)))
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            if not self._closed:
                error = RuntimeError("人体检测结果通道已断开")
                for waiter in list(self._waiters.values()):
                    if not waiter.done():
                        waiter.set_exception(error)
                self._waiters.clear()
        except Exception as exc:
            if not self._closed:
                for waiter in list(self._waiters.values()):
                    if not waiter.done():
                        waiter.set_exception(exc)
                self._waiters.clear()

    async def infer(self, bgr: np.ndarray, source_id: str, jpeg: bytes | None = None) -> dict:
        started = time.monotonic()
        if self.writer is None:
            raise RuntimeError("人体检测桥接进程未连接")
        sequence = self.sequence.get(source_id, 0) + 1
        self.sequence[source_id] = sequence
        frame_id = _frame_id(source_id, sequence)
        input_width, input_height = 960, 544
        original_height, original_width = bgr.shape[:2]
        preprocess_started = time.monotonic()
        if jpeg is None:
            loop = asyncio.get_running_loop()
            raw_bgr = await loop.run_in_executor(
                _BODY_PREPROCESS_EXECUTOR,
                _prepare_body_frame,
                bgr,
                input_width,
                input_height,
            )
            frame_encoding = 1
        else:
            raw_bgr = jpeg
            frame_encoding = 0
        preprocess_ms = (time.monotonic() - preprocess_started) * 1000
        loop = asyncio.get_running_loop()
        result = self._pending.pop(frame_id, None)
        if result is None:
            waiter = loop.create_future()
            self._waiters[frame_id] = waiter
            payload = _encode_packet(source_id, sequence, raw_bgr, frame_encoding)
            send_started = time.monotonic()
            try:
                async with self._write_lock:
                    self.writer.write(struct.pack("!I", len(payload)) + payload)
                    await self.writer.drain()
                send_ms = (time.monotonic() - send_started) * 1000
                wait_started = time.monotonic()
                result = await asyncio.wait_for(waiter, BODY_RESULT_TIMEOUT)
                wait_ms = (time.monotonic() - wait_started) * 1000
            except asyncio.TimeoutError as exc:
                self._waiters.pop(frame_id, None)
                raise TimeoutError(f"人体检测推理超时 frame_id={frame_id}") from exc
        else:
            send_ms = 0.0
            wait_ms = 0.0
        # This is the end-to-end bridge latency (JPEG encode, ROS transport,
        # BPU inference, post-processing, and result transport).
        result["latency_ms"] = (time.monotonic() - started) * 1000
        result["preprocess_ms"] = round(preprocess_ms, 2)
        result["send_ms"] = round(send_ms, 2)
        result["wait_ms"] = round(wait_ms, 2)
        _scale_result(result, original_width, original_height, input_width, input_height)
        tracker = self._trackers.setdefault(source_id, PerCameraTracker())
        tracker.assign(result.get("detections", []))
        return result

    async def infer_nv12(self, nv12: np.ndarray, source_id: str) -> dict:
        """Submit the camera's hardware-decoded NV12 frame directly to TROS."""

        if nv12.ndim != 2 or nv12.shape[0] % 3 != 0:
            raise ValueError("人体检测 NV12 帧尺寸无效")
        height = nv12.shape[0] * 2 // 3
        width = nv12.shape[1]
        started = time.monotonic()
        sequence = self.sequence.get(source_id, 0) + 1
        self.sequence[source_id] = sequence
        frame_id = _frame_id(source_id, sequence)
        source_height = height
        source_width = width
        prepared = nv12
        preprocess_ms = 0.0
        target_height = source_height
        target_width = source_width
        payload = _encode_packet(
            source_id,
            sequence,
            struct.pack("!II", target_width, target_height) + prepared.tobytes(),
            encoding=2,
        )
        waiter = loop.create_future()
        self._waiters[frame_id] = waiter
        send_started = time.monotonic()
        try:
            async with self._write_lock:
                self.writer.write(struct.pack("!I", len(payload)) + payload)
                await self.writer.drain()
            send_ms = (time.monotonic() - send_started) * 1000
            wait_started = time.monotonic()
            result = await asyncio.wait_for(waiter, BODY_RESULT_TIMEOUT)
            wait_ms = (time.monotonic() - wait_started) * 1000
        except asyncio.TimeoutError as exc:
            self._waiters.pop(frame_id, None)
            raise TimeoutError(f"人体检测推理超时 frame_id={frame_id}") from exc
        result["latency_ms"] = (time.monotonic() - started) * 1000
        result["preprocess_ms"] = round(preprocess_ms, 2)
        result["send_ms"] = round(send_ms, 2)
        result["wait_ms"] = round(wait_ms, 2)
        tracker = self._trackers.setdefault(source_id, PerCameraTracker())
        tracker.assign(result.get("detections", []))
        _scale_result(result, source_width, source_height, target_width, target_height)
        return result

    async def close(self):
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        for waiter in list(self._waiters.values()):
            if not waiter.done():
                waiter.set_exception(RuntimeError("人体检测桥接进程已关闭"))
        self._waiters.clear()
        self._pending.clear()
        self._trackers.clear()
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.writer = self.reader = self.process = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--camera-id", default="body")
    parser.add_argument("--model", default=BODY_MODEL)
    args = parser.parse_args()
    if args.worker:
        worker_main(args)
