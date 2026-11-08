"""Network address discovery for the settings page and DDNS worker."""

from __future__ import annotations

import ipaddress

import psutil


def _address_scope(value: str) -> str:
    """Return a stable, user-facing scope label for an interface address."""

    address = ipaddress.ip_address(value.split("%", 1)[0])
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_private:
        return "private"
    return "global"


def snapshot() -> dict:
    """Return active interface addresses without exposing MAC addresses.

    Link-local IPv6 addresses retain their zone suffix (for example
    ``%eth0``), which is required when a browser or an operator uses that
    address on a local network. Loopback interfaces are omitted from the
    displayed list, while tunnel interfaces remain visible when they have an
    address because they can be the only route to an IPv6 network.
    """

    stats = psutil.net_if_stats()
    interfaces: list[dict] = []
    ipv4: list[str] = []
    ipv6: list[str] = []
    for name, entries in psutil.net_if_addrs().items():
        interface = {"name": name, "is_up": bool(stats.get(name) and stats[name].isup), "ipv4": [], "ipv6": []}
        for entry in entries:
            family = entry.family
            if family == psutil.AF_LINK:
                continue
            value = str(entry.address)
            raw = value.split("%", 1)[0]
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if address.is_loopback:
                continue
            item = {
                "address": value,
                "scope": _address_scope(value),
                "netmask": entry.netmask,
            }
            if address.version == 4:
                interface["ipv4"].append(item)
                ipv4.append(value)
            else:
                interface["ipv6"].append(item)
                ipv6.append(value)
        if interface["ipv4"] or interface["ipv6"]:
            interfaces.append(interface)
    interfaces.sort(key=lambda item: (not item["is_up"], item["name"]))
    return {
        "interfaces": interfaces,
        "ipv4": ipv4,
        "ipv6": ipv6,
        "updated_at": __import__("time").time(),
    }
