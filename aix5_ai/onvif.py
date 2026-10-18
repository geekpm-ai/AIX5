from __future__ import annotations
import asyncio, socket, uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SOAP = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><w:MessageID>uuid:{}</w:MessageID>
 <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
 <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


def parse_discovery_response(text, host):
    """Read camera identity from discovery Scopes without authenticating."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    scopes = " ".join(
        el.text or "" for el in root.iter() if el.tag.split("}")[-1] == "Scopes"
    ).split()
    types = " ".join(
        el.text or "" for el in root.iter() if el.tag.split("}")[-1] == "Types"
    )
    xaddrs = " ".join(
        el.text or "" for el in root.iter() if el.tag.split("}")[-1] == "XAddrs"
    ).split()
    # Printers also answer WS-Discovery. Only list ONVIF video devices.
    if not (
        "NetworkVideoTransmitter" in types
        or any(scope.startswith("onvif://") for scope in scopes)
        or any("/onvif/" in address.lower() for address in xaddrs)
    ):
        return None

    def scope_value(kind):
        prefix = "onvif://www.onvif.org/" + kind + "/"
        return next(
            (
                unquote(scope[len(prefix) :])
                for scope in scopes
                if scope.lower().startswith(prefix)
            ),
            "",
        )

    model = scope_value("hardware")
    name = scope_value("name") or model or "未命名 ONVIF 摄像机"
    port = 80
    for address in xaddrs:
        try:
            parts = urlsplit(address)
            if parts.hostname == host:
                port = parts.port or (443 if parts.scheme == "https" else 80)
                break
        except ValueError:
            continue
    return {
        "host": host,
        "address": host,
        "name": name,
        "model": model,
        "port": port,
        "raw": text[:4096],
    }


async def discover(timeout=3):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.2)
    seen = {}
    try:
        sock.sendto(SOAP.format(uuid.uuid4()).encode(), ("239.255.255.250", 3702))
        end = loop.time() + timeout
        while loop.time() < end:
            try:
                data, addr = await asyncio.to_thread(sock.recvfrom, 65535)
            except (TimeoutError, socket.timeout):
                continue
            device = parse_discovery_response(data.decode("utf-8", "ignore"), addr[0])
            if device:
                seen[addr[0]] = device
    finally:
        sock.close()
    return list(seen.values())


def _probe_sync(host, port, username, password, wsdl_dir=None):
    from onvif import ONVIFCamera

    options = {"wsdl_dir": wsdl_dir} if wsdl_dir else {}
    camera = ONVIFCamera(host, int(port), username, password, daemon=False, **options)
    info = camera.devicemgmt.GetDeviceInformation()
    media = camera.create_media_service()
    profiles = []
    for profile in media.GetProfiles():
        request = media.create_type("GetStreamUri")
        request.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        request.ProfileToken = profile.token
        uri = media.GetStreamUri(request).Uri
        parts = urlsplit(uri)
        # Do not expose or persist the password returned by a camera. The caller
        # can construct an authenticated URL from the submitted credentials.
        auth_host = parts.hostname or host
        if ":" in auth_host:
            auth_host = f"[{auth_host}]"
        if parts.port:
            auth_host = f"{auth_host}:{parts.port}"
        authed = urlunsplit(
            (
                parts.scheme,
                f"{quote(username, safe='')}:{quote(password, safe='')}@{auth_host}",
                parts.path,
                parts.query,
                parts.fragment,
            )
        )
        enc = getattr(profile, "VideoEncoderConfiguration", None)
        resolution = getattr(enc, "Resolution", None) if enc else None
        profiles.append(
            {
                "name": profile.Name,
                "token": profile.token,
                "rtsp_url": authed,
                "encoding": str(getattr(enc, "Encoding", "") or ""),
                "width": int(getattr(resolution, "Width", 0) or 0)
                if resolution
                else None,
                "height": int(getattr(resolution, "Height", 0) or 0)
                if resolution
                else None,
                "fps": int(
                    getattr(getattr(enc, "RateControl", None), "FrameRateLimit", 0) or 0
                )
                if enc
                else None,
            }
        )
    return {
        "host": host,
        "port": int(port),
        "device": {
            k: str(getattr(info, k, "") or "")
            for k in (
                "Manufacturer",
                "Model",
                "FirmwareVersion",
                "SerialNumber",
                "HardwareId",
            )
        },
        "profiles": profiles,
    }


async def probe(host, port, username, password):
    return await asyncio.to_thread(_probe_sync, host, port, username, password)
