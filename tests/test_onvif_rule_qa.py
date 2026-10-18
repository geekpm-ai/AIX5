"""The isolated HA harness must exercise the real rule/auth/event path."""

from fastapi.testclient import TestClient

from tools.verify_onvif_rule_events import PRIMARY_RULE, create_app

SOAP = "http://www.w3.org/2003/05/soap-envelope"
EVENTS = "http://www.onvif.org/ver10/events/wsdl"


def soap_request(action, namespace=EVENTS):
    return (
        f'<s:Envelope xmlns:s="{SOAP}" xmlns:t="{namespace}">'
        f"<s:Body><t:{action}/></s:Body></s:Envelope>"
    )


def test_harness_rules_drive_only_formal_edges_and_keep_subscriptions():
    app = create_app("qa-user", "test-password-only", "http://qa.local:18080")
    with TestClient(app) as client:
        auth = ("qa-user", "test-password-only")
        assert client.get("/qa/status").status_code == 401
        assert client.get("/qa/status", auth=("qa-user", "wrong")).status_code == 401
        before = client.get("/qa/status", auth=auth).json()
        temp_root = app.state.qa.store.root
        assert before["isolated"]
        assert len(before["rule_tokens"]) == 2
        reply = client.post(
            "/onvif/events_service",
            content=soap_request("CreatePullPointSubscription"),
            auth=auth,
        )
        assert reply.status_code == 200
        subscription = next(iter(app.state.qa.hub.subscriptions))

        def step(scenario, **kwargs):
            reply = client.post(
                "/qa/step", json={"scenario": scenario, **kwargs}, auth=auth
            )
            assert reply.status_code == 200, reply.text
            result = reply.json()
            assert result["expectation_met"], result
            assert result["device_uuid"] == before["device_uuid"]
            assert subscription in app.state.qa.hub.subscriptions
            return result

        for scenario in (
            "unconfigured",
            "disabled",
            "outside",
            "below_threshold",
            "min_hits",
        ):
            result = step(scenario)
            assert result["formal_event_count"] == 0
            assert result["broadcast_totals"]["true"] == 0
            assert not result["onvif_active"]
        assert len(app.state.qa.engine.states) == 2
        assert (
            app.state.qa.engine.states[("qa-event-camera", PRIMARY_RULE, "person")].hits
            == 1
        )
        result = step("valid")
        assert result["formal_event_count"] == 1
        assert result["notifications"][-1]["inside"]
        result = step("clear")
        assert result["notifications"][-1]["inside"] is False
        result = step("cooldown")
        assert result["engine_active"]
        assert not result["onvif_active"]
        assert result["broadcast_totals"]["true"] == 1
        step("clear_cooldown")
        result = step("after_cooldown")
        assert result["formal_event_count"] == 1
        result = step("multi_rule")
        assert result["formal_event_count"] == 3
        assert len(result["onvif_active"]) == 3
        result = step("disable_active")
        assert len(result["onvif_active"]) == 1
        assert all(not item["inside"] for item in result["notifications"])
        result = step("remove_active", rule_id="qa-rule-secondary")
        assert not result["onvif_active"]
        step("multi_rule")
        result = step("disconnect")
        assert not result["engine_active"] and not result["onvif_active"]
    assert not temp_root.exists()


def test_harness_digest_auth_rejects_wrong_password_and_replay():
    from lxml import etree
    from zeep.wsse.username import UsernameToken

    def digest_request(password):
        root = etree.fromstring(soap_request("CreatePullPointSubscription").encode())
        UsernameToken("qa-user", password, use_digest=True).apply(root, {})
        return etree.tostring(root)

    app = create_app("qa-user", "test-password-only", "http://qa.local:18080")
    with TestClient(app) as client:
        assert (
            client.post(
                "/onvif/events_service", content=digest_request("wrong")
            ).status_code
            == 401
        )
        body = digest_request("test-password-only")
        assert client.post("/onvif/events_service", content=body).status_code == 200
        assert client.post("/onvif/events_service", content=body).status_code == 401
        date = soap_request(
            "GetSystemDateAndTime", "http://www.onvif.org/ver10/device/wsdl"
        )
        assert client.post("/onvif/device_service", content=date).status_code == 200
