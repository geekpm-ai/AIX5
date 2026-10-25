"""Link metadata synchronization to an existing native ONVIF integration."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr

from .const import CONF_ONVIF_ENTRY_ID, CONF_ONVIF_UNIQUE_ID, DOMAIN, MODEL


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def candidates(self):
        registry = dr.async_get(self.hass)
        return {
            entry.entry_id: entry.title
            for entry in self.hass.config_entries.async_entries("onvif")
            if (
                MODEL.lower() in entry.title.lower()
                or any(
                    device.model == MODEL
                    for device in dr.async_entries_for_config_entry(
                        registry, entry.entry_id
                    )
                )
            )
        }

    async def async_step_user(self, user_input=None):
        candidates = self.candidates()
        if not candidates:
            return self.async_abort(reason="no_onvif_gateway")
        if user_input is not None:
            source_id = user_input[CONF_ONVIF_ENTRY_ID]
            if source_id in candidates:
                source = self.hass.config_entries.async_get_entry(source_id)
                await self.async_set_unique_id(source_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{candidates[source_id]} 规则同步",
                    data={
                        CONF_ONVIF_ENTRY_ID: source_id,
                        CONF_ONVIF_UNIQUE_ID: source.unique_id,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ONVIF_ENTRY_ID): vol.In(candidates)}
            ),
        )

    async def async_step_import(self, user_input):
        """Allow the installer to link the already-configured ONVIF entry once."""
        return await self.async_step_user(user_input)
