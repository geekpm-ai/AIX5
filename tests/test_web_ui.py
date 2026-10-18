from aix5_ai.web_ui import INDEX


def test_onvif_flow_dialog_exposes_all_user_visible_stages_and_close_action():
    """The ONVIF add flow must explain long-running work instead of a small inline note."""
    assert 'id="onvifFlowDialog"' in INDEX
    assert 'aria-label="取消并关闭"' in INDEX
    for stage in ("scan", "auth", "profiles", "save"):
        assert f'data-onvif-flow-stage="{stage}"' in INDEX
    assert "id=\"onvifFlowElapsed\"" in INDEX
    assert "id=\"onvifFlowPercent\"" in INDEX
    assert "id=\"onvifFlowStatus\"" in INDEX


def test_onvif_flow_cancels_requests_and_ignores_stale_results():
    """Closing the progress window must abort the active fetch and guard late responses."""
    assert "controller:new AbortController()" in INDEX
    assert "signal:token.controller.signal" in INDEX
    assert "token.controller.abort()" in INDEX
    assert "onvifFlowIsCurrent(token)" in INDEX
    assert "if(token.kind==='scan'){onvifScanVersion++;" in INDEX


def test_linkage_page_uses_onvif_and_hides_legacy_mqtt_configuration():
    """The UI must guide Home Assistant through ONVIF while retaining backend compatibility."""
    start = INDEX.index('<section id="linkage"')
    end = INDEX.index("</section>", start) + len("</section>")
    linkage = INDEX[start:end]

    assert "ONVIF 联动" in linkage
    assert "onvifGatewayUrl" in linkage
    assert "onvifStatusDetail" in linkage
    assert "loadOnvifStatus()" in linkage
    assert "Home Assistant" in linkage
    for legacy_control in ("mqttHost", "mqttPort", "mqttUser", "mqttPass", "mqttEnabled", "mqttTls"):
        assert legacy_control not in linkage
    assert "saveSettings()" not in linkage
    assert "testMqtt()" not in linkage


def test_linkage_status_refresh_reads_onvif_event_state():
    """The linkage status action should query the ONVIF gateway diagnostics endpoint."""
    assert "async function loadOnvifStatus()" in INDEX
    assert "api('/api/onvif/events')" in INDEX
    assert "PullPoint 订阅" in INDEX


def test_home_assistant_linkage_uses_onvif_without_mqtt_form():
    """The user-facing integration entry point is ONVIF; MQTT stays backend compatible."""
    assert 'ONVIF 联动' in INDEX
    assert 'loadOnvifStatus()' in INDEX
    assert 'id="mqttHost"' not in INDEX
    assert 'id="mqttEnabled"' not in INDEX
    assert 'function saveSettings' not in INDEX
