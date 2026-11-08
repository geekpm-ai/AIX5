import socket
from types import SimpleNamespace

from aix5_ai import network


def test_network_snapshot_exposes_non_loopback_ipv4_and_ipv6(monkeypatch):
    monkeypatch.setattr(network.psutil, "net_if_stats", lambda: {"eth0": SimpleNamespace(isup=True)})
    monkeypatch.setattr(
        network.psutil,
        "net_if_addrs",
        lambda: {
            "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1", netmask="255.0.0.0")],
            "eth0": [
                SimpleNamespace(family=socket.AF_INET, address="192.168.1.5", netmask="255.255.255.0"),
                SimpleNamespace(family=socket.AF_INET6, address="2001:4860::5", netmask="ffff:ffff::"),
                SimpleNamespace(family=socket.AF_INET6, address="fe80::5%eth0", netmask="ffff:ffff::"),
            ],
        },
    )
    result = network.snapshot()
    assert result["ipv4"] == ["192.168.1.5"]
    assert result["ipv6"] == ["2001:4860::5", "fe80::5%eth0"]
    assert result["interfaces"][0]["ipv4"][0]["scope"] == "private"
    assert result["interfaces"][0]["ipv6"][0]["scope"] == "global"
    assert result["interfaces"][0]["ipv6"][1]["scope"] == "link_local"
