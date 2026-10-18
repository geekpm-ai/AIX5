"""Companion identity/catalog behavior without requiring HA on the board."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "home_assistant/custom_components/edge_ai_onvif/model.py"
)
spec = importlib.util.spec_from_file_location("edge_ai_rule_sync_model", MODULE_PATH)
model = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = model
spec.loader.exec_module(model)


def rule_value(**changes):
    values = {
        "RuleName": "客厅有人",
        "CameraId": "camera-1",
        "CameraName": "客厅",
        "Enabled": "true",
        "CameraEnabled": "true",
        "TargetLabels": '["person","cat"]',
        **changes,
    }
    return {
        "Name": "rule-1",
        "Parameters": {
            "SimpleItem": [{"Name": k, "Value": v} for k, v in values.items()]
        },
    }


def test_rule_catalog_is_one_identity_per_rule_and_keeps_disabled_rules():
    rule = model.parse_rule("camera-1", rule_value(Enabled="false"))
    renamed = model.parse_rule("camera-1", rule_value(RuleName="重新命名"))
    assert rule.native_uid("gateway") == renamed.native_uid("gateway")
    assert not rule.enabled
    assert rule.target_labels == ("person", "cat")
    assert renamed.name == "重新命名"
    assert model.managed_uid("gateway", rule.native_uid("gateway"))
    assert model.managed_uid("gateway", rule.native_uid("gateway") + ":person")
    assert not model.managed_uid("another-gateway", rule.native_uid("gateway"))
    assert not model.managed_uid(
        "gateway",
        rule.native_uid("gateway").replace(
            "aix5-analytics-camera-1", "aix5-analytics-wrong"
        ),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"CameraId": "wrong"},
        {"RuleName": ""},
        {"TargetLabels": "not-json"},
        {"TargetLabels": "[]"},
        {"Enabled": "unknown"},
        {"CameraEnabled": "unknown"},
    ],
)
def test_malformed_catalog_fails_closed(changes):
    with pytest.raises(model.CatalogError):
        model.parse_rule("camera-1", rule_value(**changes))


def test_one_to_one_legacy_migration_preserves_existing_entity_ids():
    rule = model.parse_rule("camera-1", rule_value())
    uid = rule.native_uid("gateway")
    existing = SimpleNamespace(
        entity_id="binary_sensor.keep_existing_automation", unique_id=uid + ":person"
    )
    migration = model.plan_migrations([existing], {uid: rule})
    assert len(migration) == 1
    assert migration[0].entity_id == existing.entity_id
    assert migration[0].old_unique_id == uid + ":person"
    assert migration[0].new_unique_id == uid
    assert (
        model.plan_migrations(
            [SimpleNamespace(entity_id=existing.entity_id, unique_id=uid)], {uid: rule}
        )
        == []
    )


@pytest.mark.parametrize("conflict", ["multiple_targets", "aggregate_exists"])
def test_ambiguous_migration_does_not_silently_discard_automation_entities(conflict):
    rule = model.parse_rule("camera-1", rule_value())
    uid = rule.native_uid("gateway")
    entries = [
        SimpleNamespace(entity_id="binary_sensor.person", unique_id=uid + ":person")
    ]
    entries.append(
        SimpleNamespace(
            entity_id="binary_sensor.other",
            unique_id=uid + (":cat" if conflict == "multiple_targets" else ""),
        )
    )
    with pytest.raises(model.MigrationConflict):
        model.plan_migrations(entries, {uid: rule})


def test_configuration_and_response_validation():
    assert model.parse_configuration({"token": "aix5-analytics-camera-1"}) == "camera-1"
    assert model.response_items(None, "Rule") == []
    assert model.response_items({"Rule": []}, "Rule") == []
    with pytest.raises(model.CatalogError):
        model.response_items({"Unexpected": []}, "Rule")
    with pytest.raises(model.CatalogError):
        model.parse_configuration({"token": "foreign-camera-1"})
    manifest = json.loads((MODULE_PATH.parent / "manifest.json").read_text())
    assert manifest["dependencies"] == ["onvif"]
    assert manifest["domain"] == "edge_ai_onvif"
