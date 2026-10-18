import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from aix5_ai.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "app.db")
    yield instance
    instance.close()


def test_existing_password_survives_upgrade_and_restart(tmp_path):
    path = tmp_path / "app.db"
    store = Store(path)
    password_hash = PasswordHasher().hash("existing-password")
    store.set("password_hash", password_hash)
    store.set("username", "existing-user")
    store.set("credential_version", 1)
    store.close()
    store = Store(path)
    assert store.get("username") == "existing-user"
    assert store.get("password_hash") == password_hash
    store.close()


def test_camera_persistence_excludes_all_runtime_state(store):
    store.save_camera(
        {
            "id": "c",
            "name": "camera",
            "rtsp_url": "rtsp://localhost/test",
            "last_frame": b"jpeg",
            "detection_payload": {"detections": []},
            "software_cooldown_until": 123,
            "force_software": True,
        }
    )
    assert store.camera("c") == {
        "id": "c",
        "name": "camera",
        "rtsp_url": "rtsp://localhost/test",
    }


def test_password_change_invalidates_all_sessions_and_removes_initial_file(
    store, tmp_path
):
    token = store.create_session()
    assert store.valid_session(token)
    assert (tmp_path / "initial-admin.txt").exists()
    store.change_password(PasswordHasher().hash("new-password"))
    assert not store.valid_session(token)
    assert not (tmp_path / "initial-admin.txt").exists()


def event(tmp_path, eid, target="person", created=None):
    return {
        "id": eid,
        "camera_id": "c",
        "created": created or time.time(),
        "event_type": "target_detected",
        "target_type": target,
        "image_path": str(tmp_path / f"{eid}.jpg"),
        "size": 4,
    }


def test_event_filters_apply_before_limit(store, tmp_path):
    store.insert_event(event(tmp_path, "old-person", created=time.time() - 1), b"jpeg")
    store.insert_event(event(tmp_path, "new-cat", target="cat"), b"jpeg")
    assert [e["id"] for e in store.events(limit=1, target_type="person")] == [
        "old-person"
    ]


def test_event_write_is_atomic_and_rollback_cleans_image(store, tmp_path):
    entry = event(tmp_path, "e")
    store.insert_event(entry, b"jpeg")
    assert Path(entry["image_path"]).read_bytes() == b"jpeg"
    bad = event(tmp_path, "bad")
    bad["not_json"] = object()
    with pytest.raises(TypeError):
        store.insert_event(bad, b"jpeg")
    assert not Path(bad["image_path"]).exists()
    assert not list(tmp_path.glob(".event-*"))
    assert store.event("bad") is None


def test_retention_cleans_oldest_images_to_low_watermark(store, tmp_path):
    for i in range(3):
        store.insert_event(event(tmp_path, str(i), created=time.time() - i), b"jpeg")
    store.purge(7, 9)
    remaining = store.events()
    assert len(remaining) == 2
    assert {e["id"] for e in remaining} == {"0", "1"}
    assert not (tmp_path / "2.jpg").exists()


def test_duplicate_event_never_deletes_original_image(store, tmp_path):
    entry = event(tmp_path, "e")
    store.insert_event(entry, b"original")
    with pytest.raises(FileExistsError):
        store.insert_event(entry, b"replacement")
    assert Path(entry["image_path"]).read_bytes() == b"original"
    assert store.event("e") is not None


def test_camera_order_persists_and_new_devices_append(tmp_path):
    path = tmp_path / "order.db"
    store = Store(path)
    for cid in ["a", "b", "c"]:
        store.save_camera({"id": cid, "name": cid, "rtsp_url": "rtsp://localhost"})
    store.reorder_cameras(["c", "a", "b"])
    store.close()
    store = Store(path)
    try:
        assert [c["id"] for c in store.cameras()] == ["c", "a", "b"]
        store.delete_camera("a")
        store.save_camera({"id": "d", "name": "d", "rtsp_url": "rtsp://localhost"})
        assert [c["id"] for c in store.cameras()] == ["c", "b", "d"]
        assert store.get("camera_order") == ["c", "b"]
    finally:
        store.close()


def test_invalid_camera_order_cannot_change_saved_order(store):
    for cid in ["a", "b"]:
        store.save_camera({"id": cid, "name": cid})
    store.reorder_cameras(["b", "a"])
    for order in [["a"], ["a", "a"], ["a", "unknown"]]:
        with pytest.raises(ValueError):
            store.reorder_cameras(order)
    assert [c["id"] for c in store.cameras()] == ["b", "a"]


def test_legacy_settings_get_resolution_default_without_losing_configuration(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(path)
    settings = store.get("settings")
    settings.pop("preview_resolution")
    settings["retention_days"] = 30
    settings["mqtt"]["password"] = "keep-this"
    store.set("settings", settings)
    store.close()
    store = Store(path)
    try:
        actual = store.get("settings")
        assert actual["preview_resolution"] == "960x540"
        assert actual["retention_days"] == 30
        assert actual["mqtt"]["password"] == "keep-this"
    finally:
        store.close()


def test_retention_days_apply_to_event_records_and_images(store, tmp_path):
    for days in [2, 5, 15, 31]:
        store.insert_event(
            event(tmp_path, f"{days}-days", created=time.time() - days * 86400), b"jpeg"
        )
    store.purge(30, 100000)
    assert {item["id"] for item in store.events()} == {"2-days", "5-days", "15-days"}
    assert not (tmp_path / "31-days.jpg").exists()
    store.purge(7, 100000)
    assert {item["id"] for item in store.events()} == {"2-days", "5-days"}
    assert not (tmp_path / "15-days.jpg").exists()
    store.purge(3, 100000)
    assert {item["id"] for item in store.events()} == {"2-days"}
    assert not (tmp_path / "5-days.jpg").exists()


def test_rule_creation_order_survives_edits_deletion_and_restart(tmp_path):
    path = tmp_path / "rules.db"
    store = Store(path)
    store.save_camera({"id": "cam", "name": "camera"})
    for rid in ("oldest", "middle", "newest"):
        store.save_rule({"id": rid, "camera_id": "cam", "name": rid})
    store.save_rule({"id": "oldest", "camera_id": "cam", "name": "renamed"})
    assert [r["id"] for r in store.rules(newest_first=True)] == ["newest", "middle", "oldest"]
    store.delete_rule("middle")
    store.close()
    store = Store(path)
    try:
        assert [r["id"] for r in store.rules("cam", newest_first=True)] == ["newest", "oldest"]
        assert [r["id"] for r in store.rules("cam")] == ["oldest", "newest"]
    finally:
        store.close()


def test_legacy_timezone_default_preserves_account_cameras_rules_and_event_times(tmp_path):
    path = tmp_path / "timezone.db"
    store = Store(path)
    settings = store.get("settings")
    settings.pop("timezone")
    settings["preview_resolution"] = "1920x1080"
    settings["retention_days"] = 30
    store.set("settings", settings)
    store.save_camera({"id": "cam", "name": "客厅"})
    store.save_rule({"id": "rule", "camera_id": "cam", "name": "原有规则"})
    original_event = {**event(tmp_path, "utc-event", created=1770000000), "timestamp": "2026-02-02T02:40:00+00:00"}
    store.insert_event(original_event, b"jpeg")
    username, password_hash = store.get("username"), store.get("password_hash")
    cameras, rules, events = store.cameras(), store.rules(), store.events()
    store.close()
    store = Store(path)
    try:
        assert store.get("settings") == {**settings, "timezone": "Asia/Shanghai"}
        assert store.get("username") == username
        assert store.get("password_hash") == password_hash
        assert store.cameras() == cameras and store.rules() == rules
        assert store.events() == events
        store.set("settings", {**store.get("settings"), "timezone": "America/New_York"})
    finally:
        store.close()
    store = Store(path)
    try:
        assert store.get("settings")["timezone"] == "America/New_York"
        assert store.events() == events
    finally:
        store.close()
