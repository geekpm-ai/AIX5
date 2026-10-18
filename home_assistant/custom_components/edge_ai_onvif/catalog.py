"""Read the complete rule catalog over authenticated standard ONVIF services."""

from __future__ import annotations

from pathlib import Path

import onvif
from onvif import ONVIFCamera
from onvif.client import ONVIFService

from .const import ANALYTICS_NS, MODEL
from .model import (
    CatalogError,
    RuleMetadata,
    field,
    parse_configuration,
    parse_rule,
    response_items,
)


class CatalogClient:
    def __init__(self, data):
        self.credentials = (
            data["host"],
            int(data["port"]),
            data["username"],
            data["password"],
        )
        self.camera = ONVIFCamera(*self.credentials, no_cache=True, adjust_time=True)
        self.media = None
        self.analytics = None
        self.device_id = None

    async def setup(self):
        await self.camera.update_xaddrs()
        device = await self.camera.create_devicemgmt_service()
        info = await device.GetDeviceInformation()
        if field(info, "Model") != MODEL:
            raise CatalogError("The selected ONVIF device is not Edge-AI-IPC")
        self.device_id = field(info, "SerialNumber")
        self.media = await self.camera.create_media_service()
        address = self.camera.xaddrs.get(ANALYTICS_NS)
        if not address:
            raise CatalogError(
                "This gateway does not expose its ONVIF rule catalog; update AIX5 first"
            )
        # The library's default Analytics helper binds AnalyticsEngineBinding,
        # while GetRules belongs to the separate standard RuleEngineBinding.
        self.analytics = ONVIFService(
            address,
            self.credentials[2],
            self.credentials[3],
            str(Path(onvif.__file__).parent / "wsdl" / "analytics.wsdl"),
            no_cache=True,
            dt_diff=self.camera.dt_diff,
            binding_name=f"{{{ANALYTICS_NS}}}RuleEngineBinding",
        )
        await self.analytics.setup()

    async def fetch(self, gateway_id: str) -> dict[str, RuleMetadata]:
        if self.media is None:
            await self.setup()
        if self.device_id != gateway_id:
            raise CatalogError("The ONVIF gateway identity has changed")
        configurations = response_items(
            await self.media.GetVideoAnalyticsConfigurations(), "Configurations"
        )
        result = {}
        seen_cameras = set()
        for configuration in configurations:
            cid = parse_configuration(configuration)
            if cid in seen_cameras:
                raise CatalogError("Duplicate camera in ONVIF rule catalog")
            seen_cameras.add(cid)
            rules = response_items(
                await self.analytics.GetRules(
                    {"ConfigurationToken": field(configuration, "token")}
                ),
                "Rule",
            )
            for rule_value in rules:
                rule = parse_rule(cid, rule_value)
                uid = rule.native_uid(gateway_id)
                if uid in result:
                    raise CatalogError("Duplicate rule in ONVIF catalog")
                result[uid] = rule
        # Returning only after every camera succeeds makes empty results
        # authoritative and failures incapable of deleting registered sensors.
        return result

    async def close(self):
        if self.analytics is not None:
            await self.analytics.close()
        await self.camera.close()
