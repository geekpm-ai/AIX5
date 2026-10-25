import pytest

from aix5_ai.rules import RuleEngine, contains


def rule(**overrides):
    return {
        "id": "r",
        "name": "区域",
        "enabled": True,
        "target_labels": ["person"],
        "confidence_threshold": 0.55,
        "min_hits": 2,
        "cooldown_seconds": 60,
        "clear_seconds": 3,
        "zone": [(0, 0), (1, 0), (1, 1), (0, 1)],
        **overrides,
    }


def detection(box=(100, 100, 300, 400)):
    return {"label": "person", "confidence": 0.9, "bbox": list(box)}


@pytest.mark.parametrize(
    "size,box", [((1920, 1080), (100, 100, 300, 400)), ((848, 480), (10, 20, 100, 200))]
)
def test_normalized_zone_accepts_pixel_boxes(size, box):
    engine = RuleEngine()
    assert not engine.evaluate("c", [detection(box)], [rule()], 1, size)[0]
    events, present = engine.evaluate("c", [detection(box)], [rule()], 2, size)
    assert len(events) == 1
    assert present["person"]
    assert events[0]["bbox"] == list(box)


def test_zone_rejects_target_outside_and_accepts_boundary():
    engine = RuleEngine()
    zone = [(0, 0), (0.5, 0), (0.5, 1), (0, 1)]
    assert not engine.evaluate(
        "c", [detection((70, 0, 90, 80))], [rule(zone=zone, min_hits=1)], 1, (100, 100)
    )[0]
    assert contains((0.5, 1), zone)


def test_episode_and_cooldown_are_independent_of_presence():
    engine = RuleEngine()
    rules = [rule(min_hits=1, zone=[])]
    assert len(engine.evaluate("c", [detection()], rules, 100)[0]) == 1
    assert not engine.evaluate("c", [detection()], rules, 200)[0]  # continuous presence
    assert not engine.evaluate("c", [], rules, 203)[1]["person"]
    assert len(engine.evaluate("c", [detection()], rules, 204)[0]) == 1
    engine.evaluate("c", [], rules, 207)
    events, present = engine.evaluate("c", [detection()], rules, 208)
    assert not events
    assert present["person"]  # cooldown only suppresses notifications


def test_disabled_rules_discard_active_state():
    engine = RuleEngine()
    engine.evaluate("c", [detection()], [rule(zone=[], min_hits=1)], 100)
    assert not engine.evaluate("c", [], [rule(enabled=False)], 101, (100, 100))[1][
        "person"
    ]
    assert not engine.states


def test_zone_requires_image_dimensions():
    with pytest.raises(ValueError):
        RuleEngine().evaluate("c", [detection()], [rule()], 1)


def test_absence_rule_emits_after_continuous_gap_and_resets_on_return():
    engine = RuleEngine()
    configured = [rule(min_hits=1, zone=[], absence_seconds=60)]
    # First no-target frame starts the absence clock; threshold is not reached.
    assert not engine.evaluate("c", [], configured, 100)[0]
    assert not engine.evaluate("c", [], configured, 159)[0]
    events, present = engine.evaluate("c", [], configured, 160)
    assert len(events) == 1
    assert events[0]["event_type"] == "target_absent"
    assert events[0]["confidence"] == 0.0
    assert not present["person"]
    assert not engine.evaluate("c", [], configured, 200)[0]
    # A target returning clears the absence episode, without a false presence
    # alert or a target-present event.
    events, present = engine.evaluate("c", [detection()], configured, 201)
    assert not events and present["person"]
    assert not engine.evaluate("c", [], configured, 202)[0]
    assert len(engine.evaluate("c", [], configured, 262)[0]) == 1


def test_absence_rule_respects_normalized_zone_and_cooldown():
    engine = RuleEngine()
    configured = [rule(min_hits=1, zone=[(0, 0), (0.5, 0), (0.5, 1), (0, 1)], absence_seconds=60)]
    # A target outside the zone still counts as absent.
    events, _ = engine.evaluate("c", [detection((70, 0, 90, 80))], configured, 10, (100, 100))
    assert not events
    events, _ = engine.evaluate("c", [], configured, 70, (100, 100))
    assert len(events) == 1 and events[0]["event_type"] == "target_absent"
    engine.evaluate("c", [detection((10, 0, 20, 80))], configured, 71, (100, 100))
    engine.evaluate("c", [], configured, 131, (100, 100))
    # Cooldown is 60 seconds and the new absence episode started at 71.
    assert not engine.evaluate("c", [], configured, 180, (100, 100))[0]
