"""ONVIF Device, Media and PullPoint services for the local AI gateway.

Profile/source identities are stable across restarts. Media URIs are provided
by the output bridge; events originate only from formal RuleEngine episodes.
"""

from __future__ import annotations

import asyncio
import html
import json
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .onvif_events import OnvifEventHub, parse_xsd_duration

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSA = "http://www.w3.org/2005/08/addressing"
WSA_DISCOVERY = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
DISCOVERY = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
TD = "http://www.onvif.org/ver10/device/wsdl"
TR = "http://www.onvif.org/ver10/media/wsdl"
TE = "http://www.onvif.org/ver10/events/wsdl"
TA = "http://www.onvif.org/ver20/analytics/wsdl"
AIX5 = "urn:aix5:analytics"
TT = "http://www.onvif.org/ver10/schema"
TNS1 = "http://www.onvif.org/ver10/topics"
WSTOP = "http://docs.oasis-open.org/wsn/t-1"
WSNT = "http://docs.oasis-open.org/wsn/b-2"
DEVICE_NAME = "Edge-AI-IPC"


def _now():
    return _future(time.time())


def _future(timestamp):
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _x(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _action_name(xml: str) -> str:
    try:
        root = ET.fromstring(xml)
        body = root.find(f"{{{SOAP_ENV}}}Body")
        child = next(iter(body), None) if body is not None else None
        return child.tag.rsplit("}", 1)[-1] if child is not None else ""
    except ET.ParseError:
        return ""


def _tag(xml: str, name: str, default="") -> str:
    try:
        return next(
            (
                node.text or default
                for node in ET.fromstring(xml).iter()
                if node.tag.rsplit("}", 1)[-1] == name
            ),
            default,
        )
    except ET.ParseError:
        return default


def _soap(action: str, body: str, relates_to: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV}" xmlns:a="{WSA}" xmlns:wstop="{WSTOP}" '
        f'xmlns:td="{TD}" xmlns:tr="{TR}" xmlns:tev="{TE}" xmlns:wsnt="{WSNT}" '
        f'xmlns:tan="{TA}" xmlns:aix5="{AIX5}" '
        f'xmlns:tt="{TT}" xmlns:tns1="{TNS1}" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:ter="http://www.onvif.org/ver10/error">'
        f"<s:Header><a:Action>{_x(action)}</a:Action>"
        f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
        "<a:To>http://www.w3.org/2005/08/addressing/anonymous</a:To>"
        + (f"<a:RelatesTo>{_x(relates_to)}</a:RelatesTo>" if relates_to else "")
        + "</s:Header><s:Body>"
        + body
        + "</s:Body></s:Envelope>"
    )


def _fault(reason: str, subcode: str = "ActionNotSupported") -> str:
    return _soap(
        f"{WSA}/fault",
        "<s:Fault><s:Code><s:Value>s:Sender</s:Value>"
        f"<s:Subcode><s:Value>ter:{subcode}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{_x(reason)}</s:Text></s:Reason></s:Fault>',
    )


def _response(namespace: str, prefix: str, operation: str, content: str = "") -> str:
    action_prefix = f"{namespace}/EventPortType" if namespace == TE else namespace
    return _soap(
        f"{action_prefix}/{operation}Response",
        f"<{prefix}:{operation}Response>{content}</{prefix}:{operation}Response>",
    )


def _termination_seconds(value: str, default=600) -> float:
    if value and not value.startswith("P"):
        try:
            expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return max(1, min(expires.timestamp() - time.time(), 86400))
        except ValueError:
            pass
    return parse_xsd_duration(value, default)


MULTICAST = (
    "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type>"
    "<tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
    "<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
)


class ONVIFGateway:
    def __init__(self):
        self.cameras = {}
        self.rules = lambda cid=None: []
        self.discovery_transport = None
        self.base_url = "http://127.0.0.1:8080"
        self.event_hub = OnvifEventHub()
        self.device_uuid = uuid.uuid4()
        self.stream_uri = None
        self.media_profile = None

    @property
    def subscriptions(self):
        return self.event_hub.subscriptions

    def configure(
        self,
        cameras,
        rules,
        base_url: str,
        event_hub: OnvifEventHub | None = None,
        device_id: str | None = None,
        stream_uri=None,
        media_profile=None,
    ):
        self.cameras = cameras
        self.rules = rules
        self.base_url = base_url.rstrip("/")
        if event_hub is not None:
            self.event_hub = event_hub
        if device_id:
            try:
                self.device_uuid = uuid.UUID(device_id)
            except ValueError:
                self.device_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"aix5:{device_id}")
        if stream_uri is not None:
            self.stream_uri = stream_uri
        if media_profile is not None:
            self.media_profile = media_profile
        self.event_hub.replace_catalog(self._catalog())

    def _media_info(self, camera):
        return self.media_profile(camera) if self.media_profile else {}

    def _enabled(self):
        return [
            camera for camera in self.cameras.values() if camera.get("enabled", True)
        ]

    def _profile(self, camera):
        cid = camera["id"]
        info = self._media_info(camera)
        return {
            "token": f"aix5-profile-{cid}",
            "source": f"aix5-source-{cid}",
            "name": camera.get("name", cid),
            "width": int(info.get("width") or camera.get("width") or 1920),
            "height": int(info.get("height") or camera.get("height") or 1080),
            "fps": max(
                1,
                round(
                    info.get("fps")
                    or camera.get("decode_fps")
                    or camera.get("source_fps")
                    or 20
                ),
            ),
            "uri": self.stream_uri(camera)
            if self.stream_uri
            else camera.get("rtsp_url", ""),
        }

    def _camera_for_token(self, xml):
        token = _tag(xml, "ProfileToken")
        return next(
            (c for c in self._enabled() if f"aix5-profile-{c['id']}" == token), None
        )

    def _xaddr(self, service: str):
        return f"{self.base_url}/onvif/{service}"

    def _source_config(self, camera, tag="tt:VideoSourceConfiguration"):
        p = self._profile(camera)
        return (
            f'<{tag} token="{_x(p["source"])}"><tt:Name>{_x(p["name"])}</tt:Name>'
            f"<tt:UseCount>1</tt:UseCount><tt:SourceToken>{_x(p['source'])}</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{p["width"]}" height="{p["height"]}"/></{tag}>'
        )

    def _video_config(self, camera, tag="tt:VideoEncoderConfiguration"):
        p = self._profile(camera)
        return (
            f'<{tag} token="aix5-encoder-{_x(camera["id"])}"><tt:Name>H264</tt:Name>'
            "<tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{p['width']}</tt:Width><tt:Height>{p['height']}</tt:Height></tt:Resolution>"
            f"<tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>{p['fps']}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>"
            f"<tt:H264><tt:GovLength>{p['fps']}</tt:GovLength><tt:H264Profile>Baseline</tt:H264Profile></tt:H264>"
            f"{MULTICAST}<tt:SessionTimeout>PT60S</tt:SessionTimeout></{tag}>"
        )

    def _has_audio(self, camera):
        info = {**camera, **self._media_info(camera)}
        codec = str(info.get("audio_codec") or "").lower()
        return info.get("audio_present") is True and codec in {
            "aac",
            "mp4a-latm",
            "mpeg4-generic",
        }

    def _audio_source_config(self, camera, tag="tt:AudioSourceConfiguration"):
        cid = _x(camera["id"])
        return (
            f'<{tag} token="aix5-audio-source-{cid}"><tt:Name>{_x(camera.get("name", cid))} audio</tt:Name>'
            f"<tt:UseCount>1</tt:UseCount><tt:SourceToken>aix5-audio-source-{cid}</tt:SourceToken></{tag}>"
        )

    def _audio_config(self, camera, tag="tt:AudioEncoderConfiguration"):
        info = {**camera, **self._media_info(camera)}
        rate = max(1, int(info.get("audio_sample_rate") or 48000) // 1000)
        return (
            f'<{tag} token="aix5-audio-encoder-{_x(camera["id"])}"><tt:Name>AAC</tt:Name>'
            "<tt:UseCount>1</tt:UseCount><tt:Encoding>AAC</tt:Encoding><tt:Bitrate>128</tt:Bitrate>"
            f"<tt:SampleRate>{rate}</tt:SampleRate>{MULTICAST}<tt:SessionTimeout>PT60S</tt:SessionTimeout></{tag}>"
        )

    def _profile_xml(self, camera, tag="tr:Profiles"):
        p = self._profile(camera)
        return (
            f'<{tag} token="{_x(p["token"])}" fixed="true"><tt:Name>{_x(p["name"])}</tt:Name>'
            + self._source_config(camera)
            + (self._audio_source_config(camera) if self._has_audio(camera) else "")
            + self._video_config(camera)
            + (self._audio_config(camera) if self._has_audio(camera) else "")
            + self._analytics_config(camera)
            + f"</{tag}>"
        )

    def _rule_config(self, camera, rule, tag="tan:Rule"):
        parameters = {
            "RuleName": rule.get("name", rule["id"]),
            "DisplayName": rule.get("name", rule["id"]),
            "CameraId": camera["id"],
            "CameraName": camera.get("name", camera["id"]),
            "Enabled": "true" if rule.get("enabled", True) else "false",
            "CameraEnabled": "true" if camera.get("enabled", True) else "false",
            "TargetLabels": json.dumps(
                rule.get("target_labels", []), ensure_ascii=False
            ),
        }
        values = "".join(
            f'<tt:SimpleItem Name="{key}" Value="{_x(value)}"/>'
            for key, value in parameters.items()
        )
        return (
            f'<{tag} Name="{_x(rule["id"])}" Type="aix5:DetectionRule">'
            f"<tt:Parameters>{values}</tt:Parameters></{tag}>"
        )

    def _analytics_config(self, camera, tag="tt:VideoAnalyticsConfiguration"):
        rules = "".join(
            self._rule_config(camera, rule, "tt:Rule")
            for rule in self.rules(camera["id"])
        )
        return (
            f'<{tag} token="aix5-analytics-{_x(camera["id"])}">'
            f"<tt:Name>{_x(camera.get('name', camera['id']))}</tt:Name>"
            "<tt:UseCount>1</tt:UseCount><tt:AnalyticsEngineConfiguration/>"
            f"<tt:RuleEngineConfiguration>{rules}</tt:RuleEngineConfiguration></{tag}>"
        )

    def _catalog(self):
        return [
            {
                **rule,
                "camera_id": c["id"],
                "camera_name": c.get("name", c["id"]),
                "camera_enabled": c.get("enabled", True),
            }
            for c in self.cameras.values()
            for rule in self.rules(c["id"])
        ]

    def publish(self, cid: str, rule_id: str, label: str, value: bool):
        if value:
            self.event_hub.publish_trigger(
                cid,
                {
                    "rule_id": rule_id,
                    "target_type": label,
                    "rule_name": next(
                        (
                            r.get("name", rule_id)
                            for r in self.rules(cid)
                            if r.get("id") == rule_id
                        ),
                        rule_id,
                    ),
                },
            )
        else:
            self.event_hub.clear_key(cid, rule_id, label)

    def sync_active(self, cid: str, active: set[tuple[str, str, str]]):
        self.event_hub.sync_active(cid, active)

    async def handle(self, service: str, xml: str, request_base: str | None = None):
        action = _action_name(xml)
        if request_base:
            self.base_url = request_base.rstrip("/")
        if service == "device_service":
            response = self.device(action, xml)
        elif service == "media_service":
            response = self.media(action, xml)
        elif service == "events_service":
            response = await self.events(action, xml)
        elif service == "analytics_service":
            response = self.analytics(action, xml)
        else:
            response = _fault("Unsupported service")
        message_id = _tag(xml, "MessageID")
        if message_id:
            response = response.replace(
                "</s:Header>", f"<a:RelatesTo>{_x(message_id)}</a:RelatesTo></s:Header>"
            )
        return response

    def device(self, action, xml):
        if action == "GetDeviceInformation":
            return _response(
                TD,
                "td",
                action,
                f"<td:Manufacturer>D-Robotics</td:Manufacturer><td:Model>{DEVICE_NAME}</td:Model>"
                f"<td:FirmwareVersion>0.1</td:FirmwareVersion><td:SerialNumber>{self.device_uuid}</td:SerialNumber>"
                "<td:HardwareId>RDK-X5</td:HardwareId>",
            )
        if action == "GetCapabilities":
            return _response(
                TD,
                "td",
                action,
                f"<td:Capabilities><tt:Analytics><tt:XAddr>{_x(self._xaddr('analytics_service'))}</tt:XAddr>"
                "<tt:RuleSupport>true</tt:RuleSupport><tt:AnalyticsModuleSupport>false</tt:AnalyticsModuleSupport></tt:Analytics>"
                f"<tt:Device><tt:XAddr>{_x(self._xaddr('device_service'))}</tt:XAddr></tt:Device>"
                f"<tt:Events><tt:XAddr>{_x(self._xaddr('events_service'))}</tt:XAddr>"
                "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
                "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
                "<tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>"
                f"<tt:Media><tt:XAddr>{_x(self._xaddr('media_service'))}</tt:XAddr><tt:StreamingCapabilities>"
                "<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
                "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media></td:Capabilities>",
            )
        if action == "GetServices":
            services = "".join(
                f"<td:Service><td:Namespace>{ns}</td:Namespace><td:XAddr>{_x(self._xaddr(path))}</td:XAddr>"
                "<td:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></td:Version></td:Service>"
                for ns, path in (
                    (TD, "device_service"),
                    (TR, "media_service"),
                    (TE, "events_service"),
                    (TA, "analytics_service"),
                )
            )
            return _response(TD, "td", action, services)
        if action == "GetSystemDateAndTime":
            now = datetime.now(timezone.utc)
            return _response(
                TD,
                "td",
                action,
                "<td:SystemDateAndTime><tt:DateTimeType>Manual</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
                "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone><tt:UTCDateTime><tt:Time>"
                f"<tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>"
                f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>"
                "</tt:UTCDateTime></td:SystemDateAndTime>",
            )
        if action == "GetScopes":
            return _response(
                TD,
                "td",
                action,
                "".join(
                    f"<td:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{scope}</tt:ScopeItem></td:Scopes>"
                    for scope in self._scopes()
                ),
            )
        if action == "GetNetworkInterfaces":
            # HA recognizes this fault text and uses the persistent serial
            # number instead of inventing a MAC for the software gateway.
            return _fault("GetNetworkInterfaces not implemented")
        return _fault(f"Unsupported device operation: {action}")

    def media(self, action, xml):
        if action == "GetVideoAnalyticsConfigurations":
            # Saved configurations are the authoritative rule catalog, including
            # disabled cameras whose video Profile is temporarily unavailable.
            return _response(
                TR,
                "tr",
                action,
                "".join(
                    self._analytics_config(camera, "tr:Configurations")
                    for camera in self.cameras.values()
                ),
            )
        if action == "GetProfiles":
            return _response(
                TR, "tr", action, "".join(self._profile_xml(c) for c in self._enabled())
            )
        if action == "GetProfile":
            camera = self._camera_for_token(xml)
            return (
                _response(TR, "tr", action, self._profile_xml(camera, "tr:Profile"))
                if camera
                else _fault("Unknown profile", "NoProfile")
            )
        if action == "GetVideoSources":
            sources = []
            for camera in self._enabled():
                p = self._profile(camera)
                sources.append(
                    f'<tr:VideoSources token="{_x(p["source"])}"><tt:Framerate>{p["fps"]}</tt:Framerate>'
                    f"<tt:Resolution><tt:Width>{p['width']}</tt:Width><tt:Height>{p['height']}</tt:Height></tt:Resolution></tr:VideoSources>"
                )
            return _response(TR, "tr", action, "".join(sources))
        configs = {
            "GetVideoSourceConfigurations": self._source_config,
            "GetVideoEncoderConfigurations": self._video_config,
            "GetAudioSourceConfigurations": self._audio_source_config,
            "GetAudioEncoderConfigurations": self._audio_config,
        }
        if action in configs:
            return _response(
                TR,
                "tr",
                action,
                "".join(
                    configs[action](c, "tr:Configurations")
                    for c in self._enabled()
                    if "Audio" not in action or self._has_audio(c)
                ),
            )
        if action == "GetAudioSources":
            return _response(
                TR,
                "tr",
                action,
                "".join(
                    f'<tr:AudioSources token="aix5-audio-source-{_x(c["id"])}"><tt:Channels>1</tt:Channels></tr:AudioSources>'
                    for c in self._enabled()
                    if self._has_audio(c)
                ),
            )
        if action in {"GetStreamUri", "GetSnapshotUri"}:
            camera = self._camera_for_token(xml)
            if camera is None:
                return _fault("Unknown profile", "NoProfile")
            uri = (
                self._profile(camera)["uri"]
                if action == "GetStreamUri"
                else f"{self.base_url}/onvif/snapshots/{camera['id']}.jpg"
            )
            if not uri:
                return _fault("Video output is not available", "NoStream")
            return _response(
                TR,
                "tr",
                action,
                f"<tr:MediaUri><tt:Uri>{_x(uri)}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout></tr:MediaUri>",
            )
        if action == "GetServiceCapabilities":
            return _response(
                TR,
                "tr",
                action,
                '<tr:Capabilities SnapshotUri="true" Rotation="false">'
                f'<tr:ProfileCapabilities MaximumNumberOfProfiles="{max(1, len(self._enabled()))}"/>'
                '<tr:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true"/>'
                "</tr:Capabilities>",
            )
        return _fault(f"Unsupported media operation: {action}")

    def analytics(self, action, xml):
        if action == "GetServiceCapabilities":
            return _response(
                TA,
                "tan",
                action,
                '<tan:Capabilities RuleSupport="true" AnalyticsModuleSupport="false" '
                'CellBasedSceneDescriptionSupported="false"/>',
            )
        if action in {"GetRules", "GetSupportedRules"}:
            token = _tag(xml, "ConfigurationToken")
            camera = next(
                (
                    item
                    for item in self.cameras.values()
                    if token == f"aix5-analytics-{item['id']}"
                ),
                None,
            )
            if camera is None:
                return _fault("Unknown analytics configuration", "NoConfig")
            if action == "GetRules":
                return _response(
                    TA,
                    "tan",
                    action,
                    "".join(
                        self._rule_config(camera, rule)
                        for rule in self.rules(camera["id"])
                    ),
                )
            # Read-only metadata for the local rule editor's vendor-defined
            # DetectionRule. ONVIF Create/Modify/Delete operations fault rather
            # than advertising a second configuration authority.
            fields = (
                ("RuleName", "xs:string"),
                ("DisplayName", "xs:string"),
                ("CameraId", "xs:string"),
                ("CameraName", "xs:string"),
                ("Enabled", "xs:boolean"),
                ("CameraEnabled", "xs:boolean"),
                ("TargetLabels", "xs:string"),
            )
            description = "".join(
                f'<tt:SimpleItemDescription Name="{name}" Type="{kind}"/>'
                for name, kind in fields
            )
            return _response(
                TA,
                "tan",
                action,
                '<tan:SupportedRules><tt:RuleDescription Name="aix5:DetectionRule">'
                f"<tt:Parameters>{description}</tt:Parameters></tt:RuleDescription></tan:SupportedRules>",
            )
        return _fault(f"Unsupported analytics operation: {action}")

    async def events(self, action, xml):
        if action == "GetServiceCapabilities":
            return _response(
                TE,
                "tev",
                action,
                '<tev:Capabilities WSSubscriptionPolicySupport="false" WSPullPointSupport="true" '
                'WSPausableSubscriptionManagerInterfaceSupport="false" MaxPullPoints="32" PersistentNotificationStorage="false"/>',
            )
        if action == "GetEventProperties":
            return _response(
                TE,
                "tev",
                action,
                "<tev:TopicNamespaceLocation>http://www.onvif.org/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>"
                "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet><wstop:TopicSet><tns1:RuleEngine>"
                '<FieldDetector><ObjectsInside wstop:topic="true"><tt:MessageDescription IsProperty="true"><tt:Source>'
                '<tt:SimpleItemDescription Name="VideoSourceConfigurationToken" Type="tt:ReferenceToken"/>'
                '<tt:SimpleItemDescription Name="VideoAnalyticsConfigurationToken" Type="tt:ReferenceToken"/>'
                '<tt:SimpleItemDescription Name="Rule" Type="xs:string"/></tt:Source>'
                '<tt:Data><tt:SimpleItemDescription Name="IsInside" Type="xs:boolean"/></tt:Data>'
                "</tt:MessageDescription></ObjectsInside></FieldDetector></tns1:RuleEngine></wstop:TopicSet>"
                "<wsnt:TopicExpressionDialect>http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</wsnt:TopicExpressionDialect>"
                "<wsnt:TopicExpressionDialect>http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet</wsnt:TopicExpressionDialect>"
                "<tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>"
                "<tev:MessageContentSchemaLocation>http://www.onvif.org/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation>",
            )
        if action == "CreatePullPointSubscription":
            timeout = _termination_seconds(_tag(xml, "InitialTerminationTime"))
            try:
                token = await self.event_hub.subscribe(timeout, self._catalog())
            except RuntimeError:
                return _fault(
                    "PullPoint subscription limit reached", "MaxPullPointsReached"
                )
            ref = f"{self.base_url}/onvif/events/subscriptions/{token}"
            return _response(
                TE,
                "tev",
                action,
                f"<tev:SubscriptionReference><a:Address>{_x(ref)}</a:Address></tev:SubscriptionReference>"
                f"<wsnt:CurrentTime>{_now()}</wsnt:CurrentTime><wsnt:TerminationTime>{_future(self.subscriptions[token].expires_at)}</wsnt:TerminationTime>",
            )
        return _fault(f"Unsupported events operation: {action}")

    async def pull(self, token, action, xml):
        try:
            if action == "PullMessages":
                try:
                    limit = min(100, max(1, int(_tag(xml, "MessageLimit", "10"))))
                except ValueError:
                    return _fault("MessageLimit must be an integer", "InvalidArgVal")
                timeout = parse_xsd_duration(_tag(xml, "Timeout", "PT10S"), 10)
                notifications = await self.event_hub.pull(token, timeout, limit)
                messages = "".join(
                    ET.tostring(item.message_element(), encoding="unicode")
                    for item in notifications
                )
                expires = self.subscriptions[token].expires_at
                return _soap(
                    f"{TE}/PullPointSubscription/PullMessagesResponse",
                    f"<tev:PullMessagesResponse><tev:CurrentTime>{_now()}</tev:CurrentTime>"
                    f"<tev:TerminationTime>{_future(expires)}</tev:TerminationTime>{messages}</tev:PullMessagesResponse>",
                )
            if action == "SetSynchronizationPoint":
                # Disabled rules remain false properties; only deletion removes
                # their identity from the catalog.
                self.event_hub.replace_catalog(self._catalog())
                await self.event_hub.synchronize(token)
                return _soap(
                    f"{TE}/PullPointSubscription/SetSynchronizationPointResponse",
                    "<tev:SetSynchronizationPointResponse/>",
                )
            if action == "Renew":
                expires = self.event_hub.renew(
                    token, _termination_seconds(_tag(xml, "TerminationTime"))
                )
                return _soap(
                    "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewResponse",
                    "<wsnt:RenewResponse>"
                    f"<wsnt:TerminationTime>{_future(expires)}</wsnt:TerminationTime><wsnt:CurrentTime>{_now()}</wsnt:CurrentTime></wsnt:RenewResponse>",
                )
            if action == "Unsubscribe":
                self.event_hub.unsubscribe(token)
                return _soap(
                    "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeResponse",
                    "<wsnt:UnsubscribeResponse/>",
                )
        except KeyError:
            return _fault("Subscription expired", "ResourceUnknown")
        return _fault(f"Unsupported subscription operation: {action}")

    def _scopes(self):
        return [
            # Home Assistant searches this ONVIF media-service scope explicitly.
            "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/type/video_encoder",
            f"onvif://www.onvif.org/name/{DEVICE_NAME}",
            "onvif://www.onvif.org/hardware/RDK-X5",
        ]

    def discovery_response(self, data: bytes) -> bytes | None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return None
        probe = root.find(f"{{{SOAP_ENV}}}Body/{{{DISCOVERY}}}Probe")
        resolve = root.find(f"{{{SOAP_ENV}}}Body/{{{DISCOVERY}}}Resolve")
        if probe is None and resolve is None:
            return None
        ns = (
            WSA_DISCOVERY
            if root.find(f"{{{SOAP_ENV}}}Header/{{{WSA_DISCOVERY}}}MessageID")
            is not None
            else WSA
        )
        relates = root.findtext(f"{{{SOAP_ENV}}}Header/{{{ns}}}MessageID", "")
        endpoint = f"urn:uuid:{self.device_uuid}"
        if (
            resolve is not None
            and resolve.findtext(f"{{{ns}}}EndpointReference/{{{ns}}}Address")
            != endpoint
        ):
            return None
        operation = "Probe" if probe is not None else "Resolve"
        body = (
            f"<d:{operation}Matches><d:{operation}Match><a:EndpointReference><a:Address>{endpoint}</a:Address></a:EndpointReference>"
            "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
            f"<d:Scopes>{' '.join(self._scopes())}</d:Scopes><d:XAddrs>{_x(self._xaddr('device_service'))}</d:XAddrs>"
            f"<d:MetadataVersion>1</d:MetadataVersion></d:{operation}Match></d:{operation}Matches>"
        )
        return (
            f'<?xml version="1.0"?><s:Envelope xmlns:s="{SOAP_ENV}" xmlns:a="{ns}" xmlns:d="{DISCOVERY}" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header>'
            f"<a:Action>{DISCOVERY}/{operation}Matches</a:Action><a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
            f"<a:RelatesTo>{_x(relates)}</a:RelatesTo><a:To>{ns}{'/role' if ns == WSA_DISCOVERY else ''}/anonymous</a:To>"
            f"</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        ).encode()

    async def start_discovery(self):
        if self.discovery_transport:
            return
        gateway = self

        class Protocol(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                packet = gateway.discovery_response(data)
                if packet:
                    self.transport.sendto(packet, addr)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", 3702))
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton("239.255.255.250") + socket.inet_aton("0.0.0.0"),
            )
            (
                self.discovery_transport,
                _,
            ) = await asyncio.get_running_loop().create_datagram_endpoint(
                Protocol, sock=sock
            )
        except OSError:
            sock.close()
            self.discovery_transport = None

    async def stop(self):
        if self.discovery_transport:
            self.discovery_transport.close()
            self.discovery_transport = None
        for token in list(self.subscriptions):
            self.event_hub.unsubscribe(token)


gateway = ONVIFGateway()
