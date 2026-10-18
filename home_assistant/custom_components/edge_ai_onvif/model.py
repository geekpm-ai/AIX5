"""Strict ONVIF catalog decoding and scoped native entity reconciliation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

TOPIC = "tns1:RuleEngine/FieldDetector/ObjectsInside"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class CatalogError(ValueError):
    """A partial or malformed catalog must never authorize entity removal."""


class MigrationConflict(ValueError):
    """Several existing entity IDs cannot safely become one automatically."""


def field(value: Any, key: str, default=None):
    return (
        value.get(key, default)
        if isinstance(value, dict)
        else getattr(value, key, default)
    )


def response_items(value: Any, wrapper: str) -> list:
    """Zeep unwraps a single repeated return value into a list or None."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    items = field(value, wrapper, ...)
    if items is ...:
        raise CatalogError(f"Malformed ONVIF {wrapper} response")
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise CatalogError(f"Malformed ONVIF {wrapper} list")
    return list(items)


@dataclass(frozen=True)
class RuleMetadata:
    camera_id: str
    rule_id: str
    name: str
    camera_name: str
    enabled: bool
    camera_enabled: bool
    target_labels: tuple[str, ...]

    def native_uid(self, gateway_id: str) -> str:
        return (
            f"{gateway_id}_{TOPIC}_aix5-source-{self.camera_id}_"
            f"aix5-analytics-{self.camera_id}_{self.rule_id}"
        )


def parse_configuration(configuration: Any) -> str:
    token = field(configuration, "token", "")
    prefix = "aix5-analytics-"
    if not isinstance(token, str) or not token.startswith(prefix):
        raise CatalogError("Unexpected analytics configuration token")
    camera_id = token[len(prefix) :]
    if not TOKEN.fullmatch(camera_id):
        raise CatalogError("Invalid camera identity in ONVIF catalog")
    return camera_id


def parse_rule(camera_id: str, value: Any) -> RuleMetadata:
    rule_id = field(value, "Name", "")
    if not isinstance(rule_id, str) or not TOKEN.fullmatch(rule_id):
        raise CatalogError("Invalid rule identity in ONVIF catalog")
    parameters = field(value, "Parameters")
    if parameters is None:
        raise CatalogError("Rule metadata is missing")
    values = {}
    for item in field(parameters, "SimpleItem", []) or []:
        name, data = field(item, "Name"), field(item, "Value")
        if name in values:
            raise CatalogError("Duplicate rule metadata parameter")
        values[name] = data
    if values.get("CameraId") != camera_id:
        raise CatalogError("Rule belongs to a different camera")
    name = values.get("RuleName")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError("Rule display name is missing")
    for key in ("Enabled", "CameraEnabled"):
        if values.get(key) not in {"true", "false"}:
            raise CatalogError(f"Invalid {key} flag")
    try:
        labels = json.loads(values["TargetLabels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogError("Invalid target label list") from exc
    if (
        not isinstance(labels, list)
        or not labels
        or not all(isinstance(x, str) for x in labels)
    ):
        raise CatalogError("Invalid target label list")
    return RuleMetadata(
        camera_id,
        rule_id,
        name,
        str(values.get("CameraName", "")),
        values["Enabled"] == "true",
        values["CameraEnabled"] == "true",
        tuple(labels),
    )


def managed_uid(gateway_id: str, unique_id: str) -> bool:
    """Only this gateway's exact AIX5 FieldDetector identities are managed."""
    prefix = f"{gateway_id}_{TOPIC}_aix5-source-"
    if not unique_id.startswith(prefix):
        return False
    remainder = unique_id[len(prefix) :]
    match = re.fullmatch(
        r"(?P<camera>[A-Za-z0-9_-]+)_aix5-analytics-(?P=camera)_(?P<rule>[A-Za-z0-9_-]+)(?::[A-Za-z0-9_-]+)?",
        remainder,
    )
    return match is not None


@dataclass(frozen=True)
class Migration:
    entity_id: str
    old_unique_id: str
    new_unique_id: str


def plan_migrations(entries: list, catalog: dict[str, RuleMetadata]) -> list[Migration]:
    """Preserve existing entity_id/automations when one old target becomes a rule.

    Conflicting aggregate or multiple old target entries require an explicit
    migration decision; selecting one silently could break an automation.
    """
    by_uid = {entry.unique_id: entry for entry in entries}
    migrations = []
    for uid in catalog:
        legacy = [
            entry for old_uid, entry in by_uid.items() if old_uid.startswith(uid + ":")
        ]
        if not legacy:
            continue
        if len(legacy) != 1 or uid in by_uid:
            identities = ", ".join(entry.entity_id for entry in legacy)
            if uid in by_uid:
                identities += ", " + by_uid[uid].entity_id
            raise MigrationConflict(
                f"Multiple entities refer to one rule: {identities}"
            )
        old = legacy[0]
        migrations.append(Migration(old.entity_id, old.unique_id, uid))
    return migrations
