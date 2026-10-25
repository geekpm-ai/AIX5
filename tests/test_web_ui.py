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


def test_linkage_page_uses_onvif_configuration_only():
    """The UI guides Home Assistant through ONVIF as the sole integration path."""
    start = INDEX.index('<section id="linkage"')
    end = INDEX.index("</section>", start) + len("</section>")
    linkage = INDEX[start:end]

    assert "ONVIF 联动" in linkage
    assert "onvifGatewayUrl" in linkage
    assert "onvifStatusDetail" in linkage
    assert "loadOnvifStatus()" in linkage
    assert "Home Assistant" in linkage
    assert "saveSettings()" not in linkage


def test_linkage_status_refresh_reads_onvif_event_state():
    """The linkage status action should query the ONVIF gateway diagnostics endpoint."""
    assert "async function loadOnvifStatus()" in INDEX
    assert "api('/api/onvif/events')" in INDEX
    assert "PullPoint 订阅" in INDEX


def test_home_assistant_linkage_uses_onvif_only():
    """The user-facing integration entry point is ONVIF."""
    assert 'ONVIF 联动' in INDEX
    assert 'loadOnvifStatus()' in INDEX
    assert 'function saveSettings' not in INDEX


def test_preview_uses_status_dot_and_codec_metadata_without_resource_dock():
    """Preview cards stay unobstructed while retaining accessible status information."""
    assert 'id="resourceDock"' not in INDEX
    assert "previewCodec(camera)" in INDEX
    assert "status-dot ${c.status==='running'?'ok':'bad'}" in INDEX
    assert "SYSTEM LOAD" not in INDEX


def test_events_use_thumbnail_grid_cards():
    """Event history is image-led and keeps filter/time/device metadata."""
    assert 'id="eventRows" class="event-grid"' in INDEX
    assert "event-card" in INDEX
    assert "event-thumb" in INDEX
    assert "eventImageUrl(event)" in INDEX
    assert "formatEventTimestamp(event)" in INDEX
    assert "查看原图" in INDEX


def test_rule_editor_supports_target_absence_durations():
    """Rules can alert after a selected target has been absent continuously."""
    assert 'id="ruleAbsenceSeconds"' in INDEX
    for value in ("60", "300", "600", "1800", "3600"):
        assert f'value="{value}"' in INDEX
    assert "absence_seconds" in INDEX
    assert "持续缺失" in INDEX
