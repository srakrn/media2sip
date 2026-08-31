"""Config and options flow.

The sidecar endpoint is validated against `/health` before the entry is created,
so a typo shows up here rather than as an entity that is permanently unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import PbxPageClient, SidecarError
from .const import (
    CONF_CHIME,
    CONF_EXTENSION,
    CONF_GLOBAL_LOCK,
    CONF_LEAD_IN,
    CONF_NAME,
    CONF_POLICY,
    CONF_TARGETS,
    DEFAULT_POLICY,
    DOMAIN,
    POLICIES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_URL, default="http://localhost:8080"): str,
        vol.Optional(CONF_TOKEN): str,
    }
)


class PbxPageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sidecar endpoint, then paging targets."""

    VERSION = 1

    def __init__(self) -> None:
        self._url: str = ""
        self._token: str | None = None
        self._targets: list[dict[str, Any]] = []
        self._sounds: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            client = PbxPageClient(
                async_get_clientsession(self.hass), url, user_input.get(CONF_TOKEN)
            )
            try:
                health = await client.async_health()
            except SidecarError as err:
                _LOGGER.debug("sidecar validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                self._url = url
                self._token = user_input.get(CONF_TOKEN)
                self._sounds = health.get("sounds", [])
                if not health.get("accounts"):
                    errors["base"] = "no_accounts"
                else:
                    return await self.async_step_target()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER, errors=errors
        )

    async def async_step_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add paging targets, one at a time."""
        errors: dict[str, str] = {}
        if user_input is not None:
            extension = str(user_input[CONF_EXTENSION]).strip()
            if any(t[CONF_EXTENSION] == extension for t in self._targets):
                errors[CONF_EXTENSION] = "duplicate_target"
            else:
                self._targets.append(
                    {CONF_NAME: user_input[CONF_NAME].strip(), CONF_EXTENSION: extension}
                )
                if not user_input.get("add_another"):
                    return self.async_create_entry(
                        title=f"PBX Page ({self._url})",
                        data={
                            CONF_URL: self._url,
                            CONF_TOKEN: self._token,
                            CONF_TARGETS: self._targets,
                        },
                    )

        return self.async_show_form(
            step_id="target",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_EXTENSION): str,
                    vol.Optional("add_another", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={"count": str(len(self._targets))},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PbxPageOptionsFlow:
        return PbxPageOptionsFlow()


class PbxPageOptionsFlow(OptionsFlow):
    """Lead-in, default chime, concurrency policy."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # An empty chime means "no chime", which vol cannot express as a default.
            if not user_input.get(CONF_CHIME):
                user_input.pop(CONF_CHIME, None)
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        sounds = list(self.config_entry.runtime_data.client.sounds) if (
            hasattr(self.config_entry, "runtime_data") and self.config_entry.runtime_data
        ) else []

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_LEAD_IN, default=options.get(CONF_LEAD_IN, 1.0)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=5, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                    )
                ),
                vol.Optional(CONF_CHIME, default=options.get(CONF_CHIME, "")): SelectSelector(
                    SelectSelectorConfig(
                        options=["", *sounds], mode=SelectSelectorMode.DROPDOWN, custom_value=True
                    )
                ),
                vol.Optional(
                    CONF_POLICY, default=options.get(CONF_POLICY, DEFAULT_POLICY)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=POLICIES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="policy",
                    )
                ),
                vol.Optional(
                    CONF_GLOBAL_LOCK, default=options.get(CONF_GLOBAL_LOCK, False)
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
