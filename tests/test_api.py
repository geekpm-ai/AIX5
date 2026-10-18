import asyncio
import base64
import threading
import time
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest

from aix5_ai import main, webrtc
from aix5_ai.hw_capture import HardwareManager, HardwareCapture
from aix5_ai.mqtt import Publisher
from aix5_ai.rules import RuleEngine
from aix5_ai.store import Store


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # API unit tests may run on the live board: never bind its discovery/RTSP
    # ports or start publishers with temporary credentials.
    monkeypatch.setattr(main.onvif_gateway, "start_discovery", AsyncMock())
    monkeypatch.setattr(main.RtspOutputManager, "start", AsyncMock())
    monkeypatch.setattr(main, "hbm_runtime", None)
    monkeypatch.setattr(main, "inference_executor", None)
    monkeypatch.setattr(main, "Store", lambda: Store(tmp_path / "app.db"))
    manager = HardwareManager()
    monkeypatch.setattr(main, "manager", manager)
    monkeypatch.setattr(webrtc, "manager", manager)
    monkeypatch.setattr(main, "engine", RuleEngine())
    monkeypatch.setattr(main, "publisher", Publisher())
    monkeypatch.setattr(main, "tasks", {})
    monkeypatch.setattr(main, "inference_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(main, "camera_lock", asyncio.Lock())
    monkeypatch.setattr(main, "EVENTS", tmp_path)

    async def idle_capture(self):
        self.set_status("running")
        await asyncio.Event().wait()

    monkeypatch.setattr(HardwareCapture, "read_once", idle_capture)
    await main.startup()
    password = (
        (tmp_path / "initial-admin.txt").read_text().splitlines()[1].split("：", 1)[1]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as session:
        session.initial_password = password
        yield session
    await main.shutdown()


async def login(client):
    result = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": client.initial_password},
    )
    assert result.status_code == 200
    return result


async def test_auth_cookie_password_change_and_logout(client):
    assert (await client.get("/api/cameras")).status_code == 401
    result = await login(client)
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "session" not in result.json()
    old = client.cookies.get("aix5_session")
    assert (
        await client.post(
            "/api/auth/password",
            json={"current_password": "wrong", "new_password": "new-password"},
        )
    ).status_code == 400
    result = await client.post(
        "/api/auth/password",
        json={
            "current_password": client.initial_password,
            "new_password": "new-password",
        },
    )
    assert result.status_code == 200
    assert client.cookies.get("aix5_session") != old
    assert not main.store.valid_session(old)
    assert (await client.get("/api/cameras")).status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 200
    assert (await client.get("/api/cameras")).status_code == 401
    assert (
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": client.initial_password},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"username": "admin", "password": "new-password"}
        )
    ).status_code == 200


async def test_onvif_services_use_account_and_subscription_action(client):
    await login(client)
    envelope = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><td:GetCapabilities xmlns:td="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>'
    unauthorized = await client.post("/onvif/device_service", content=envelope)
    assert unauthorized.status_code == 401
    credentials = base64.b64encode(f"admin:{client.initial_password}".encode()).decode()
    response = await client.post(
        "/onvif/device_service",
        content=envelope,
        headers={"Authorization": f"Basic {credentials}"},
    )
    assert response.status_code == 200
    assert "GetCapabilitiesResponse" in response.text
    wsse = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"><s:Header><wsse:Security><wsse:UsernameToken><wsse:Username>admin</wsse:Username><wsse:Password Type="...#PasswordDigest">arbitrary-digest</wsse:Password><wsse:Nonce>arbitrary-nonce</wsse:Nonce><wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">2026-10-18T00:00:00Z</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header><s:Body><td:GetCapabilities xmlns:td="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>'
    digest_response = await client.post(
        "/onvif/device_service", content=wsse
    )
    assert digest_response.status_code == 401
    # Existing hash-only accounts are provisioned by a verified web login.
    main.store.set("onvif_credential", None)
    await login(client)
    assert main.store.onvif_password() == client.initial_password
    from lxml import etree
    from zeep.wsse.username import UsernameToken

    signed = etree.fromstring(envelope.encode())
    UsernameToken("admin", client.initial_password, use_digest=True).apply(signed, {})
    valid_digest = await client.post("/onvif/device_service", content=etree.tostring(signed))
    assert valid_digest.status_code == 200
    replayed = await client.post("/onvif/device_service", content=etree.tostring(signed))
    assert replayed.status_code == 401


async def test_camera_patch_enable_disable_and_config_export(client):
    await login(client)
    response = await client.post(
        "/api/cameras",
        json={
            "name": "c",
            "rtsp_url": "rtsp://user:secret@localhost/test",
            "enabled": False,
        },
    )
    cid = response.json()["id"]
    assert response.json()["status"] == "disabled"
    assert main.manager.workers[cid].task is None
    assert (
        await client.patch("/api/cameras/" + cid, json={"enabled": True})
    ).status_code == 200
    await asyncio.sleep(0)
    assert main.manager.cameras[cid]["status"] == "running"
    main.manager.cameras[cid]["last_frame"] = b"not-serializable"
    assert (
        await client.patch(
            "/api/cameras/" + cid + "/labels",
            json={"enabled_labels": ["person", "car"]},
        )
    ).status_code == 200
    assert (
        await client.patch("/api/cameras/" + cid, json={"enabled": False})
    ).status_code == 200
    assert main.manager.workers[cid].task is None
    assert (await client.post("/api/cameras/" + cid + "/reconnect")).status_code == 409
    exported = await client.get("/api/settings/export")
    assert exported.status_code == 200
    assert "secret" not in exported.text
    assert "last_frame" not in exported.text
    assert (await client.delete("/api/cameras/" + cid)).status_code == 200
    assert not main.manager.cameras


@pytest.mark.parametrize(
    "body",
    [
        {"name": "c", "rtsp_url": ""},
        {"name": "c", "rtsp_url": "rtsp://localhost", "enabled_labels": ["fire"]},
        {"name": "c", "rtsp_url": "rtsp://localhost:99999/test"},
    ],
)
async def test_camera_validation(client, body):
    await login(client)
    assert (await client.post("/api/cameras", json=body)).status_code == 422


async def test_partial_patch_validates_merged_config(client):
    await login(client)
    response = await client.post(
        "/api/cameras",
        json={"name": "c", "rtsp_url": "rtsp://localhost", "enabled": False},
    )
    cid = response.json()["id"]
    for patch in [{"preview_fps": 0}, {"name": None}, {"rtsp_url": ""}]:
        assert (
            await client.patch("/api/cameras/" + cid, json=patch)
        ).status_code == 422
    result = await client.patch("/api/cameras/" + cid, json={"name": "renamed"})
    assert result.status_code == 200
    assert result.json()["rtsp_url"] == "rtsp://localhost"


async def test_system_status_never_returns_mqtt_password(client):
    await login(client)
    settings = main.store.get("settings")
    settings["mqtt"]["password"] = "private-broker-password"
    main.store.set("settings", settings)
    for url in ["/api/settings", "/api/system/status", "/api/settings/export"]:
        assert "private-broker-password" not in (await client.get(url)).text


async def test_inference_retries_and_observes_camera_model_change(client, monkeypatch):
    await login(client)
    cid = (
        await client.post(
            "/api/cameras", json={"name": "c", "rtsp_url": "rtsp://localhost"}
        )
    ).json()["id"]
    await asyncio.sleep(0)
    called = []

    class Detector:
        def __init__(self, model_id):
            self.model_id = model_id

        def infer(self, image):
            called.append(self.model_id)
            return {"latency_ms": 1, "detections": []}

    monkeypatch.setattr(main, "get_detector", Detector)
    image = np.zeros((16, 16, 3), np.uint8)
    await main.manager.on_frame(main.manager.cameras[cid], None, image)

    async def wait_calls(count):
        while len(called) < count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_calls(1), 2)
    assert called == ["yolo11n"]
    await client.patch("/api/cameras/" + cid, json={"model_id": "yolo11m"})
    await asyncio.sleep(0.21)
    await main.manager.on_frame(main.manager.cameras[cid], None, image)
    await asyncio.wait_for(wait_calls(2), 2)
    assert called[-1] == "yolo11m"


async def test_cancelled_bpu_thread_keeps_semaphore_until_finished():
    entered, release = threading.Event(), threading.Event()
    semaphore = asyncio.Semaphore(1)

    def blocking():
        entered.set()
        release.wait(timeout=2)

    async def work():
        async with semaphore:
            await main.run_worker(blocking)

    task = asyncio.create_task(work())
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    assert semaphore.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not semaphore.locked()


async def test_rule_event_uses_inference_frame_without_waiting_for_preview_cache(
    client, monkeypatch
):
    await login(client)
    cid = (
        await client.post(
            "/api/cameras", json={"name": "c", "rtsp_url": "rtsp://localhost"}
        )
    ).json()["id"]
    await asyncio.sleep(0)
    response = await client.post(
        f"/api/cameras/{cid}/rules",
        json={
            "name": "全图区域",
            "target_labels": ["person"],
            "min_hits": 1,
            "zone": [[0, 0], [1, 0], [1, 1], [0, 1]],
        },
    )
    assert response.status_code == 200

    class Detector:
        def infer(self, image):
            return {
                "latency_ms": 1,
                "detections": [
                    {"class_id": 0, "confidence": 0.9, "bbox": [2, 2, 10, 14]}
                ],
            }

    monkeypatch.setattr(main, "get_detector", lambda _: Detector())
    monkeypatch.setattr(main, "make_web_preview", lambda _: b"inference-frame")
    main.manager.frames[cid] = b"unrelated-preview"
    camera = main.manager.cameras[cid]
    main.manager.frame_queues[cid].put_nowait(
        (np.zeros((16, 16, 3), np.uint8), None, main.manager.generations[cid])
    )

    async def wait_event():
        while not main.store.events():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_event(), 2)
    event = main.store.events()[0]
    from pathlib import Path

    assert Path(event["image_path"]).read_bytes() == b"inference-frame"
    assert event["rule_id"] == response.json()["id"]
    assert camera["present"]["person"]


async def test_rule_sensor_lifecycle_and_name_only_change_preserve_active_state(client):
    await login(client)
    camera = await client.post(
        "/api/cameras", json={"name": "入口", "rtsp_url": "rtsp://localhost"}
    )
    cid = camera.json()["id"]
    await asyncio.sleep(0)
    sid = await main.onvif_events.subscribe()
    values = {"name": "有人或猫进入", "target_labels": ["person", "cat"], "min_hits": 1}
    response = await client.post(f"/api/cameras/{cid}/rules", json=values)
    assert response.status_code == 200
    rule = response.json()
    rid = rule["id"]
    added = await main.onvif_events.pull(sid, timeout=0)
    assert [(n.rule_id, n.property_operation, n.inside) for n in added] == [
        (rid, "Initialized", False)
    ]
    events, _ = main.engine.evaluate(
        cid, [{"label": "person", "confidence": 0.95, "bbox": [1, 1, 5, 5]}],
        [rule], time.time(), image_size=(10, 10),
    )
    assert len(events) == 1
    main.onvif_events.publish_trigger(cid, events[0])
    await main.onvif_events.pull(sid, timeout=0)
    active_before = main.engine.active_rules(cid)

    renamed = await client.patch(f"/api/rules/{rid}", json={**values, "name": "入口 <有人> & 猫"})
    assert renamed.status_code == 200
    changed = await main.onvif_events.pull(sid, timeout=0)
    assert len(changed) == 1
    assert changed[0].inside and changed[0].rule_id == rid
    assert changed[0].rule_name == "入口 <有人> & 猫"
    assert main.engine.active_rules(cid) == active_before

    disabled = await client.patch(
        f"/api/rules/{rid}", json={**values, "enabled": False}
    )
    assert disabled.status_code == 200
    cleared = await main.onvif_events.pull(sid, timeout=0)
    assert cleared and all(not n.inside and n.property_operation != "Deleted" for n in cleared)
    catalog = main.onvif_events.snapshot()["rules"]
    assert len(catalog) == 1 and not catalog[0]["inside"]
    assert (await client.delete(f"/api/rules/{rid}")).status_code == 200
    deleted = await main.onvif_events.pull(sid, timeout=0)
    assert len(deleted) == 1 and deleted[0].property_operation == "Deleted"
    assert not main.onvif_events.snapshot()["rules"]


async def test_camera_delete_removes_only_its_rule_sensors_and_analytics_is_authenticated(client):
    await login(client)
    camera_ids = []
    for name in ("门口", "后院"):
        result = await client.post(
            "/api/cameras", json={"name": name, "rtsp_url": "rtsp://localhost"}
        )
        cid = result.json()["id"]
        camera_ids.append(cid)
        assert (await client.post(
            f"/api/cameras/{cid}/rules", json={"name": name + "规则", "target_labels": ["person"]}
        )).status_code == 200
    sid = await main.onvif_events.subscribe()
    await main.onvif_events.pull(sid, timeout=0)
    assert (await client.delete(f"/api/cameras/{camera_ids[0]}")).status_code == 200
    deleted = await main.onvif_events.pull(sid, timeout=0)
    assert len(deleted) == 1 and deleted[0].property_operation == "Deleted"
    assert deleted[0].camera_id == camera_ids[0]
    assert [x["camera_id"] for x in main.onvif_events.snapshot()["rules"]] == camera_ids[1:]
    body = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://www.onvif.org/ver20/analytics/wsdl"><s:Body><a:GetRules>'
        f'<a:ConfigurationToken>aix5-analytics-{camera_ids[1]}</a:ConfigurationToken>'
        '</a:GetRules></s:Body></s:Envelope>'
    )
    assert (await client.post("/onvif/analytics_service", content=body)).status_code == 401
    response = await client.post(
        "/onvif/analytics_service", content=body, auth=("admin", client.initial_password)
    )
    assert response.status_code == 200 and "后院规则" in response.text


async def test_rule_condition_edit_during_event_persistence_cannot_publish_old_trigger(
    client, monkeypatch
):
    await login(client)
    cid = (await client.post(
        "/api/cameras", json={"name": "c", "rtsp_url": "rtsp://localhost"}
    )).json()["id"]
    values = {"name": "规则", "target_labels": ["person"], "min_hits": 1,
              "confidence_threshold": 0.5}
    rule = (await client.post(f"/api/cameras/{cid}/rules", json=values)).json()
    sid = await main.onvif_events.subscribe()
    await main.onvif_events.pull(sid, timeout=0)

    class Detector:
        def infer(self, image):
            return {"latency_ms": 1, "detections": [
                {"class_id": 0, "confidence": 0.9, "bbox": [2, 2, 10, 14]}
            ]}

    entered, release = threading.Event(), threading.Event()
    finished = asyncio.Event()
    original_insert = main.store.insert_event

    def persist(event, image):
        original_insert(event, image)
        entered.set()
        release.wait(timeout=3)

    monkeypatch.setattr(main, "get_detector", lambda _: Detector())
    monkeypatch.setattr(main, "make_web_preview", lambda _: b"frame")
    monkeypatch.setattr(main.store, "insert_event", persist)
    monkeypatch.setattr(main.publisher, "event", lambda *args: finished.set())
    main.manager.frame_queues[cid].put_nowait(
        (np.zeros((16, 16, 3), np.uint8), None, main.manager.generations[cid])
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        response = await client.patch(
            f"/api/rules/{rule['id']}",
            json={**values, "confidence_threshold": 0.99},
        )
        assert response.status_code == 200
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        notifications = await main.onvif_events.pull(sid, timeout=0)
        assert not any(item.inside for item in notifications)
        assert not main.onvif_events.snapshot()["active"]
    finally:
        release.set()


async def test_analysis_fps_is_not_raised_when_viewers_are_present(client, monkeypatch):
    await login(client)
    cid = (
        await client.post(
            "/api/cameras",
            json={"name": "c", "rtsp_url": "rtsp://localhost", "analysis_fps": 1},
        )
    ).json()["id"]
    await asyncio.sleep(0)
    calls = []

    class Detector:
        def infer(self, image):
            calls.append(1)
            return {"detections": []}

    monkeypatch.setattr(main, "get_detector", lambda _: Detector())
    monkeypatch.setattr(main, "peer_connections", {object()})
    for _ in range(5):
        main.manager._publish_analysis(
            main.manager.cameras[cid],
            None,
            np.zeros((16, 16, 3), np.uint8),
            time.monotonic(),
        )
        await asyncio.sleep(0.05)
    assert len(calls) == 1


async def test_camera_order_updates_preview_without_restarting_workers(client):
    await login(client)
    ids = []
    for index in range(3):
        response = await client.post(
            "/api/cameras",
            json={"name": f"camera-{index}", "rtsp_url": "rtsp://localhost"},
        )
        ids.append(response.json()["id"])
    await asyncio.sleep(0)
    workers = {cid: main.manager.workers[cid].task for cid in ids}
    reordered = [ids[2], ids[0], ids[1]]
    result = await client.put("/api/cameras/order", json={"camera_ids": reordered})
    assert result.status_code == 200
    assert [c["id"] for c in result.json()] == reordered
    assert [c["id"] for c in (await client.get("/api/cameras")).json()] == reordered
    assert [
        c["id"] for c in (await client.get("/api/system/status")).json()["cameras"]
    ] == reordered
    assert [c["id"] for c in main.store.cameras()] == reordered
    assert all(
        main.manager.workers[cid].task is workers[cid] and not workers[cid].done()
        for cid in ids
    )
    for invalid, status in [
        ([ids[0]], 409),
        ([ids[0], ids[0]], 422),
        ([ids[0], ids[1], "missing"], 409),
    ]:
        response = await client.put("/api/cameras/order", json={"camera_ids": invalid})
        assert response.status_code == status
    assert [c["id"] for c in main.store.cameras()] == reordered


async def test_camera_order_requires_authentication(client):
    assert (
        await client.put("/api/cameras/order", json={"camera_ids": []})
    ).status_code == 401


async def test_system_preferences_merge_and_survive_other_settings_updates(client):
    await login(client)
    original = (await client.get("/api/settings")).json()
    response = await client.patch(
        "/api/settings", json={"preview_resolution": "1920x1080", "retention_days": 30}
    )
    assert response.status_code == 200
    assert main.manager.preview_resolution == "1920x1080"
    stored = main.store.get("settings")
    assert stored["retention_days"] == 30
    assert stored["mqtt"] == original["mqtt"]
    assert stored["max_event_storage_mb"] == original["max_event_storage_mb"]
    assert (
        await client.patch(
            "/api/settings",
            json={
                "base_url": "http://device:8080",
                "mqtt": {"enabled": False, "host": "broker"},
            },
        )
    ).status_code == 200
    settings = (await client.get("/api/settings")).json()
    assert settings["preview_resolution"] == "1920x1080"
    assert settings["retention_days"] == 30
    for value in [3, 7, 30]:
        assert (
            await client.patch("/api/settings", json={"retention_days": value})
        ).status_code == 200
    for invalid in [
        {"retention_days": 1},
        {"retention_days": 31},
        {"preview_resolution": "1280x720"},
        {"preview_resolution": None},
    ]:
        assert (await client.patch("/api/settings", json=invalid)).status_code == 422


async def test_single_account_can_change_username_and_password_together(client):
    await login(client)
    account = (await client.get("/api/auth/account")).json()
    assert account == {"username": "admin", "single_user": True}
    old_session = client.cookies.get("aix5_session")
    assert (
        await client.patch(
            "/api/auth/account",
            json={
                "username": "new-admin",
                "new_password": "new-password",
                "current_password": "wrong",
            },
        )
    ).status_code == 400
    assert main.store.valid_session(old_session)
    response = await client.patch(
        "/api/auth/account",
        json={
            "username": "new-admin",
            "new_password": "new-password",
            "current_password": client.initial_password,
        },
    )
    assert response.status_code == 200
    assert response.json()["username"] == "new-admin"
    assert main.store.onvif_password() == "new-password"
    assert not main.store.valid_session(old_session)
    assert (await client.get("/api/cameras")).status_code == 200
    await client.post("/api/auth/logout")
    for username, password in [
        ("admin", client.initial_password),
        ("new-admin", client.initial_password),
        ("admin", "new-password"),
    ]:
        assert (
            await client.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
        ).status_code == 401
    assert (
        await client.post(
            "/api/auth/login",
            json={"username": "new-admin", "password": "new-password"},
        )
    ).status_code == 200
    assert (
        main.store.db.execute(
            "SELECT COUNT(*) FROM kv WHERE key='username'"
        ).fetchone()[0]
        == 1
    )


async def test_account_username_only_and_password_only_changes(client):
    await login(client)
    old_hash = main.store.get("password_hash")
    response = await client.patch(
        "/api/auth/account",
        json={"username": " renamed ", "current_password": client.initial_password},
    )
    assert response.status_code == 200
    assert main.store.get("username") == "renamed"
    assert main.store.get("password_hash") == old_hash
    assert main.store.onvif_password() == client.initial_password
    response = await client.patch(
        "/api/auth/account",
        json={
            "new_password": "changed-password",
            "current_password": client.initial_password,
        },
    )
    assert response.status_code == 200
    assert main.store.get("username") == "renamed"
    assert main.store.get("password_hash") != old_hash
    assert main.store.onvif_password() == "changed-password"


async def test_account_rejects_empty_username_and_missing_change(client):
    await login(client)
    for body in [{"username": "   "}, {"username": "name:part"}, {"new_password": "short"}, {}]:
        response = await client.patch(
            "/api/auth/account",
            json={**body, "current_password": client.initial_password},
        )
        assert response.status_code == 422


async def test_software_fallback_policy_is_explicit_and_defaults_off(client):
    await login(client)
    settings = (await client.get("/api/settings")).json()
    assert settings["allow_software_fallback"] is False
    assert main.manager.allow_software_fallback is False
    assert (
        await client.patch("/api/settings", json={"allow_software_fallback": True})
    ).status_code == 200
    assert main.manager.allow_software_fallback is True
    assert (
        await client.patch("/api/settings", json={"allow_software_fallback": False})
    ).status_code == 200
    assert main.manager.allow_software_fallback is False
    assert (await client.get("/api/audio/status")).status_code == 200


async def test_rule_options_use_camera_model_defaults_and_enabled_targets(client, monkeypatch):
    assert (await client.get("/api/rules")).status_code == 401
    assert (await client.get("/api/cameras/unknown/rule-options")).status_code == 401
    await login(client)
    monkeypatch.setitem(main.MODELS["yolo11m"], "confidence_threshold", 0.45)
    cameras = []
    for model, labels in (("yolo11n", ["person", "cat"]), ("yolo11m", ["car"])):
        response = await client.post("/api/cameras", json={
            "name": model, "rtsp_url": "rtsp://localhost/test", "enabled": False,
            "model_id": model, "enabled_labels": labels,
        })
        assert response.status_code == 200
        cameras.append(response.json())
    for camera, threshold, labels in zip(cameras, [0.35, 0.45], [["person", "cat"], ["car"]]):
        result = (await client.get(f"/api/cameras/{camera['id']}/rule-options")).json()
        assert result["model_id"] == camera["model_id"]
        assert result["model_name"] == main.MODELS[camera["model_id"]]["name"]
        assert result["confidence_threshold"] == result["default_confidence_threshold"] == threshold
        assert result["min_confidence_threshold"] == threshold
        assert [item["id"] for item in result["enabled_labels"]] == labels
        assert all(item["name"] for item in result["enabled_labels"])
        assert [event["requires_zone"] for event in result["event_types"]] == [False, True]
        created = await client.post(f"/api/cameras/{camera['id']}/rules", json={"name": "默认规则", "target_labels": labels})
        assert created.status_code == 200
        assert created.json()["confidence_threshold"] == threshold
        rejected = await client.post(f"/api/cameras/{camera['id']}/rules", json={"name": "无效低阈值", "target_labels": labels, "confidence_threshold": threshold - 0.1})
        assert rejected.status_code == 422
    assert (await client.get("/api/cameras/unknown/rule-options")).status_code == 404


async def test_global_rules_newest_first_and_partial_rename_preserves_legacy_conditions(client):
    await login(client)
    cid = (await client.post("/api/cameras", json={"name": "客厅", "rtsp_url": "rtsp://localhost/test", "enabled": False, "enabled_labels": ["person"]})).json()["id"]
    legacy = {
        "id": "legacy-rule", "camera_id": cid, "name": "原有规则", "enabled": True,
        "target_labels": ["cat"], "confidence_threshold": 0.2, "min_hits": 4,
        "clear_seconds": 7, "cooldown_seconds": 120, "zone": [[0, 0], [1, 0], [0, 1]],
    }
    main.store.save_rule(legacy)
    newer = (await client.post(f"/api/cameras/{cid}/rules", json={"name": "新规则", "target_labels": ["person"], "confidence_threshold": 0.6})).json()
    rename = await client.patch("/api/rules/legacy-rule", json={"name": "只改名称"})
    assert rename.status_code == 200, rename.text
    assert rename.json() == {**legacy, "name": "只改名称"}
    listing = (await client.get("/api/rules")).json()
    assert [r["id"] for r in listing] == [newer["id"], "legacy-rule"]
    assert all(r["camera_name"] == "客厅" for r in listing)
    assert listing[0]["confidence_threshold"] == 0.6
    for update in ({"confidence_threshold": 0.25}, {"min_hits": 0}, {"zone": [[0, 0]]}, {"name": None}, {"target_labels": ["cat", "dog"]}):
        response = await client.patch("/api/rules/legacy-rule", json=update)
        assert response.status_code == 422, response.text
    assert (await client.get("/api/rules")).json()[1]["confidence_threshold"] == 0.2
    assert (await client.patch(f"/api/rules/{newer['id']}", json={"name": "新规则改名"})).json()["confidence_threshold"] == 0.6


async def test_timezone_settings_persist_validate_and_do_not_rewrite_utc_events(client):
    await login(client)
    original_settings = (await client.get("/api/settings")).json()
    assert original_settings["timezone"] == "Asia/Shanghai"
    settings_without_timezone = main.store.get("settings")
    settings_without_timezone.pop("timezone")
    main.store.set("settings", settings_without_timezone)
    assert (await client.get("/api/settings")).json()["timezone"] == "Asia/Shanghai"
    entry = {"id": "timezone-event", "camera_id": "camera", "created": time.time(),
             "timestamp": "2026-10-18T08:00:00+00:00", "image_path": str(main.EVENTS / "timezone-event.jpg"), "size": 4}
    main.store.insert_event(entry, b"jpeg")
    original_event = main.store.event("timezone-event")
    account = (main.store.get("username"), main.store.get("password_hash"))
    for zone in ["UTC", "America/New_York", "Asia/Kathmandu", "Europe/London", "Etc/GMT+8", "Asia/Shanghai"]:
        response = await client.patch("/api/settings", json={"timezone": zone})
        assert response.status_code == 200, response.text
        assert main.store.get("settings")["timezone"] == zone
        assert (await client.get("/api/settings")).json()["timezone"] == zone
        assert main.store.event("timezone-event") == original_event
        assert (main.store.get("username"), main.store.get("password_hash")) == account
        assert (await client.get("/api/events")).json()[0]["timestamp"] == original_event["timestamp"]
        exported = await client.get("/api/events/export", params={"format": "json"})
        assert exported.json()[0]["timestamp"] == original_event["timestamp"]
        csv_export = await client.get("/api/events/export", params={"format": "csv"})
        assert original_event["timestamp"] in csv_export.text
        from xml.etree import ElementTree as ET
        from aix5_ai.onvif_gateway import TT
        utc_response = ET.fromstring(main.onvif_gateway.device("GetSystemDateAndTime", ""))
        utc = utc_response.find(f".//{{{TT}}}UTCDateTime")
        instant = main.datetime.datetime.now(main.datetime.timezone.utc)
        assert int(utc.findtext(f"{{{TT}}}Date/{{{TT}}}Year")) == instant.year
        assert int(utc.findtext(f"{{{TT}}}Time/{{{TT}}}Hour")) == instant.hour
    for zone in [None, "", "Not/A_Timezone", "../Asia/Shanghai", "/etc/localtime", "UTC+8", "Asia/Shanghai " , "x" * 65]:
        response = await client.patch("/api/settings", json={"timezone": zone})
        assert response.status_code == 422, response.text
        assert main.store.get("settings")["timezone"] == "Asia/Shanghai"
    updated = (await client.get("/api/settings")).json()
    assert updated == original_settings
