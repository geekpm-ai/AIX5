"""Rules are ONVIF properties; labels are internal activation contributors."""

import xml.etree.ElementTree as ET

import pytest

from aix5_ai.onvif_events import TT, OnvifEventHub, rule_target_token
from aix5_ai.rules import RuleEngine


def rule(**values):
    return {
        "id": "r1",
        "name": "客厅目标",
        "enabled": True,
        "target_labels": ["person", "cat"],
        "confidence_threshold": 0.55,
        "min_hits": 1,
        "cooldown_seconds": 60,
        "clear_seconds": 3,
        "zone": [],
        **values,
    }


def trigger(target, rid="r1", event_id=None):
    return {
        "rule_id": rid,
        "rule_name": "客厅目标",
        "target_type": target,
        "event_id": event_id,
    }


async def messages(hub, sid):
    return await hub.pull(sid, timeout=0, limit=100)


def source_token(notification):
    xml = notification.message_element()
    return xml.find(f".//{{{TT}}}Source/{{{TT}}}SimpleItem[@Name='Rule']").get("Value")


@pytest.mark.asyncio
async def test_rule_targets_aggregate_until_last_formal_target_clears():
    hub = OnvifEventHub()
    hub.register_rule("camera", rule())
    sid = await hub.subscribe()
    initial = await messages(hub, sid)
    assert len(initial) == 1
    assert initial[0].inside is False
    assert source_token(initial[0]) == "r1"

    assert hub.publish_trigger("camera", trigger("person", event_id="event-person"))
    assert hub.publish_trigger("camera", trigger("cat", event_id="event-cat"))
    assert not hub.publish_trigger("camera", trigger("cat"))
    edges = await messages(hub, sid)
    assert len(edges) == 1
    assert edges[0].inside is True
    assert edges[0].event_id == "event-person"
    assert len(hub.active) == 2  # The RuleEngine still has two target episodes.
    assert len(hub.snapshot()["active"]) == 1
    assert hub.snapshot()["rules"][0]["active_targets"] == ["cat", "person"]

    hub.clear_key("camera", "r1", "person")
    assert await messages(hub, sid) == []
    assert hub.snapshot()["rules"][0]["inside"] is True
    hub.clear_key("camera", "r1", "cat")
    edges = await messages(hub, sid)
    assert len(edges) == 1 and edges[0].inside is False
    assert edges[0].event_id == "event-cat"
    assert source_token(edges[0]) == "r1"


@pytest.mark.asyncio
async def test_dynamic_rule_lifecycle_preserves_uuid_and_emits_deleted():
    hub = OnvifEventHub()
    sid = await hub.subscribe()
    original = rule()
    assert hub.register_rule("camera", original, camera_name="客厅")
    added = await messages(hub, sid)
    assert len(added) == 1
    assert added[0].property_operation == "Initialized"
    assert added[0].rule_name == "客厅目标"
    assert not added[0].inside
    assert not hub.register_rule("camera", original, camera_name="客厅")
    assert await messages(hub, sid) == []

    hub.publish_trigger("camera", trigger("person"))
    await messages(hub, sid)
    renamed = {**original, "name": "门口 & <有人>"}
    hub.register_rule("camera", renamed, camera_name="客厅")
    updated = await messages(hub, sid)
    assert len(updated) == 1 and updated[0].property_operation == "Changed"
    assert updated[0].inside  # A rename must not create a new false/true episode.
    assert source_token(updated[0]) == source_token(added[0])
    assert updated[0].rule_name == "门口 & <有人>"
    ET.fromstring(ET.tostring(updated[0].message_element()))
    assert len(hub.snapshot()["rules"]) == 1

    hub.remove_rule("camera", "r1")
    deleted = await messages(hub, sid)
    assert len(deleted) == 1
    assert deleted[0].property_operation == "Deleted"
    assert not deleted[0].inside
    assert source_token(deleted[0]) == "r1"
    assert not hub.active and not hub.known
    await hub.synchronize(sid)
    assert await messages(hub, sid) == []
    assert not hub.publish_trigger("camera", trigger("person"))


@pytest.mark.asyncio
async def test_disabled_rules_and_cameras_keep_one_false_catalog_property():
    hub = OnvifEventHub()
    hub.replace_catalog(
        [
            {"camera_id": "camera", **rule(id="disabled", enabled=False)},
            {"camera_id": "disabled-camera", "camera_enabled": False, **rule(id="r2")},
        ]
    )
    sid = await hub.subscribe()
    initial = await messages(hub, sid)
    assert len(initial) == 2
    assert all(item.property_operation == "Initialized" for item in initial)
    assert all(not item.inside and not item.enabled for item in initial)
    assert not hub.publish_trigger("camera", trigger("person", rid="disabled"))
    assert not hub.publish_trigger("disabled-camera", trigger("cat", rid="r2"))
    assert len(hub.snapshot()["rules"]) == 2


@pytest.mark.asyncio
async def test_disabling_target_changes_and_camera_deletion_clear_correct_rule():
    hub = OnvifEventHub()
    first = rule()
    hub.register_rule("camera", first)
    hub.register_rule("camera", rule(id="r2", target_labels=["person"]))
    sid = await hub.subscribe()
    await messages(hub, sid)
    for target in ["person", "cat"]:
        hub.publish_trigger("camera", trigger(target))
    hub.publish_trigger("camera", trigger("person", rid="r2"))
    await messages(hub, sid)

    hub.register_rule("camera", {**first, "target_labels": ["cat"]})
    changed = await messages(hub, sid)
    assert len(changed) == 1 and changed[0].inside
    assert ("camera", "r1", "person") not in hub.active
    hub.register_rule("camera", {**first, "enabled": False})
    changed = await messages(hub, sid)
    assert len(changed) == 1 and changed[0].inside is False
    assert changed[0].property_operation == "Changed"
    assert hub.snapshot()["active"][0]["rule_id"] == "r2"
    assert len(hub.snapshot()["rules"]) == 2

    hub.remove_camera("camera")
    deleted = await messages(hub, sid)
    assert len(deleted) == 2
    assert {item.rule_id for item in deleted} == {"r1", "r2"}
    assert all(
        item.property_operation == "Deleted" and not item.inside for item in deleted
    )
    assert not hub.snapshot()["rules"]


@pytest.mark.asyncio
async def test_cooldown_suppressed_presence_cannot_extend_an_onvif_rule_episode():
    hub = OnvifEventHub()
    engine = RuleEngine()
    config = rule()
    hub.register_rule("camera", config)
    sid = await hub.subscribe()
    await messages(hub, sid)

    def evaluate(labels, now):
        detections = [
            {"label": label, "confidence": 0.9, "bbox": [10, 10, 20, 20]}
            for label in labels
        ]
        events, _ = engine.evaluate("camera", detections, [config], now)
        for event in events:
            hub.publish_trigger("camera", event)
        hub.sync_active("camera", engine.active_rules("camera"))
        return events

    assert len(evaluate(["person"], 100)) == 1
    evaluate([], 104)
    assert len(evaluate(["cat"], 105)) == 1
    assert evaluate(["person", "cat"], 106) == []  # person is still in cooldown.
    assert len(hub.active) == 1 and ("camera", "r1", "cat") in hub.active
    evaluate(["person"], 109)
    assert engine.active_rules("camera") == {("camera", "r1", "person")}
    assert not hub.active
    edges = await messages(hub, sid)
    assert [item.inside for item in edges] == [True, False, True, False]
    assert {source_token(item) for item in edges} == {"r1"}


def test_legacy_token_helper_returns_rule_uuid_for_all_targets():
    assert (
        rule_target_token("rule", "person")
        == rule_target_token("rule", "cat")
        == "rule"
    )


@pytest.mark.asyncio
async def test_large_catalog_initializes_every_rule_with_bounded_change_queue():
    hub = OnvifEventHub(queue_size=8)
    hub.replace_catalog(
        [
            {"camera_id": "camera", **rule(id=f"r{index}", name=f"规则 {index}")}
            for index in range(300)
        ]
    )
    sid = await hub.subscribe()
    sub = hub.subscriptions[sid]
    assert len(sub.pending_initial) == 300
    assert not sub.queue
    hub.publish_trigger("camera", trigger("person", rid="r299"))
    hub.register_rule("camera", rule(id="r298", name="新名称"))
    hub.remove_rule("camera", "r297")

    received = []
    while sub.pending_initial or sub.queue:
        page = await hub.pull(sid, timeout=0, limit=47)
        assert 0 < len(page) <= 47
        received.extend(page)
        assert len(sub.queue) <= 8
    initialized = [
        item for item in received if item.property_operation == "Initialized"
    ]
    assert len(initialized) == 299
    assert {item.rule_id for item in initialized} == {
        f"r{i}" for i in range(300)
    } - {"r297"}
    assert next(item for item in initialized if item.rule_id == "r299").inside
    assert (
        next(item for item in initialized if item.rule_id == "r298").rule_name
        == "新名称"
    )
    removed = [item for item in received if item.rule_id == "r297"]
    assert len(removed) == 1 and removed[0].property_operation == "Deleted"
    assert not removed[0].inside


@pytest.mark.asyncio
async def test_many_dynamic_additions_and_repeated_sync_do_not_drop_rules():
    hub = OnvifEventHub(queue_size=4)
    sid = await hub.subscribe()
    for index in range(200):
        hub.register_rule("camera", rule(id=f"r{index}"))
    sub = hub.subscriptions[sid]
    assert len(sub.pending_initial) == 200
    for _ in range(3):
        await hub.synchronize(sid)
        assert len(sub.pending_initial) == 200
        assert len(sub.queue) <= 4
    received = []
    for _ in range(20):
        received.extend(await hub.pull(sid, timeout=0, limit=10))
    assert len(received) == len({item.rule_id for item in received}) == 200
    assert not sub.pending_initial
    for _ in range(20):
        hub.publish_trigger("camera", trigger("person", rid="r0"))
        hub.clear_key("camera", "r0", "person")
    assert len(sub.queue) == sub.queue.maxlen == 4
    assert not (await hub.pull(sid, timeout=0, limit=10))[-1].inside


@pytest.mark.asyncio
async def test_rename_refreshes_queued_metadata_and_delete_discards_old_states():
    hub = OnvifEventHub()
    hub.register_rule("camera", rule())
    sid = await hub.subscribe()
    await messages(hub, sid)
    hub.publish_trigger("camera", trigger("person"))
    hub.clear_key("camera", "r1", "person")
    hub.register_rule("camera", rule(name="最新名称"))
    queued = await messages(hub, sid)
    assert [item.inside for item in queued] == [True, False, False]
    assert {item.rule_name for item in queued} == {"最新名称"}

    hub.publish_trigger("camera", trigger("cat"))
    hub.register_rule("camera", rule(name="再次改名"))
    hub.remove_rule("camera", "r1")
    queued = await messages(hub, sid)
    assert len(queued) == 1
    assert queued[0].property_operation == "Deleted"
    assert not queued[0].inside
    await hub.synchronize(sid)
    assert not await messages(hub, sid)


@pytest.mark.asyncio
async def test_synchronization_cannot_replay_old_true_after_current_false():
    hub = OnvifEventHub()
    hub.register_rule("camera", rule())
    sid = await hub.subscribe()
    await messages(hub, sid)
    hub.publish_trigger("camera", trigger("person"))
    hub.clear_key("camera", "r1", "person")
    await hub.synchronize(sid)
    states = await messages(hub, sid)
    assert len(states) == 1
    assert states[0].property_operation == "Initialized"
    assert states[0].inside is False
