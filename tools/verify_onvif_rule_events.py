#!/usr/bin/env python3
"""Disposable ONVIF + RuleEngine server for real Home Assistant event QA.

Run from the project checkout on the board (never opens the production DB):
    python tools/verify_onvif_rule_events.py --base-url http://BOARD:18080 \
        --credentials-file /secure/path/qa-credentials.json

The JSON file contains username/password and optionally rtsp_uri. Alternatively
set AIX5_QA_USERNAME, AIX5_QA_PASSWORD and optionally AIX5_QA_RTSP_URI. Credentials
are not accepted on the command line or printed. The server creates its own
TemporaryDirectory and Store, and removes them on graceful shutdown. It does
not start WS-Discovery or touch production cameras, rules, events, or services.

Add BOARD:18080 manually as a disposable ONVIF integration in Home Assistant.
GET /qa/status and POST /qa/step require HTTP Basic using the supplied account.
POST {"scenario":"min_hits"}, then {"scenario":"valid"}, then
{"scenario":"clear"} to observe false -> true -> false. Full scenario names
and resulting rule tokens are returned by /qa/status. Stop with SIGTERM after
removing the disposable HA entry. No video/audio is captured by this process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

# A directly executed tools/ script must import the checkout, not an installed
# release from another virtual environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argon2 import PasswordHasher
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from aix5_ai.onvif_auth import OnvifAuthError, authenticate
from aix5_ai.onvif_events import OnvifEventHub
from aix5_ai.onvif_gateway import ONVIFGateway, _action_name, _fault
from aix5_ai.rules import RuleEngine
from aix5_ai.schemas import CameraConfig, RuleConfig
from aix5_ai.store import Store

CAMERA_ID = "qa-event-camera"
PRIMARY_RULE = "qa-rule-primary"
SECONDARY_RULE = "qa-rule-secondary"
SCENARIOS = (
    "unconfigured",
    "disabled",
    "outside",
    "below_threshold",
    "min_hits",
    "valid",
    "clear",
    "cooldown",
    "clear_cooldown",
    "after_cooldown",
    "multi_rule",
    "disable_active",
    "remove_active",
    "disconnect",
    "reset",
)


def primary_rule(**overrides):
    return {
        "id": PRIMARY_RULE,
        "camera_id": CAMERA_ID,
        **RuleConfig(
            name="QA primary: person and cat",
            target_labels=["person", "cat"],
            confidence_threshold=0.8,
            min_hits=2,
            clear_seconds=3,
            cooldown_seconds=60,
            zone=[(0, 0), (0.5, 0), (0.5, 1), (0, 1)],
            **overrides,
        ).model_dump(),
    }


def secondary_rule():
    rule = primary_rule()
    rule.update(
        id=SECONDARY_RULE, name="QA secondary: person", target_labels=["person"]
    )
    return rule


def detection(label="person", confidence=0.95, outside=False):
    return {
        "label": label,
        "confidence": confidence,
        "bbox": [70, 10, 90, 80] if outside else [10, 10, 30, 80],
    }


class AuditHub(OnvifEventHub):
    def __init__(self):
        super().__init__()
        self.history = deque(maxlen=1000)
        self.true_count = 0
        self.false_count = 0

    def _broadcast(self, notification):
        self.history.append(notification.as_dict())
        self.true_count += int(notification.inside)
        self.false_count += int(not notification.inside)
        super()._broadcast(notification)


class RuleEventHarness:
    def __init__(
        self,
        root: Path,
        username: str,
        password: str,
        base_url: str,
        rtsp_uri: str = "rtsp://127.0.0.1:8554/qa-not-playing",
    ):
        self.store = Store(root / "qa.db")
        self.store.change_account(
            username, PasswordHasher().hash(password), password=password
        )
        # change_account writes both the Argon2 hash and ONVIF credential.
        self.engine = RuleEngine()
        self.hub = AuditHub()
        self.gateway = ONVIFGateway()
        self.now = 1000.0
        self.step_number = 0
        self.base_url = base_url.rstrip("/")
        camera = {
            "id": CAMERA_ID,
            **CameraConfig(name="QA synthetic events", rtsp_url=rtsp_uri).model_dump(),
        }
        self.store.save_camera(camera)
        self.cameras = {CAMERA_ID: camera}
        self.replace_rules([primary_rule(), secondary_rule()])
        self.gateway.configure(
            self.cameras,
            self.store.rules,
            self.base_url,
            event_hub=self.hub,
            device_id=self.store.get("device_id"),
        )

    def replace_rules(self, rules):
        self.hub.clear_camera(CAMERA_ID)
        self.engine.reset_camera(CAMERA_ID)
        for old in self.store.rules(CAMERA_ID):
            self.store.delete_rule(old["id"])
        for rule in rules:
            self.store.save_rule(rule)
        self.refresh_catalog()

    def refresh_catalog(self):
        self.hub.replace_catalog([
            {**r, "camera_name": self.cameras[CAMERA_ID]["name"],
             "camera_enabled": self.cameras[CAMERA_ID].get("enabled", True)}
            for r in self.store.rules(CAMERA_ID)
        ])

    def feed(self, detections, advance=0.2):
        if not math.isfinite(advance) or not 0 <= advance <= 86400:
            raise ValueError("advance must be between 0 and 86400 seconds")
        self.now += advance
        events, present = self.engine.evaluate(
            CAMERA_ID,
            detections,
            self.store.rules(CAMERA_ID),
            self.now,
            image_size=(100, 100),
        )
        # Publish new formal targets before clearing ended targets so a rule
        # stays on when its matching target changes within the same frame.
        for index, event in enumerate(events):
            self.hub.publish_trigger(
                CAMERA_ID, {**event, "event_id": f"qa-{self.step_number}-{index}"}
            )
        self.hub.sync_active(CAMERA_ID, self.engine.active_rules(CAMERA_ID))
        return events, present

    def ensure_primary(self):
        rules = self.store.rules(CAMERA_ID)
        if len(rules) != 1 or rules[0] != json.loads(json.dumps(primary_rule())):
            self.replace_rules([primary_rule()])

    def control(self, payload):
        self.step_number += 1
        before_sequence = self.hub._sequence
        events = []
        present = {}
        scenario = payload.get("scenario")
        action = payload.get("action", "scenario" if scenario else "feed")

        def feed(items, advance=0.2):
            nonlocal present
            emitted, present = self.feed(items, advance)
            events.extend(emitted)

        expected = None
        if action == "scenario":
            if scenario not in SCENARIOS:
                raise ValueError(
                    f"Unknown scenario; choose one of {', '.join(SCENARIOS)}"
                )
            if scenario == "unconfigured":
                self.replace_rules([])
                feed([detection()])
                feed([detection()])
                expected = 0
            elif scenario == "disabled":
                self.replace_rules([primary_rule(enabled=False)])
                feed([detection()])
                feed([detection()])
                expected = 0
            elif scenario in {"outside", "below_threshold", "min_hits"}:
                self.replace_rules([primary_rule()])
                sample = detection(
                    outside=scenario == "outside",
                    confidence=0.2 if scenario == "below_threshold" else 0.95,
                )
                feed([sample])
                if scenario != "min_hits":
                    feed([sample])
                expected = 0
            elif scenario == "valid":
                self.ensure_primary()
                feed([detection()])
                if not events:
                    feed([detection()])
                expected = 1
            elif scenario in {"clear", "clear_cooldown"}:
                feed([], 3.1)
                expected = 0
            elif scenario == "cooldown":
                feed([detection()])
                feed([detection()])
                expected = 0
            elif scenario == "after_cooldown":
                feed([], 61)
                feed([detection()])
                feed([detection()])
                expected = 1
            elif scenario == "multi_rule":
                self.replace_rules([primary_rule(), secondary_rule()])
                feed([detection(), detection("cat")])
                feed([detection(), detection("cat")])
                expected = 3
            elif scenario in {"disable_active", "remove_active"}:
                rid = payload.get("rule_id", PRIMARY_RULE)
                rule = next(
                    (r for r in self.store.rules(CAMERA_ID) if r["id"] == rid), None
                )
                if rule is None:
                    raise ValueError("Rule does not exist")
                if scenario == "disable_active":
                    self.store.save_rule({**rule, "enabled": False})
                else:
                    self.store.delete_rule(rid)
                self.engine.drop_rule(rid)
                self.hub.clear_rule(CAMERA_ID, rid)
                self.refresh_catalog()
                expected = 0
            elif scenario == "disconnect":
                self.engine.reset_camera(CAMERA_ID)
                self.hub.clear_camera(CAMERA_ID)
                expected = 0
            elif scenario == "reset":
                self.replace_rules([primary_rule(), secondary_rule()])
                self.now += 61
                expected = 0
        elif action == "feed":
            samples = payload.get("detections", [])
            if not isinstance(samples, list) or len(samples) > 100:
                raise ValueError("detections must be a list of at most 100 objects")
            for sample in samples:
                box = sample.get("bbox", [])
                if len(box) != 4 or not all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in box
                ):
                    raise ValueError("bbox must contain four finite pixel coordinates")
                if (
                    not isinstance(sample.get("label"), str)
                    or not 0 <= float(sample.get("confidence", -1)) <= 1
                ):
                    raise ValueError(
                        "Each detection requires label and confidence in [0, 1]"
                    )
            feed(samples, float(payload.get("advance", 0.2)))
        elif action == "configure":
            rules = []
            for index, item in enumerate(payload.get("rules", [])):
                if index >= 10:
                    raise ValueError("At most 10 QA rules are allowed")
                rid = item.get(
                    "id", PRIMARY_RULE if index == 0 else f"qa-rule-{index + 1}"
                )
                values = RuleConfig(**item).model_dump()
                rules.append({"id": rid, "camera_id": CAMERA_ID, **values})
            self.replace_rules(rules)
        else:
            raise ValueError("action must be scenario, feed or configure")
        notifications = [
            item for item in self.hub.history if item["sequence"] > before_sequence
        ]
        result = {
            "step": self.step_number,
            "scenario": scenario,
            "formal_events": events,
            "formal_event_count": len(events),
            "notifications": notifications,
            "expected_formal_events": expected,
            "expectation_met": expected is None or len(events) == expected,
            "present": present,
            **self.status(),
        }
        return result

    def status(self):
        rules = self.store.rules(CAMERA_ID)
        return {
            "isolated": True,
            "device_uuid": str(self.gateway.device_uuid),
            "base_url": self.base_url,
            "camera_id": CAMERA_ID,
            "virtual_time": round(self.now, 3),
            "rules": rules,
            "rule_tokens": [r["id"] for r in rules],
            "rule_sensors": self.hub.snapshot()["rules"],
            "engine_active": sorted(self.engine.active_rules(CAMERA_ID)),
            "onvif_active": sorted(self.hub.active),
            "broadcast_totals": {
                "true": self.hub.true_count,
                "false": self.hub.false_count,
            },
            "subscriptions": len(self.hub.snapshot()["subscriptions"]),
            "scenarios": list(SCENARIOS),
        }


def create_app(
    username: str, password: str, base_url: str, rtsp_uri: str | None = None
):
    @asynccontextmanager
    async def lifespan(app):
        with tempfile.TemporaryDirectory(prefix="aix5-onvif-rule-qa-") as directory:
            state = RuleEventHarness(
                Path(directory),
                username,
                password,
                base_url,
                **({"rtsp_uri": rtsp_uri} if rtsp_uri else {}),
            )
            app.state.qa = state
            app.state.auth_lock = asyncio.Lock()
            print(
                json.dumps(
                    {
                        "qa_ready": True,
                        "base_url": base_url,
                        "device_uuid": str(state.gateway.device_uuid),
                        "isolated": True,
                    }
                ),
                flush=True,
            )
            try:
                yield
            finally:
                await state.gateway.stop()
                state.store.close()

    app = FastAPI(
        title="Disposable ONVIF rule QA",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    async def authorize(request, body="", basic_only=False):
        header = request.headers.get("authorization", "")
        if basic_only and not header.lower().startswith("basic "):
            raise OnvifAuthError("QA control requires HTTP Basic")
        async with app.state.auth_lock:
            await asyncio.to_thread(authenticate, app.state.qa.store, header, body)

    def auth_failure(error, soap=False):
        headers = {"WWW-Authenticate": 'Basic realm="AIX5 isolated ONVIF QA"'}
        if soap:
            return Response(
                _fault(str(error), "NotAuthorized"),
                status_code=401,
                media_type="application/soap+xml",
                headers=headers,
            )
        raise HTTPException(401, str(error), headers=headers)

    @app.get("/qa/status")
    async def status(request: Request):
        try:
            await authorize(request, basic_only=True)
        except OnvifAuthError as exc:
            return auth_failure(exc)
        return app.state.qa.status()

    @app.post("/qa/step")
    async def step(request: Request):
        try:
            await authorize(request, basic_only=True)
        except OnvifAuthError as exc:
            return auth_failure(exc)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise TypeError("Request must be a JSON object")
            return app.state.qa.control(payload)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/onvif/{service}")
    async def service(service: str, request: Request):
        if service not in {
            "device_service", "media_service", "events_service", "analytics_service"
        }:
            raise HTTPException(404, "Unknown ONVIF service")
        body = (await request.body()).decode("utf-8", "replace")
        if service != "device_service" or _action_name(body) != "GetSystemDateAndTime":
            try:
                await authorize(request, body)
            except OnvifAuthError as exc:
                return auth_failure(exc, soap=True)
        return Response(
            await app.state.qa.gateway.handle(service, body),
            media_type="application/soap+xml",
        )

    @app.post("/onvif/events/subscriptions/{token}")
    async def subscription(token: str, request: Request):
        body = (await request.body()).decode("utf-8", "replace")
        try:
            await authorize(request, body)
        except OnvifAuthError as exc:
            return auth_failure(exc, soap=True)
        return Response(
            await app.state.qa.gateway.pull(token, _action_name(body), body),
            media_type="application/soap+xml",
        )

    @app.get("/onvif/snapshots/{cid}.jpg")
    async def snapshot(cid: str, request: Request):
        try:
            await authorize(request, basic_only=True)
        except OnvifAuthError as exc:
            return auth_failure(exc)
        if cid != CAMERA_ID:
            raise HTTPException(404, "Unknown QA camera")
        import cv2
        import numpy as np

        image = np.full((100, 160, 3), 32, dtype=np.uint8)
        cv2.putText(
            image,
            "QA EVENTS",
            (14, 55),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (220, 220, 220),
            1,
        )
        _, jpeg = cv2.imencode(".jpg", image)
        return Response(
            jpeg.tobytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    return app


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--base-url", required=True, help="URL reachable by HA, including port 18080"
    )
    parser.add_argument("--credentials-file", type=Path)
    args = parser.parse_args()
    credentials = (
        json.loads(args.credentials_file.read_text()) if args.credentials_file else {}
    )
    username = os.environ.get("AIX5_QA_USERNAME") or credentials.get("username")
    password = os.environ.get("AIX5_QA_PASSWORD") or credentials.get("password")
    if not username or not password:
        parser.error(
            "Provide credentials via --credentials-file or AIX5_QA_USERNAME/AIX5_QA_PASSWORD"
        )
    rtsp_uri = os.environ.get("AIX5_QA_RTSP_URI") or credentials.get("rtsp_uri")
    import uvicorn

    uvicorn.run(
        create_app(username, password, args.base_url, rtsp_uri),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
