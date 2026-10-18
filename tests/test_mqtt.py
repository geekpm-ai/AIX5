import json
from types import SimpleNamespace

from aix5_ai import mqtt


class Client:
    def __init__(self, *args, **kwargs):
        self.messages = []

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload, kwargs))
        return SimpleNamespace(rc=0, wait_for_publish=lambda timeout: None)

    def will_set(self, *args, **kwargs):
        pass

    def connect_async(self, *args, **kwargs):
        pass

    def loop_start(self):
        pass

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


def connect(monkeypatch):
    monkeypatch.setattr(mqtt.mqtt, "Client", Client)
    publisher = mqtt.Publisher()
    publisher.publish_discovery([{"id": "c", "name": "摄像头"}], "d", "http://device")
    publisher.configure({"enabled": True, "host": "broker", "port": 1883}, "d")
    return publisher, publisher.client


def test_reconnect_replays_latest_retained_state_and_snapshot(monkeypatch):
    publisher, client = connect(monkeypatch)
    publisher.publish_state("d", "c", {"online": True, "person": True})
    publisher.snapshot("c", b"jpeg")
    assert not client.messages
    client.on_connect(client, None, None, 0)
    assert any(
        topic.endswith("/snapshot") and payload == b"jpeg"
        for topic, payload, _ in client.messages
    )
    assert any(
        topic.endswith("/state/person") and payload == "ON"
        for topic, payload, _ in client.messages
    )
    camera_config = next(
        json.loads(payload)
        for topic, payload, _ in client.messages
        if "/camera/" in topic
    )
    assert "device_class" not in camera_config
    client.on_disconnect(client, None, None, 1)
    publisher.publish_state("d", "c", {"online": False, "person": False})
    client.messages.clear()
    client.on_connect(client, None, None, 0)
    assert any(
        topic.endswith("/state/online") and payload == "OFF"
        for topic, payload, _ in client.messages
    )
    assert any(
        topic.endswith("/state/person") and payload == "OFF"
        for topic, payload, _ in client.messages
    )


def test_deleting_camera_while_disconnected_removes_discovery_after_reconnect(
    monkeypatch,
):
    publisher, client = connect(monkeypatch)
    publisher.publish_discovery([], "d", "http://device")
    client.on_connect(client, None, None, 0)
    removed = [
        (topic, payload) for topic, payload, _ in client.messages if payload == b""
    ]
    assert len([topic for topic, _ in removed if topic.endswith("/config")]) == 4


def test_rejected_broker_connection_is_not_reported_as_success(monkeypatch):
    publisher, client = connect(monkeypatch)
    client.on_connect(client, None, None, 5)
    assert not publisher.connection_status(timeout=0)["ok"]
    assert not client.messages
