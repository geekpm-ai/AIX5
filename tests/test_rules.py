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
