"""ONVIF event state and PullPoint notification support.

The application already has a rule engine and an MQTT publisher.  This module
keeps the ONVIF side deliberately small and independent from both of those
implementations: only formal rule episode edges are published, and every
PullPoint subscriber receives its own bounded queue.  The HTTP/SOAP adapter is
in :mod:`aix5_ai.main`.

One configured rule is one ONVIF property. Target classes remain internal
contributors to that property's active episode; their names are metadata.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WS_ADDRESSING = "http://www.w3.org/2005/08/addressing"
WSNT = "http://docs.oasis-open.org/wsn/b-2"
TEV = "http://www.onvif.org/ver10/events/wsdl"
TT = "http://www.onvif.org/ver10/schema"
TOPICS = "http://www.onvif.org/ver10/topics"

EVENT_TOPIC = "tns1:RuleEngine/FieldDetector/ObjectsInside"

for prefix, uri in (
    ("s", SOAP_ENV),
    ("wsa", WS_ADDRESSING),
    ("wsnt", WSNT),
    ("tev", TEV),
    ("tt", TT),
    ("tns1", TOPICS),
):
    ET.register_namespace(prefix, uri)


def camera_source_token(camera_id: str) -> str:
    """Return a stable ONVIF source token for a persisted camera UUID."""

    # Keep this token identical to the Media Profile's VideoSourceConfiguration
    # token so HA can associate an event with the same video channel.
    return f"aix5-source-{camera_id}"


def analytics_configuration_token(camera_id: str) -> str:
    """Return a stable analytics configuration token for a persisted camera."""

    return f"aix5-analytics-{camera_id}"


def rule_target_token(rule_id: str, target_type: str = "") -> str:
    """Return one stable Rule token regardless of configured target labels.

    Keep the helper name for existing diagnostics; target_type is deliberately
    ignored because one configured rule represents one ONVIF property.
    """

    return str(rule_id)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_xsd_duration(value: str | None, default: float = 60.0) -> float:
    """Parse the small subset of xsd:duration used by ONVIF timeouts.

    ONVIF clients normally send ``PT60S``.  Supporting days/hours/minutes as
    well makes the endpoint tolerant of clients that choose a different legal
    duration.  Invalid or unbounded durations are clamped to a safe default.
    """

    import re

    if not value:
        return default
    match = re.fullmatch(
        r"P(?:(?P<days>\d+(?:\.\d+)?)D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?",
        value.strip().upper(),
    )
    if not match:
        return default
    seconds = sum(
        float(match.group(name) or 0) * scale
        for name, scale in (
            ("days", 86400),
            ("hours", 3600),
            ("minutes", 60),
            ("seconds", 1),
        )
    )
    return max(1.0, min(seconds or default, 86400.0))


@dataclass(frozen=True)
class OnvifNotification:
    """One standard FieldDetector notification.

    ``key`` is the camera/rule identity and is intentionally retained
    in addition to the XML fields.  It lets tests and diagnostics identify the
    source without reparsing XML, while HA receives only standard ONVIF data.
    """

    key: tuple[str, str]
    camera_id: str
    rule_id: str
    rule_name: str
    target_type: str
    inside: bool
    sequence: int
    utc_time: str
    event_type: str = "target_detected"
    event_id: str | None = None
    property_operation: str = "Changed"
    camera_name: str = ""
    target_labels: tuple[str, ...] = ()
    enabled: bool = True

    def message_element(self) -> ET.Element:
        """Build a ``wsnt:NotificationMessage`` element for PullMessages."""

        notification = ET.Element(f"{{{WSNT}}}NotificationMessage")
        topic = ET.SubElement(notification, f"{{{WSNT}}}Topic")
        topic.set(
            "Dialect", "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete"
        )
        topic.text = EVENT_TOPIC
        message = ET.SubElement(notification, f"{{{WSNT}}}Message")
        payload = ET.SubElement(message, f"{{{TT}}}Message")
        payload.set("UtcTime", self.utc_time)
        payload.set("PropertyOperation", self.property_operation)
        source = ET.SubElement(payload, f"{{{TT}}}Source")
        for name, value in (
            ("VideoSourceConfigurationToken", camera_source_token(self.camera_id)),
            (
                "VideoAnalyticsConfigurationToken",
                analytics_configuration_token(self.camera_id),
            ),
            ("Rule", rule_target_token(self.rule_id, self.target_type)),
            ("RuleId", self.rule_id),
            ("RuleName", self.rule_name),
        ):
            ET.SubElement(source, f"{{{TT}}}SimpleItem", Name=name, Value=str(value))
        data = ET.SubElement(payload, f"{{{TT}}}Data")
        ET.SubElement(
            data,
            f"{{{TT}}}SimpleItem",
            Name="IsInside",
            Value="true" if self.inside else "false",
        )
        # Extensions are ignored by HA's standard parser but useful to a
        # client that wants to show the configured rule and target names.
        ET.SubElement(
            data,
            f"{{{TT}}}SimpleItem",
            Name="EventType",
            Value=self.event_type,
        )
        for name, value in (
            ("CameraName", self.camera_name),
            ("Target", self.target_type),
            ("TargetLabels", ",".join(self.target_labels)),
            ("Enabled", "true" if self.enabled else "false"),
        ):
            ET.SubElement(data, f"{{{TT}}}SimpleItem", Name=name, Value=value)
        if self.event_id:
            ET.SubElement(
                data,
                f"{{{TT}}}SimpleItem",
                Name="EventId",
                Value=self.event_id,
            )
        return notification

    def as_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "target_type": self.target_type,
            "inside": self.inside,
            "sequence": self.sequence,
            "utc_time": self.utc_time,
            "topic": EVENT_TOPIC,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "property_operation": self.property_operation,
            "camera_name": self.camera_name,
            "target_labels": list(self.target_labels),
            "enabled": self.enabled,
            "rule_token": self.rule_id,
        }


@dataclass
class _Subscription:
    id: str
    expires_at: float
    queue: deque[OnvifNotification] = field(default_factory=lambda: deque(maxlen=128))
    # Only keys are retained for synchronization. Messages are generated at
    # pull time, so a large rule catalog is paginated without overflowing the
    # bounded queue or retaining obsolete rule names/states.
    pending_initial: dict[tuple[str, str], None] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class RuleCatalogEntry:
    camera_id: str
    rule_id: str
    name: str
    target_labels: tuple[str, ...]
    event_type: str
    enabled: bool = True
    camera_name: str = ""
    camera_enabled: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return self.camera_id, self.rule_id


class OnvifEventHub:
    """One ONVIF property per configured rule, with per-target episode state.

    Only formal RuleEngine triggers may enter ``active``.  Rule presence alone
    never activates an ONVIF property, including presence suppressed by the
    notification cooldown.  The exposed property is true while any admitted
    target remains active; clearing one target cannot clear another target.
    """

    def __init__(self, queue_size: int = 128, subscription_limit: int = 32):
        self.queue_size = max(1, int(queue_size))
        self.subscription_limit = max(1, int(subscription_limit))
        self.subscriptions: dict[str, _Subscription] = {}
        self.active: dict[tuple[str, str, str], OnvifNotification] = {}
        self.known: dict[tuple[str, str], RuleCatalogEntry] = {}
        self._sequence = 0

    def reset(self) -> None:
        for sub in self.subscriptions.values():
            sub.wake.set()
        self.subscriptions.clear()
        self.active.clear()
        self.known.clear()
        self._sequence = 0

    def _active_for(self, key: tuple[str, str]) -> list[OnvifNotification]:
        return [item for target, item in self.active.items() if target[:2] == key]

    def _new_notification(
        self,
        entry: RuleCatalogEntry,
        *,
        property_operation: str = "Changed",
        event_id: str | None = None,
        target_type: str | None = None,
    ) -> OnvifNotification:
        self._sequence += 1
        active = self._active_for(entry.key)
        latest = max(active, key=lambda item: item.sequence, default=None)
        return OnvifNotification(
            key=entry.key,
            camera_id=entry.camera_id,
            camera_name=entry.camera_name,
            rule_id=entry.rule_id,
            rule_name=entry.name,
            target_type=target_type
            if target_type is not None
            else ",".join(sorted(item.target_type for item in active)),
            target_labels=entry.target_labels,
            enabled=entry.enabled and entry.camera_enabled,
            inside=bool(active),
            sequence=self._sequence,
            utc_time=_utc_now(),
            event_type=entry.event_type,
            event_id=event_id or (latest.event_id if latest else None),
            property_operation=property_operation,
        )

    def _broadcast(self, notification: OnvifNotification) -> None:
        self._expire()
        for sub in self.subscriptions.values():
            if notification.property_operation in {"Initialized", "Deleted"}:
                # A delete supersedes every pending state for this identity;
                # a re-create must not replay its previous deletion later.
                sub.queue = deque(
                    (item for item in sub.queue if item.key != notification.key),
                    maxlen=self.queue_size,
                )
                if notification.property_operation == "Initialized":
                    sub.pending_initial[notification.key] = None
                else:
                    sub.pending_initial.pop(notification.key, None)
                    sub.queue.append(notification)
            elif notification.key not in sub.pending_initial:
                sub.queue.append(notification)
            # If initialization is still pending, its current rule state is
            # sufficient; an older queued Changed must not overwrite it.
            sub.wake.set()

    def register_rule(
        self,
        camera_id: str,
        rule: dict,
        *,
        camera_name: str = "",
        camera_enabled: bool = True,
    ) -> bool:
        """Register/update one persisted rule without changing its identity.

        Disabled rules remain in the catalog as false properties.  A rename
        publishes Changed metadata under the same Rule UUID and does not reset
        an active episode.  Returns whether catalog metadata changed.
        """
        entry = RuleCatalogEntry(
            camera_id=str(camera_id),
            rule_id=str(rule["id"]),
            name=str(rule.get("name", rule["id"])),
            target_labels=tuple(sorted(set(map(str, rule.get("target_labels", []))))),
            event_type=str(
                rule.get("event_type")
                or ("target_entered_zone" if rule.get("zone") else "target_detected")
            ),
            enabled=bool(rule.get("enabled", True)),
            camera_name=str(camera_name),
            camera_enabled=bool(camera_enabled),
        )
        old = self.known.get(entry.key)
        if old == entry:
            return False
        self.known[entry.key] = entry
        if old is not None:
            # Preserve queued event edges but refresh display metadata. A slow
            # subscriber must not undo a rename using an older queued name.
            for sub in self.subscriptions.values():
                sub.queue = deque(
                    (
                        replace(
                            item,
                            rule_name=entry.name,
                            camera_name=entry.camera_name,
                            target_labels=entry.target_labels,
                            enabled=entry.enabled and entry.camera_enabled,
                        )
                        if item.key == entry.key
                        else item
                        for item in sub.queue
                    ),
                    maxlen=self.queue_size,
                )
        # Update membership before sending Changed so disable/target-removal
        # metadata carries the final aggregate state in a single message.
        for key in list(self.active):
            if key[:2] == entry.key and (
                not entry.enabled
                or not entry.camera_enabled
                or key[2] not in entry.target_labels
            ):
                self.active.pop(key)
        self._broadcast(
            self._new_notification(
                entry, property_operation="Initialized" if old is None else "Changed"
            )
        )
        return True

    def replace_catalog(self, catalog: list[dict]) -> None:
        """Reconcile the complete saved rule catalog, including disabled rules."""
        keys = {(str(item["camera_id"]), str(item["id"])) for item in catalog}
        for key in list(self.known):
            if key not in keys:
                self.remove_rule(*key)
        for item in catalog:
            self.register_rule(
                item["camera_id"],
                item,
                camera_name=item.get("camera_name", ""),
                camera_enabled=item.get("camera_enabled", True),
            )

    def remove_rule(self, camera_id: str, rule_id: str) -> bool:
        key = (str(camera_id), str(rule_id))
        entry = self.known.pop(key, None)
        for target in list(self.active):
            if target[:2] == key:
                self.active.pop(target)
        if entry is None:
            return False
        self._broadcast(self._new_notification(entry, property_operation="Deleted"))
        return True

    def remove_camera(self, camera_id: str) -> None:
        for cid, rid in list(self.known):
            if cid == str(camera_id):
                self.remove_rule(cid, rid)

    def publish_trigger(self, camera_id: str, event: dict) -> bool:
        """Admit a formal target event; emit true only for the first target."""
        target = (str(camera_id), str(event["rule_id"]), str(event["target_type"]))
        entry = self.known.get(target[:2])
        if (
            entry is None
            or not entry.enabled
            or not entry.camera_enabled
            or target[2] not in entry.target_labels
            or target in self.active
        ):
            return False
        was_active = bool(self._active_for(entry.key))
        # Internal target metadata keeps the formal EventId even if a sibling
        # already made the aggregate property true and no new edge is sent.
        target_item = self._new_notification(
            entry,
            event_id=str(event["event_id"]) if event.get("event_id") else None,
            target_type=target[2],
        )
        self.active[target] = replace(target_item, inside=True)
        if not was_active:
            self._broadcast(self._new_notification(entry))
        return True

    def sync_active(
        self, camera_id: str, active_keys: set[tuple[str, str, str]]
    ) -> None:
        """Clear ended targets; emit false only when the last target clears."""
        active_keys = {tuple(map(str, key)) for key in active_keys}
        cleared: dict[tuple[str, str], OnvifNotification] = {}
        for key, item in list(self.active.items()):
            if key[0] == str(camera_id) and key not in active_keys:
                cleared[key[:2]] = item
                self.active.pop(key)
        for key, old in cleared.items():
            if key in self.known and not self._active_for(key):
                self._broadcast(
                    self._new_notification(
                        self.known[key],
                        event_id=old.event_id,
                    )
                )

    def clear_camera(self, camera_id: str) -> None:
        self.sync_active(camera_id, set())

    def clear_key(self, camera_id: str, rule_id: str, target_type: str) -> None:
        key = (str(camera_id), str(rule_id), str(target_type))
        self.sync_active(camera_id, set(self.active) - {key})

    def clear_rule(self, camera_id: str, rule_id: str) -> None:
        key = (str(camera_id), str(rule_id))
        self.sync_active(
            camera_id, {target for target in self.active if target[:2] != key}
        )

    async def subscribe(
        self, timeout: float = 600, initial: list[dict] | None = None
    ) -> str:
        self._expire()
        if len(self.subscriptions) >= self.subscription_limit:
            raise RuntimeError("ONVIF PullPoint 订阅数量已达上限")
        if initial is not None:
            self.replace_catalog(initial)
        sid = uuid.uuid4().hex
        sub = _Subscription(
            sid,
            time.time() + max(1, min(float(timeout), 86400)),
            queue=deque(maxlen=self.queue_size),
        )
        self.subscriptions[sid] = sub
        await self.synchronize(sid)
        return sid

    async def pull(
        self, sid: str, timeout: float = 10, limit: int = 10
    ) -> list[OnvifNotification]:
        sub = self._get(sid)
        deadline = min(time.time() + max(0, min(float(timeout), 60)), sub.expires_at)
        while not sub.queue and not sub.pending_initial and time.time() < deadline:
            try:
                await asyncio.wait_for(
                    sub.wake.wait(), max(0.01, deadline - time.time())
                )
            except asyncio.TimeoutError:
                break
            finally:
                sub.wake.clear()
            sub = self._get(sid)
        result = []
        message_limit = max(1, min(int(limit), 100))
        while sub.pending_initial and len(result) < message_limit:
            key = next(iter(sub.pending_initial))
            sub.pending_initial.pop(key)
            entry = self.known.get(key)
            if entry is not None:
                result.append(
                    self._new_notification(entry, property_operation="Initialized")
                )
        while sub.queue and len(result) < message_limit:
            result.append(sub.queue.popleft())
        if not sub.queue and not sub.pending_initial:
            sub.wake.clear()
        return result

    async def synchronize(self, sid: str) -> None:
        sub = self._get(sid)
        # Replace, rather than append, the catalog snapshot. Repeated syncs
        # consume at most one key per configured rule. Superseded Changed
        # states cannot reactivate a property after its current snapshot.
        sub.pending_initial = dict.fromkeys(self.known)
        sub.queue = deque(
            (
                item
                for item in sub.queue
                if item.property_operation == "Deleted" and item.key not in self.known
            ),
            maxlen=self.queue_size,
        )
        if sub.queue or sub.pending_initial:
            sub.wake.set()

    def renew(self, sid: str, timeout: float = 600) -> float:
        sub = self._get(sid)
        sub.expires_at = time.time() + max(1, min(float(timeout), 86400))
        return sub.expires_at

    def unsubscribe(self, sid: str) -> None:
        if sid not in self.subscriptions:
            raise KeyError(sid)
        self.subscriptions.pop(sid).wake.set()

    def snapshot(self) -> dict:
        self._expire()
        rules = []
        for entry in self.known.values():
            active = self._active_for(entry.key)
            rules.append(
                {
                    "camera_id": entry.camera_id,
                    "camera_name": entry.camera_name,
                    "rule_id": entry.rule_id,
                    "rule_token": entry.rule_id,
                    "rule_name": entry.name,
                    "target_labels": list(entry.target_labels),
                    "active_targets": sorted(item.target_type for item in active),
                    "enabled": entry.enabled,
                    "camera_enabled": entry.camera_enabled,
                    "inside": bool(active),
                    "event_type": entry.event_type,
                }
            )
        return {
            "rules": rules,
            "active": [item for item in rules if item["inside"]],
            "subscriptions": [
                {
                    "id": sub.id,
                    "queued": len(sub.queue),
                    "initializing": len(sub.pending_initial),
                    "expires_at": sub.expires_at,
                }
                for sub in self.subscriptions.values()
            ],
        }

    def _get(self, sid: str) -> _Subscription:
        self._expire()
        sub = self.subscriptions.get(sid)
        if not sub:
            raise KeyError(sid)
        return sub

    def _expire(self) -> None:
        now = time.time()
        for sid, sub in list(self.subscriptions.items()):
            if sub.expires_at <= now:
                self.subscriptions.pop(sid, None)


def soap_envelope(body: ET.Element, action: str | None = None) -> bytes:
    """Wrap an ONVIF response body in a SOAP 1.2 envelope."""

    envelope = ET.Element(f"{{{SOAP_ENV}}}Envelope")
    if action:
        header = ET.SubElement(envelope, f"{{{SOAP_ENV}}}Header")
        ET.SubElement(header, f"{{{WS_ADDRESSING}}}Action").text = action
    ET.SubElement(envelope, f"{{{SOAP_ENV}}}Body").append(body)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def soap_fault(code: str, reason: str) -> bytes:
    fault = ET.Element(f"{{{SOAP_ENV}}}Fault")
    code_node = ET.SubElement(fault, f"{{{SOAP_ENV}}}Code")
    ET.SubElement(code_node, f"{{{SOAP_ENV}}}Value").text = f"s:{code}"
    reason_node = ET.SubElement(fault, f"{{{SOAP_ENV}}}Reason")
    ET.SubElement(reason_node, f"{{{SOAP_ENV}}}Text").text = reason
    return soap_envelope(fault)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_text(root: ET.Element, name: str) -> str | None:
    return next(
        (el.text for el in root.iter() if local_name(el.tag) == name and el.text), None
    )
