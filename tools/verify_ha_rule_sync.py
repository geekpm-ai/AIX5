"""Verify the companion against installed HA APIs in an isolated temp instance.

Run inside the HA container, using a staged checkout outside /config:
    python verify_ha_rule_sync.py --package-root /tmp/edge-ai-qa/home_assistant

No connection is made to a gateway and no running HA instance is modified.
"""

import argparse
import asyncio
import importlib.metadata
import json
import sys
import tempfile
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.package_root)
    asyncio.run(verify())


async def verify():
    from custom_components.edge_ai_onvif import RuleSync
    from custom_components.edge_ai_onvif.model import RuleMetadata
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    calls = []
    source = SimpleNamespace(
        entry_id="isolated-native",
        unique_id="isolated-gateway",
        domain="onvif",
        disabled_by=None,
        state=ConfigEntryState.LOADED,
        title="Edge-AI-IPC",
        data={
            "host": "127.0.0.1",
            "port": 18080,
            "username": "qa",
            "password": "unused",
        },
        pref_disable_new_entities=False,
        subentries={},
    )
    companion = SimpleNamespace(data={"onvif_entry_id": source.entry_id})

    class Entries:
        def async_get_entry(self, entry_id):
            return source if entry_id == source.entry_id else None

        async def async_unload(self, entry_id):
            assert entry_id == source.entry_id
            calls.append("unload")
            source.state = ConfigEntryState.NOT_LOADED
            return True

        async def async_setup(self, entry_id):
            assert entry_id == source.entry_id
            calls.append("setup")
            source.state = ConfigEntryState.LOADED
            return True

    with tempfile.TemporaryDirectory(prefix="edge-ai-ha-api-qa-") as directory:
        hass = HomeAssistant(directory)
        hass.config_entries = Entries()
        dr.async_setup(hass)
        await dr.async_load(hass, load_empty=True)
        registry = er.async_get(hass)
        await registry.async_load(load_empty=True)
        sync = RuleSync(hass, companion)
        sync.gateway_id = source.unique_id
        rule = RuleMetadata(
            "cam", "rule", "客厅有人", "客厅", True, True, ("person", "cat")
        )
        uid = rule.native_uid(source.unique_id)
        old = registry.async_get_or_create(
            "binary_sensor",
            "onvif",
            uid + ":person",
            config_entry=source,
            suggested_object_id="preserve_automation",
            original_name="Edge-AI-IPC Field Detection",
        )
        unrelated = registry.async_get_or_create(
            "binary_sensor",
            "onvif",
            "unrelated-native-sensor",
            config_entry=source,
            suggested_object_id="unrelated",
        )
        sync.catalog = {uid: rule}
        await sync.async_migrate(source, sync.catalog)
        assert calls == ["unload", "setup"]
        assert registry.async_get(old.entity_id).unique_id == uid
        sync.reconcile()
        assert registry.async_get(old.entity_id).name == "客厅有人"
        renamed = RuleMetadata(
            "cam", "rule", "改名后仍保留自动化", "客厅", True, True, ("person", "cat")
        )
        sync.catalog = {uid: renamed}
        sync.reconcile()
        assert registry.async_get(old.entity_id).name == renamed.name
        assert (
            registry.async_get_entity_id("binary_sensor", "onvif", uid) == old.entity_id
        )
        sync.catalog = {}
        sync.reconcile()
        assert registry.async_get(old.entity_id) is not None
        sync.reconcile()
        assert registry.async_get(old.entity_id) is None
        assert registry.async_get(unrelated.entity_id) is not None
        disabled_rule = RuleMetadata(
            "cam", "rule-disabled", "维护窗口", "客厅", True, True, ("person",)
        )
        disabled_uid = disabled_rule.native_uid(source.unique_id)
        disabled_old = registry.async_get_or_create(
            "binary_sensor",
            "onvif",
            disabled_uid + ":person",
            config_entry=source,
            suggested_object_id="preserve_disabled_migration",
        )
        source.disabled_by = "user"
        source.state = ConfigEntryState.NOT_LOADED
        call_count = len(calls)
        await sync.async_migrate(source, {disabled_uid: disabled_rule})
        assert len(calls) == call_count
        assert source.state is ConfigEntryState.NOT_LOADED
        assert registry.async_get(disabled_old.entity_id).unique_id == disabled_uid
        await sync.refresh()
        assert sync.client is None

        class FailedClient:
            credentials = ("127.0.0.1", 18080, "qa", "unused")

            async def fetch(self, _gateway):
                raise OSError("isolated simulated network interruption")

            async def close(self):
                pass

        source.disabled_by = None
        source.state = ConfigEntryState.LOADED
        sync.client = FailedClient()
        sync._absent_confirmations = {disabled_uid: 1}
        try:
            await sync.refresh()
            raise AssertionError("Expected a catalog network failure")
        except OSError:
            pass
        assert registry.async_get(disabled_old.entity_id) is not None
        assert not sync._absent_confirmations
        assert sync.client is None
        await hass.async_block_till_done()
        await hass.async_stop(force=True)
    print(
        json.dumps(
            {
                "homeassistant": importlib.metadata.version("homeassistant"),
                "isolated": True,
                "passed": True,
                "checks": [
                    "public registry APIs",
                    "legacy identity migration retains entity_id",
                    "unload before migration and setup afterwards",
                    "rename retains automation identity",
                    "two catalog confirmations before deletion",
                    "unrelated sensors preserved",
                    "disabled migration never enables native ONVIF",
                    "disabled source pauses network polling",
                    "catalog failure retains existing entities",
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
