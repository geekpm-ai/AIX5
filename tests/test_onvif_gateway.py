import xml.etree.ElementTree as ET

import pytest

from aix5_ai.onvif_events import OnvifEventHub
from aix5_ai.onvif_gateway import DEVICE_NAME, ONVIFGateway

SOAP = "http://www.w3.org/2003/05/soap-envelope"


def request(action: str, extra: str = "") -> str:
    return f'<s:Envelope xmlns:s="{SOAP}" xmlns:tev="http://www.onvif.org/ver10/events/wsdl"><s:Body><tev:{action}>{extra}</tev:{action}></s:Body></s:Envelope>'


@pytest.mark.asyncio
async def test_gateway_events_use_formal_rule_edges_and_pullpoint_queue():
    hub = OnvifEventHub()
    gateway = ONVIFGateway()
    cid, rid = "camera-1", "rule-1"
    gateway.configure(
        {cid: {"id": cid, "name": "客厅", "enabled": True}},
        lambda camera_id=None: (
            [
                {
                    "id": rid,
                    "name": "有人进入",
                    "enabled": True,
                    "target_labels": ["person"],
                }
            ]
            if camera_id == cid
            else []
        ),
        "http://aix5.local:8080",
        event_hub=hub,
    )

    props = await gateway.handle("events_service", request("GetEventProperties"))
    ET.fromstring(props)
    assert "ObjectsInside" in props
    subscribed = await gateway.handle(
        "events_service", request("CreatePullPointSubscription")
    )
    ET.fromstring(subscribed)
    token = next(iter(hub.subscriptions))

    # A real rule edge is the only event that enters the PullPoint queue.
    assert hub.publish_trigger(
        cid,
        {
            "rule_id": rid,
            "rule_name": "有人进入",
            "target_type": "person",
            "event_type": "target_detected",
            "event_id": "local-event-1",
        },
    )
    pulled = await gateway.pull(
        token,
        "PullMessages",
        request(
            "PullMessages",
            "<tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>5</tev:MessageLimit>",
        ),
    )
    ET.fromstring(pulled)
    assert 'Name="IsInside" Value="true"' in pulled
    assert 'Name="Rule" Value="rule-1"' in pulled
    assert 'Name="EventId" Value="local-event-1"' in pulled

    # Clearing the RuleEngine state emits one false edge for the same sensor.
    hub.sync_active(cid, set())
    pulled = await gateway.pull(
        token,
        "PullMessages",
        request(
            "PullMessages",
            "<tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>5</tev:MessageLimit>",
        ),
    )
    assert 'Name="IsInside" Value="false"' in pulled


@pytest.mark.asyncio
async def test_pullpoint_initial_sync_clears_stale_home_assistant_state():
    hub = OnvifEventHub()
    gateway = ONVIFGateway()
    gateway.configure(
        {"camera-1": {"id": "camera-1", "enabled": True}},
        lambda camera_id=None: [
            {
                "id": "rule-1",
                "name": "有人",
                "enabled": True,
                "target_labels": ["person"],
            }
        ],
        "http://aix5.local:8080",
        event_hub=hub,
    )
    await gateway.handle("events_service", request("CreatePullPointSubscription"))
    token = next(iter(hub.subscriptions))
    pulled = await gateway.pull(
        token,
        "PullMessages",
        request(
            "PullMessages",
            "<tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>5</tev:MessageLimit>",
        ),
    )
    assert 'Name="IsInside" Value="false"' in pulled


@pytest.mark.asyncio
async def test_gateway_media_profile_has_stable_h264_profile():
    gateway = ONVIFGateway()
    cid = "camera-1"
    gateway.configure(
        {
            cid: {
                "id": cid,
                "name": "客厅",
                "enabled": True,
                "width": 1920,
                "height": 1080,
                "source_fps": 20,
                "rtsp_url": "rtsp://camera.example/live",
            }
        },
        lambda camera_id=None: [],
        "http://aix5.local:8080",
    )
    response = await gateway.handle("media_service", request("GetProfiles"))
    assert f"aix5-profile-{cid}" in response
    assert "<tt:Encoding>H264</tt:Encoding>" in response
    stream = await gateway.handle(
        "media_service",
        request(
            "GetStreamUri",
            f'<tr:ProfileToken xmlns:tr="http://www.onvif.org/ver10/media/wsdl">aix5-profile-{cid}</tr:ProfileToken>',
        ),
    )
    assert "rtsp://camera.example/live" in stream


@pytest.mark.asyncio
async def test_gateway_advertises_aac_only_when_input_audio_is_aac():
    gateway = ONVIFGateway()
    gateway.configure(
        {
            "aac": {
                "id": "aac",
                "name": "带声音",
                "enabled": True,
                "audio_present": True,
                "audio_codec": "aac",
            },
            "g711": {
                "id": "g711",
                "name": "其他编码",
                "enabled": True,
                "audio_present": True,
                "audio_codec": "pcm_alaw",
            },
        },
        lambda camera_id=None: [],
        "http://aix5.local:8080",
    )
    response = await gateway.handle("media_service", request("GetProfiles"))
    assert "aix5-audio-encoder-aac" in response
    assert "aix5-audio-encoder-g711" not in response


@pytest.mark.asyncio
async def test_gateway_device_capabilities_are_well_formed():
    gateway = ONVIFGateway()
    gateway.configure({}, lambda camera_id=None: [], "http://aix5.local:8080")
    response = await gateway.handle("device_service", request("GetCapabilities"))
    ET.fromstring(response)
    assert "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>" in response
    assert "http://aix5.local:8080/onvif/media_service" in response


@pytest.mark.asyncio
async def test_gateway_uses_distinct_edge_identity():
    gateway = ONVIFGateway()
    gateway.configure({}, lambda camera_id=None: [], "http://aix5.local:8080")
    response = await gateway.handle("device_service", request("GetDeviceInformation"))
    ET.fromstring(response)
    assert DEVICE_NAME in response


@pytest.mark.asyncio
async def test_gateway_advertises_aac_after_video_profile_in_schema_order():
    gateway = ONVIFGateway()
    gateway.configure(
        {
            "camera-1": {
                "id": "camera-1",
                "name": "客厅",
                "enabled": True,
                "audio_present": True,
                "audio_codec": "aac",
            }
        },
        lambda camera_id=None: [],
        "http://aix5.local:8080",
    )
    response = await gateway.handle("media_service", request("GetProfiles"))
    ET.fromstring(response)
    assert (
        response.index("AudioSourceConfiguration")
        < response.index("VideoEncoderConfiguration")
        < response.index("AudioEncoderConfiguration")
    )


@pytest.fixture(scope="module")
def onvif_schemas():
    """Use the actual installed ONVIF WSDL/XSD, not string-only assertions."""
    from pathlib import Path

    import onvif
    import zeep
    from lxml import etree

    wsdl_dir = Path(onvif.__file__).resolve().parent.parent / "wsdl"
    schemas = {}
    for service, filename in (
        ("device_service", "devicemgmt.wsdl"),
        ("media_service", "media.wsdl"),
        ("events_service", "events.wsdl"),
        ("analytics_service", "analytics.wsdl"),
    ):
        path = wsdl_dir / filename
        schema_node = etree.parse(str(path)).find(
            ".//{http://www.w3.org/2001/XMLSchema}schema"
        )
        # Detach with inherited xmlns declarations and retain relative-import base.
        schema_node = etree.fromstring(etree.tostring(schema_node), base_url=str(path))
        schemas[service] = (etree.XMLSchema(schema_node), zeep.Client(str(path)))
    schemas["subscription"] = (
        etree.XMLSchema(etree.parse(str(wsdl_dir / "b-2.xsd"))),
        schemas["events_service"][1],
    )
    return schemas


def validate_soap(response, schema_and_client):
    from lxml import etree

    schema, client = schema_and_client
    root = etree.fromstring(response.encode())
    body = root.find(f"{{{SOAP}}}Body")[0]
    schema.assertValid(body)
    # The same decoder used by onvif-zeep/HA must consume the complete body.
    element = client.wsdl.types.get_element(body.tag)
    decoded = element.parse(body, client.wsdl.types)
    assert not getattr(decoded, "_raw_elements", None)
    return decoded


@pytest.mark.asyncio
async def test_device_and_media_responses_validate_against_onvif_xsd(onvif_schemas):
    import uuid
    from datetime import datetime, timezone

    gateway = ONVIFGateway()
    device_id = uuid.uuid4().hex
    gateway.configure(
        {
            "cam": {
                "id": "cam",
                "name": "客厅 & 书桌",
                "audio_present": True,
                "audio_codec": "aac",
                "rtsp_url": "rtsp://camera/source",
            }
        },
        lambda cid: [],
        "http://aix5.local:8080",
        device_id=device_id,
        stream_uri=lambda c: f"rtsp://aix5.local:8554/aix5/{c['id']}",
        media_profile=lambda c: {
            "width": 960,
            "height": 540,
            "audio_present": True,
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "fps": 20,
        },
    )
    for action in (
        "GetDeviceInformation",
        "GetCapabilities",
        "GetServices",
        "GetSystemDateAndTime",
        "GetScopes",
    ):
        response = await gateway.handle("device_service", request(action))
        decoded = validate_soap(response, onvif_schemas["device_service"])
        if action == "GetSystemDateAndTime":
            value = decoded.SystemDateAndTime.UTCDateTime
            actual = datetime(
                value.Date.Year,
                value.Date.Month,
                value.Date.Day,
                value.Time.Hour,
                value.Time.Minute,
                value.Time.Second,
                tzinfo=timezone.utc,
            )
            assert abs((datetime.now(timezone.utc) - actual).total_seconds()) < 2
        if action == "GetDeviceInformation":
            assert decoded.SerialNumber == str(uuid.UUID(device_id))
    for action in (
        "GetProfiles",
        "GetProfile",
        "GetVideoSources",
        "GetAudioSources",
        "GetVideoSourceConfigurations",
        "GetVideoEncoderConfigurations",
        "GetAudioSourceConfigurations",
        "GetAudioEncoderConfigurations",
        "GetStreamUri",
        "GetSnapshotUri",
        "GetServiceCapabilities",
    ):
        response = await gateway.handle(
            "media_service",
            request(action, "<tev:ProfileToken>aix5-profile-cam</tev:ProfileToken>"),
        )
        decoded = validate_soap(response, onvif_schemas["media_service"])
        if action == "GetProfiles":
            profile = decoded.Profiles[0]
            assert profile.VideoEncoderConfiguration.Resolution.Height == 540
            assert profile.AudioEncoderConfiguration.SampleRate == 48
        if action == "GetStreamUri":
            assert decoded.MediaUri.Uri == "rtsp://aix5.local:8554/aix5/cam"
            assert "camera/source" not in response


@pytest.mark.asyncio
async def test_subscription_lifecycle_validates_with_real_onvif_schemas(onvif_schemas):
    from datetime import datetime, timezone

    gateway = ONVIFGateway()
    gateway.configure(
        {"cam": {"id": "cam"}},
        lambda cid: [
            {"id": "rule", "target_labels": ["person", "cat"], "enabled": True}
        ],
        "http://aix5.local:8080",
    )
    for action in (
        "GetEventProperties",
        "GetServiceCapabilities",
        "CreatePullPointSubscription",
    ):
        response = await gateway.handle(
            "events_service",
            request(
                action, "<tev:InitialTerminationTime>PT30S</tev:InitialTerminationTime>"
            ),
        )
        decoded = validate_soap(response, onvif_schemas["events_service"])
        if action == "CreatePullPointSubscription":
            assert (
                25
                < (decoded.TerminationTime - datetime.now(timezone.utc)).total_seconds()
                <= 30
            )
    token = next(iter(gateway.subscriptions))
    response = await gateway.pull(token, "PullMessages", request("PullMessages"))
    decoded = validate_soap(response, onvif_schemas["events_service"])
    assert len(decoded.NotificationMessage) == 1
    assert response.count('PropertyOperation="Initialized"') == 1
    assert 'Name="Rule" Value="rule"' in response
    response = await gateway.pull(
        token, "SetSynchronizationPoint", request("SetSynchronizationPoint")
    )
    validate_soap(response, onvif_schemas["events_service"])
    response = await gateway.pull(
        token,
        "Renew",
        request("Renew", "<tev:TerminationTime>PT2M</tev:TerminationTime>"),
    )
    decoded = validate_soap(response, onvif_schemas["subscription"])
    assert (
        115
        < (decoded.TerminationTime - datetime.now(timezone.utc)).total_seconds()
        <= 120
    )
    response = await gateway.pull(token, "Unsubscribe", request("Unsubscribe"))
    validate_soap(response, onvif_schemas["subscription"])
    assert token not in gateway.subscriptions
    response = await gateway.pull(token, "PullMessages", request("PullMessages"))
    assert "Subscription expired" in response


def test_discovery_has_valid_stable_identity_qnames_and_correlation():
    import uuid

    from lxml import etree

    from aix5_ai.onvif_gateway import DISCOVERY, WSA_DISCOVERY

    device_id = uuid.uuid4().hex
    gateway = ONVIFGateway()
    gateway.configure({}, lambda cid: [], "http://aix5.local:8080", device_id=device_id)
    probe = (
        f'<s:Envelope xmlns:s="{SOAP}" xmlns:a="{WSA_DISCOVERY}" xmlns:d="{DISCOVERY}">'
        "<s:Header><a:MessageID>urn:uuid:request-id</a:MessageID></s:Header>"
        "<s:Body><d:Probe/></s:Body></s:Envelope>"
    ).encode()
    root = etree.fromstring(gateway.discovery_response(probe))
    ns = {"a": WSA_DISCOVERY, "d": DISCOVERY, "s": SOAP}
    assert root.findtext("s:Header/a:RelatesTo", namespaces=ns) == "urn:uuid:request-id"
    assert (
        root.findtext(".//a:EndpointReference/a:Address", namespaces=ns)
        == f"urn:uuid:{uuid.UUID(device_id)}"
    )
    types = root.find(".//d:Types", namespaces=ns)
    assert (
        types.nsmap[types.text.split(":")[0]]
        == "http://www.onvif.org/ver10/network/wsdl"
    )
    assert DEVICE_NAME in root.findtext(".//d:Scopes", namespaces=ns)


@pytest.mark.asyncio
async def test_synchronizing_false_states_wakes_pending_pull():
    import asyncio

    from aix5_ai.onvif_events import OnvifEventHub

    hub = OnvifEventHub()
    token = await hub.subscribe(
        initial=[
            {
                "camera_id": "cam",
                "id": "rule",
                "name": "presence",
                "target_labels": ["person"],
            }
        ]
    )
    await hub.pull(token, 0, 10)
    waiting = asyncio.create_task(hub.pull(token, 60, 10))
    await asyncio.sleep(0)
    await hub.synchronize(token)
    notifications = await asyncio.wait_for(waiting, 0.2)
    assert notifications[0].inside is False
    assert notifications[0].property_operation == "Initialized"


@pytest.mark.asyncio
async def test_analytics_catalog_validates_and_retains_disabled_rules(onvif_schemas):
    import json

    from aix5_ai.onvif_gateway import TA

    gateway = ONVIFGateway()
    cameras = {
        "cam": {"id": "cam", "name": "客厅", "enabled": True},
        "off": {"id": "off", "name": "书房", "enabled": False},
    }
    rules = {
        "cam": [
            {"id": "r1", "name": "人或猫", "target_labels": ["person", "cat"]},
            {"id": "r2", "name": "已停用", "target_labels": ["dog"], "enabled": False},
        ],
        "off": [{"id": "r3", "name": "书房检测", "target_labels": ["person"]}],
    }
    gateway.configure(cameras, lambda cid: rules[cid], "http://aix5.local")
    response = await gateway.handle("device_service", request("GetCapabilities"))
    capabilities = validate_soap(response, onvif_schemas["device_service"])
    assert (
        capabilities.Capabilities.Analytics.XAddr
        == "http://aix5.local/onvif/analytics_service"
    )
    services = validate_soap(
        await gateway.handle("device_service", request("GetServices")),
        onvif_schemas["device_service"],
    )
    assert TA in [item.Namespace for item in services.Service]

    configs = validate_soap(
        await gateway.handle(
            "media_service", request("GetVideoAnalyticsConfigurations")
        ),
        onvif_schemas["media_service"],
    )
    assert [item.token for item in configs.Configurations] == [
        "aix5-analytics-cam",
        "aix5-analytics-off",
    ]
    assert configs.Configurations[1].Name == "书房"
    for action in ["GetRules", "GetSupportedRules", "GetServiceCapabilities"]:
        reply = await gateway.handle(
            "analytics_service",
            request(
                action,
                "<tev:ConfigurationToken>aix5-analytics-cam</tev:ConfigurationToken>",
            ),
        )
        decoded = validate_soap(reply, onvif_schemas["analytics_service"])
        if action == "GetRules":
            assert [item.Name for item in decoded.Rule] == ["r1", "r2"]
            params = {
                item.Name: item.Value for item in decoded.Rule[0].Parameters.SimpleItem
            }
            assert params["RuleName"] == "人或猫"
            assert params["CameraId"] == "cam"
            assert json.loads(params["TargetLabels"]) == ["person", "cat"]
            disabled = {
                item.Name: item.Value for item in decoded.Rule[1].Parameters.SimpleItem
            }
            assert disabled["Enabled"] == "false"

    rules["cam"][0]["name"] = "改名后 & <目标>"
    renamed = validate_soap(
        await gateway.handle(
            "analytics_service",
            request(
                "GetRules",
                "<tev:ConfigurationToken>aix5-analytics-cam</tev:ConfigurationToken>",
            ),
        ),
        onvif_schemas["analytics_service"],
    )
    assert renamed.Rule[0].Name == "r1"
    assert {item.Name: item.Value for item in renamed.Rule[0].Parameters.SimpleItem}[
        "RuleName"
    ] == "改名后 & <目标>"
    rules["cam"].clear()
    empty = validate_soap(
        await gateway.handle(
            "analytics_service",
            request(
                "GetRules",
                "<tev:ConfigurationToken>aix5-analytics-cam</tev:ConfigurationToken>",
            ),
        ),
        onvif_schemas["analytics_service"],
    )
    assert not empty.Rule
    unknown = await gateway.handle(
        "analytics_service",
        request("GetRules", "<tev:ConfigurationToken>missing</tev:ConfigurationToken>"),
    )
    assert "Fault" in unknown and "NoConfig" in unknown


@pytest.mark.asyncio
async def test_gateway_catalog_changes_publish_one_rule_lifecycle_property():
    gateway = ONVIFGateway()
    cameras = {"cam": {"id": "cam", "name": "客厅", "enabled": False}}
    saved = []
    gateway.configure(cameras, lambda cid: saved, "http://aix5.local")
    token = await gateway.event_hub.subscribe()
    saved.append({"id": "r1", "name": "一条规则", "target_labels": ["person", "cat"]})
    gateway.configure(cameras, lambda cid: saved, "http://aix5.local")
    created = await gateway.event_hub.pull(token, 0, 10)
    assert len(created) == 1
    assert created[0].property_operation == "Initialized"
    assert not created[0].inside
    assert len(gateway._catalog()) == 1
    saved[0]["name"] = "新名称"
    gateway.configure(cameras, lambda cid: saved, "http://aix5.local")
    renamed = await gateway.event_hub.pull(token, 0, 10)
    assert len(renamed) == 1 and renamed[0].property_operation == "Changed"
    assert renamed[0].rule_id == "r1" and renamed[0].rule_name == "新名称"
    saved.clear()
    gateway.configure(cameras, lambda cid: saved, "http://aix5.local")
    deleted = await gateway.event_hub.pull(token, 0, 10)
    assert len(deleted) == 1 and deleted[0].property_operation == "Deleted"


@pytest.mark.asyncio
async def test_unsupported_push_subscription_is_a_real_zeep_fault(onvif_schemas):
    from requests import Response
    from zeep.exceptions import Fault

    gateway = ONVIFGateway()
    reply = await gateway.handle("events_service", request("Subscribe"))
    client = onvif_schemas["events_service"][1]
    binding = next(
        binding
        for binding in client.wsdl.bindings.values()
        if "Subscribe" in binding._operations
    )
    response = Response()
    response.status_code = 200
    response._content = reply.encode()
    response.headers["Content-Type"] = "application/soap+xml"
    # HA catches a zeep Fault to fall back from webhook subscriptions to
    # PullPoint. An empty successful response instead crashes its setup.
    with pytest.raises(Fault, match="Unsupported events operation: Subscribe") as error:
        binding.process_reply(client, binding.get("Subscribe"), response)
    assert str(error.value.subcodes[0]).endswith("}ActionNotSupported")
