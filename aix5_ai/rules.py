"""Detection state and event edges are separate from event notification cooldown."""

from dataclasses import dataclass


def contains(point, polygon):
    if not polygon:
        return True
    x, y = point
    inside = False
    for i, (ax, ay) in enumerate(polygon):
        bx, by = polygon[(i + 1) % len(polygon)]
        cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
        if (
            abs(cross) < 1e-9
            and min(ax, bx) - 1e-9 <= x <= max(ax, bx) + 1e-9
            and min(ay, by) - 1e-9 <= y <= max(ay, by) + 1e-9
        ):
            return True
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


@dataclass
class State:
    hits: int = 0
    active: bool = False
    seen: float = 0
    emitted: float = -1e20
    absence_since: float | None = None


class RuleEngine:
    def __init__(self):
        self.states = {}

    def reset_camera(self, cid):
        self.states = {k: v for k, v in self.states.items() if k[0] != cid}

    def drop_rule(self, rid):
        self.states = {k: v for k, v in self.states.items() if k[1] != rid}

    def active_rules(self, cid):
        """Return active (camera, rule, label) states for ONVIF events."""
        return {
            key for key, state in self.states.items() if key[0] == cid and state.active
        }

    def evaluate(self, cid, detections, rules, now, image_size=None):
        """Detections use pixel xyxy boxes; zones use normalized coordinates."""
        if any(r["enabled"] and r["zone"] for r in rules):
            if not image_size or min(image_size) <= 0:
                raise ValueError("区域规则需要有效的原始图像宽高")
        width, height = image_size or (1, 1)
        valid_keys = {
            (cid, r["id"], label)
            for r in rules
            if r["enabled"]
            for label in r["target_labels"]
        }
        self.states = {
            k: v for k, v in self.states.items() if k[0] != cid or k in valid_keys
        }
        events = []
        present = {
            label: False for rule in rules for label in rule.get("target_labels", [])
        }
        for rule in rules:
            if not rule["enabled"]:
                continue
            for label in rule["target_labels"]:
                key = (cid, rule["id"], label)
                s = self.states.setdefault(key, State())
                matches = [
                    d
                    for d in detections
                    if d["label"] == label
                    and d["confidence"] >= rule["confidence_threshold"]
                    and contains(
                        (
                            (d["bbox"][0] + d["bbox"][2]) / (2 * width),
                            d["bbox"][3] / height,
                        ),
                        rule["zone"],
                    )
                ]
                if matches:
                    if rule.get("absence_seconds"):
                        # A returning target clears an absence alert episode;
                        # absence rules do not emit target-present events.
                        s.active = False
                        # Keep the last-seen timestamp so the next missing
                        # frame measures absence from the actual return frame.
                        s.absence_since = now
                        s.hits = 0
                        present[label] = True
                        continue
                    s.hits += 1
                    s.seen = now
                    if s.hits >= rule["min_hits"]:
                        was_active = s.active
                        s.active = True
                        # One event per continuous episode. Cooldown suppresses a rapid re-entry.
                        if (
                            not was_active
                            and now - s.emitted >= rule["cooldown_seconds"]
                        ):
                            s.emitted = now
                            events.append(
                                {
                                    "rule_id": rule["id"],
                                    "rule_name": rule["name"],
                                    "event_type": "target_entered_zone"
                                    if rule["zone"]
                                    else "target_detected",
                                    "target_type": label,
                                    **max(matches, key=lambda d: d["confidence"]),
                                }
                            )
                else:
                    if rule.get("absence_seconds"):
                        s.hits = 0
                        if s.absence_since is None:
                            s.absence_since = now
                        if (
                            not s.active
                            and now - s.absence_since >= rule["absence_seconds"]
                            and now - s.emitted >= rule["cooldown_seconds"]
                        ):
                            s.active = True
                            s.emitted = now
                            events.append(
                                {
                                    "rule_id": rule["id"],
                                    "rule_name": rule["name"],
                                    "event_type": "target_absent",
                                    "target_type": label,
                                    "confidence": 0.0,
                                    "bbox": [0.0, 0.0, 0.0, 0.0],
                                }
                            )
                        # An absence alert is the ONVIF active episode; it does
                        # not make the ordinary detection presence indicator true.
                        continue
                    s.hits = 0
                    if now - s.seen >= rule["clear_seconds"]:
                        s.active = False
                present[label] |= s.active
        present["pet"] = present.get("cat", False) or present.get("dog", False)
        return events, present
