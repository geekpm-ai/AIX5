from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import onvif

from aix5_ai.onvif import _probe_sync, parse_discovery_response


def test_probe_uses_installed_wsdl_and_encodes_rtsp_credentials(monkeypatch):
    calls = []
    profile = SimpleNamespace(token="p", Name="sub", VideoEncoderConfiguration=None)
    media = SimpleNamespace(
        GetProfiles=lambda: [profile],
        create_type=lambda name: SimpleNamespace(),
        GetStreamUri=lambda request: SimpleNamespace(
            Uri="rtsp://old:returned@127.0.0.1:554/live"
        ),
    )

    def camera(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            devicemgmt=SimpleNamespace(GetDeviceInformation=lambda: SimpleNamespace()),
            create_media_service=lambda: media,
        )

    monkeypatch.setattr(onvif, "ONVIFCamera", camera)
    result = _probe_sync("127.0.0.1", 80, "user:@", "p@ss:/?#")
    url = urlsplit(result["profiles"][0]["rtsp_url"])
    assert unquote(url.username) == "user:@"
    assert unquote(url.password) == "p@ss:/?#"
    assert url.hostname == "127.0.0.1"
    assert "wsdl_dir" not in calls[0]


def test_discovery_ignores_non_camera_devices():
    assert (
        parse_discovery_response(
            "<Envelope><Types>Printer</Types></Envelope>", "127.0.0.1"
        )
        is None
    )
