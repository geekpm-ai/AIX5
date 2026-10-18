"""Keep native ONVIF rule sensors named and removed with their AIX5 rules."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .catalog import CatalogClient
from .const import CONF_ONVIF_ENTRY_ID, DOMAIN, POLL_SECONDS
from .model import MigrationConflict, managed_uid, plan_migrations

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


class RuleSync:
    """Metadata-only companion; native ONVIF owns sensor creation and state."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.source_id = entry.data[CONF_ONVIF_ENTRY_ID]
        self.client = None
        self.catalog = None
        self.gateway_id = None
        self._lock = asyncio.Lock()
        self._task = None
        self._stopping = False
        self._unsubscribers = []
        self._failed = False
        self._missing_since = {}
        self._absent_confirmations = {}

    def source(self):
        source = self.hass.config_entries.async_get_entry(self.source_id)
        if source is None or source.domain != "onvif":
            raise ConfigEntryNotReady("The linked ONVIF integration is unavailable")
        if source.state in {
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.UNLOAD_IN_PROGRESS,
        }:
            raise ConfigEntryNotReady("The linked ONVIF integration is changing state")
        if not source.unique_id:
            raise ConfigEntryNotReady(
                "The linked ONVIF integration has no device identity"
            )
        return source

    def entries(self):
        return [
            entry
            for entry in er.async_entries_for_config_entry(
                er.async_get(self.hass), self.source_id
            )
            if entry.domain == "binary_sensor"
            and entry.platform == "onvif"
            and managed_uid(self.gateway_id, entry.unique_id)
        ]

    async def async_migrate(self, source, catalog):
        registry = er.async_get(self.hass)
        migrations = plan_migrations(self.entries(), catalog)
        if not migrations:
            return
        was_loaded = source.state is ConfigEntryState.LOADED
        if was_loaded and not await self.hass.config_entries.async_unload(
            self.source_id
        ):
            raise ConfigEntryNotReady(
                "Cannot unload ONVIF safely to migrate rule identities"
            )
        if source.state is not ConfigEntryState.NOT_LOADED:
            raise ConfigEntryNotReady(
                "ONVIF must be unloaded before migrating rule identities"
            )
        applied = []
        try:
            for item in migrations:
                registry.async_update_entity(
                    item.entity_id, new_unique_id=item.new_unique_id
                )
                applied.append(item)
        except Exception:
            for item in reversed(applied):
                registry.async_update_entity(
                    item.entity_id, new_unique_id=item.old_unique_id
                )
            raise
        finally:
            # A maintenance barrier may have unloaded the source before this
            # companion starts. Restore it after migration in that case too.
            if source.disabled_by is None:
                await self.hass.config_entries.async_setup(self.source_id)
        _LOGGER.info(
            "Migrated %d ONVIF rule identities; existing entity IDs were retained",
            len(applied),
        )

    def reconcile(self):
        if self.catalog is None:
            return
        registry = er.async_get(self.hass)
        absent = {}
        for entry in self.entries():
            rule = self.catalog.get(entry.unique_id)
            if rule is None:
                # A rule can be created while the multi-camera catalog is
                # being fetched. Require two complete observations before
                # removing an entity so a newly-created sensor is not lost.
                confirmations = self._absent_confirmations.get(entry.unique_id, 0) + 1
                if confirmations >= 2:
                    registry.async_remove(entry.entity_id)
                else:
                    absent[entry.unique_id] = confirmations
            elif entry.name != rule.name:
                registry.async_update_entity(entry.entity_id, name=rule.name)
        self._absent_confirmations = absent

    async def refresh(self, initial=False):
        async with self._lock:
            source = self.source()
            # Initial setup may migrate an intentionally disabled native entry
            # during the installer maintenance barrier. Never enable it here;
            # later user-disabled periods pause all catalog network polling.
            if source.disabled_by is not None and not initial:
                return
            self.gateway_id = source.unique_id
            credentials = (
                source.data["host"],
                int(source.data["port"]),
                source.data["username"],
                source.data["password"],
            )
            try:
                if self.client is None or self.client.credentials != credentials:
                    if self.client:
                        await self.client.close()
                    self.client = CatalogClient(source.data)
                async with asyncio.timeout(20):
                    catalog = await self.client.fetch(self.gateway_id)
                migration = self.hass.async_create_task(
                    self.async_migrate(source, catalog)
                )
                try:
                    await asyncio.shield(migration)
                except asyncio.CancelledError:
                    # Finish restoring native ONVIF after an identity migration
                    # even if this companion is unloaded midway through it.
                    await migration
                    raise
            except MigrationConflict as exc:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"migration_{self.source_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="migration_conflict",
                    translation_placeholders={"entities": str(exc)},
                )
                raise ConfigEntryNotReady(str(exc)) from exc
            except Exception:
                # Drop the old client so a service address or firmware change
                # is rediscovered. Keep the last catalog and every entity.
                if self.client:
                    await self.client.close()
                    self.client = None
                self._absent_confirmations.clear()
                raise
            ir.async_delete_issue(self.hass, DOMAIN, f"migration_{self.source_id}")
            self.catalog = catalog
            self.reconcile()
            if self._failed:
                _LOGGER.info(
                    "Rule catalog synchronization recovered for %s", source.title
                )
                self._failed = False
            # If a previously deleted rule UUID is restored, native ONVIF's
            # in-memory UID set suppresses recreation. A scoped reload clears
            # that cache. Wait for ordinary new entities to arrive first.
            present = {entry.unique_id for entry in self.entries()}
            now = self.hass.loop.time()
            self._missing_since = {
                uid: self._missing_since.get(uid, now)
                for uid in catalog
                if uid not in present
            }
            if (
                not initial
                and source.state is ConfigEntryState.LOADED
                and any(now - since > 15 for since in self._missing_since.values())
            ):
                self._missing_since.clear()
                await self.hass.config_entries.async_reload(self.source_id)

    @callback
    def schedule(self, _event=None):
        if self._stopping or (self._task and not self._task.done()):
            return
        self._task = self.hass.async_create_task(self.background_refresh())

    async def background_refresh(self):
        try:
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep polling after client errors
            if not self._failed:
                _LOGGER.warning(
                    "Rule catalog synchronization paused; existing entities retained: %s",
                    type(exc).__name__,
                )
                self._failed = True

    @callback
    def registry_created(self, event):
        if event.data.get("action") != "create" or self.catalog is None:
            return
        entry = er.async_get(self.hass).async_get(event.data["entity_id"])
        if (
            entry is None
            or entry.config_entry_id != self.source_id
            or entry.domain != "binary_sensor"
        ):
            return
        # New native sensor creation must be checked against a fresh catalog;
        # using the cached catalog here could delete a just-created rule.
        self.schedule()

    def start(self):
        self._unsubscribers.append(
            async_track_time_interval(
                self.hass, self.schedule, timedelta(seconds=POLL_SECONDS)
            )
        )
        self._unsubscribers.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self.registry_created
            )
        )

    async def stop(self):
        self._stopping = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.client:
            await self.client.close()


async def async_setup_entry(hass, entry):
    sync = RuleSync(hass, entry)
    try:
        await sync.refresh(initial=True)
    except Exception as exc:
        await sync.stop()
        raise ConfigEntryNotReady(str(exc)) from exc
    entry.runtime_data = sync
    sync.start()
    return True


async def async_unload_entry(hass, entry):
    await entry.runtime_data.stop()
    return True
