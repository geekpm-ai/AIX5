"""MQTT discovery and retained state, with replay after broker reconnects."""

from __future__ import annotations

import json
import logging
import threading
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

log = logging.getLogger("aix5.mqtt")


class Publisher:
    def __init__(self):
        self.client = None
        self.cfg = {}
        self.device_id = None
        self.last_state = {}
        self._sent_state = {}
        self._discovery = None
        self._snapshots = {}
        self._snapshot_sent = {}
        self._connected = threading.Event()
        self._lock = threading.RLock()
        self.connection_error = None
        self._pending_removals = set()

    def close(self):
        with self._lock:
            client = self.client
            if client is None:
                return
            info = None
            if self._connected.is_set():
                info = client.publish(
                    f"aix5/{self.device_id}/availability", "offline", qos=1, retain=True
                )
            self.client = None
            self._connected.clear()
        if info is not None:
            try:
                info.wait_for_publish(timeout=2)
            except Exception:
                log.warning("MQTT 离线状态未确认送达")
        try:
            client.disconnect()
        finally:
            client.loop_stop()

    def configure(self, cfg, device_id):
        self.close()
        self.cfg = dict(cfg)
        self.device_id = device_id
        self.connection_error = None
        self._connected.clear()
        with self._lock:
            self._sent_state.clear()
            self._snapshot_sent.clear()
        if not cfg.get("enabled") or mqtt is None:
            return False
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"aix5-{device_id}"
        )
        if cfg.get("username"):
            client.username_pw_set(cfg["username"], cfg.get("password", ""))
        if cfg.get("tls"):
            client.tls_set()
        avail = f"aix5/{device_id}/availability"
        client.will_set(avail, "offline", qos=1, retain=True)

        def on_connect(client, userdata, flags, reason_code, properties=None):
            with self._lock:
                if client is not self.client:
                    return
                if reason_code != 0:
                    self.connection_error = str(reason_code)
                    self._connected.clear()
                    return
                self._connected.set()
                self.connection_error = None
                self._sent_state.clear()
                self._snapshot_sent.clear()
                client.publish(avail, "online", qos=1, retain=True)
                self._emit_discovery()
                for (cid, label), payload in self.last_state.items():
                    self._send_state(cid, label, payload)
                for cid, image in self._snapshots.items():
                    self._send_snapshot(cid, image)

        def on_disconnect(client, userdata, flags, reason_code, properties=None):
            if client is self.client:
                self._connected.clear()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        self.client = client
        try:
            client.connect_async(cfg["host"], int(cfg.get("port", 1883)))
            client.loop_start()
            return True
        except Exception as exc:
            self.connection_error = str(exc)
            self.close()
            return False

    def connection_status(self, timeout=5):
        if self._connected.wait(timeout):
            return {"ok": True, "message": "MQTT Broker 已连接"}
        return {
            "ok": False,
            "message": "MQTT 连接失败："
            + (self.connection_error or "连接超时，请检查地址、端口和认证配置"),
        }

    def _config_topics(self, cid, prefix):
        return [f"{prefix}/camera/aix5_{cid}/config"] + [
            f"{prefix}/binary_sensor/aix5_{cid}_{label}/config"
            for label in ("person", "pet", "online")
        ]

    def publish_discovery(self, cameras, device_id, base_url, prefix="homeassistant"):
        with self._lock:
            current = {c["id"] for c in cameras}
            if self._discovery:
                old, _, _, old_prefix = self._discovery
                for c in old:
                    if c["id"] not in current:
                        self.remove_camera(c["id"], old_prefix)
            self._discovery = (
                [{"id": c["id"], "name": c["name"]} for c in cameras],
                device_id,
                base_url,
                prefix,
            )
            self._emit_discovery()

    def remove_camera(self, cid, prefix="homeassistant"):
        with self._lock:
            self._pending_removals.add((cid, prefix))
            if self.client and self._connected.is_set():
                for topic in self._config_topics(cid, prefix):
                    self.client.publish(topic, b"", qos=1, retain=True)
                for label in ("person", "pet", "online", "last_event"):
                    self.client.publish(
                        f"aix5/{self.device_id}/{cid}/state/{label}",
                        b"",
                        qos=1,
                        retain=True,
                    )
                self.client.publish(
                    f"aix5/{self.device_id}/{cid}/snapshot", b"", qos=1, retain=True
                )
                self._pending_removals.discard((cid, prefix))
            self.last_state = {k: v for k, v in self.last_state.items() if k[0] != cid}
            self._sent_state = {
                k: v for k, v in self._sent_state.items() if k[0] != cid
            }
            self._snapshots.pop(cid, None)
            self._snapshot_sent.pop(cid, None)

    def _emit_discovery(self):
        if not self.client or not self._connected.is_set() or not self._discovery:
            return
        for cid, removed_prefix in list(self._pending_removals):
            self.remove_camera(cid, removed_prefix)
        cameras, device_id, base_url, prefix = self._discovery
        for camera in cameras:
            cid = camera["id"]
            device = {
                "identifiers": [f"aix5_{device_id}"],
                "name": "AIX5 AI 视频分析",
                "manufacturer": "D-Robotics / AIX5",
                "model": "RDK X5",
            }
            common = {
                "device": device,
                "availability_topic": f"aix5/{device_id}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
            }
            config = {
                **common,
                "name": camera["name"],
                "unique_id": f"aix5_{cid}_snapshot",
                "topic": f"aix5/{device_id}/{cid}/snapshot",
            }
            self.client.publish(
                f"{prefix}/camera/aix5_{cid}/config",
                json.dumps(config, ensure_ascii=False),
                qos=1,
                retain=True,
            )
            for label in ("person", "pet", "online"):
                config = {
                    **common,
                    "name": f"{camera['name']} {label}",
                    "unique_id": f"aix5_{cid}_{label}",
                    "state_topic": f"aix5/{device_id}/{cid}/state/{label}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                }
                if label in ("person", "online"):
                    config["device_class"] = {
                        "person": "occupancy",
                        "online": "connectivity",
                    }[label]
                self.client.publish(
                    f"{prefix}/binary_sensor/aix5_{cid}_{label}/config",
                    json.dumps(config, ensure_ascii=False),
                    qos=1,
                    retain=True,
                )

    def _send_state(self, cid, label, payload):
        key = (cid, label)
        if self._sent_state.get(key) == payload:
            return
        info = self.client.publish(
            f"aix5/{self.device_id}/{cid}/state/{label}", payload, qos=1, retain=True
        )
        if info.rc == 0:
            self._sent_state[key] = payload

    def publish_state(self, device_id, camera_id, state):
        with self._lock:
            for label, value in state.items():
                payload = "ON" if value else "OFF"
                self.last_state[(camera_id, label)] = payload
                if self.client and self._connected.is_set():
                    self._send_state(camera_id, label, payload)

    def _send_snapshot(self, cid, image):
        info = self.client.publish(
            f"aix5/{self.device_id}/{cid}/snapshot", image, qos=1, retain=True
        )
        if info.rc == 0:
            self._snapshot_sent[cid] = time.monotonic()

    def snapshot(self, cid, image):
        with self._lock:
            self._snapshots[cid] = image
            if (
                self.client
                and self._connected.is_set()
                and time.monotonic() - self._snapshot_sent.get(cid, -1e20) >= 5
            ):
                self._send_snapshot(cid, image)

    def event(self, device_id, camera, event, base_url):
        with self._lock:
            if not self.client or not self._connected.is_set():
                return
            payload = {
                k: v
                for k, v in event.items()
                if k not in ("image_path", "size", "created")
            }
            self.client.publish(
                f"aix5/{device_id}/{camera['id']}/event",
                json.dumps(payload, ensure_ascii=False),
                qos=1,
            )
            self.client.publish(
                f"aix5/{device_id}/{camera['id']}/state/last_event",
                event.get("timestamp", ""),
                qos=1,
                retain=True,
            )


publisher = Publisher()
