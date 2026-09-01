"""The PBX Page integration.

Owns the sidecar client's lifecycle and the domain-level `pbx_page.page` service.
There is no SIP here and no compiled dependency; everything below the control API
lives in the sidecar, which is what makes this side maintainable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration

from .client import PbxPageClient, SidecarError
from .const import (
    ATTR_CHIME,
    ATTR_PRIORITY,
    ATTR_SOUND,
    ATTR_TARGETS,
    ATTR_TEXT,
    DEV_VERSION,
    DOMAIN,
    PRIORITY_NORMAL,
    PRIORITY_URGENT,
    SERVICE_PAGE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]


@dataclass
class PbxPageData:
    """Runtime state shared by the entities of one config entry."""

    client: PbxPageClient
    # Optional lock across entities that share physical handsets. Explicit,
    # because inferring which targets overlap is guesswork the user can just tell us.
    global_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Our own entities, so the page service can find them without reaching into
    # Home Assistant internals. Entities add and remove themselves.
    entities: dict[str, Any] = field(default_factory=dict)


type PbxPageConfigEntry = ConfigEntry[PbxPageData]

SERVICE_PAGE_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Optional(ATTR_TEXT): cv.string,
            vol.Optional(ATTR_SOUND): cv.string,
            vol.Required(ATTR_TARGETS): cv.entity_ids,
            vol.Optional(ATTR_CHIME): cv.string,
            vol.Optional(ATTR_PRIORITY, default=PRIORITY_NORMAL): vol.In(
                [PRIORITY_NORMAL, PRIORITY_URGENT]
            ),
        },
        cv.has_at_least_one_key(ATTR_TEXT, ATTR_SOUND),
    )
)


async def async_setup_entry(hass: HomeAssistant, entry: PbxPageConfigEntry) -> bool:
    """Set up a sidecar from a config entry."""
    client = PbxPageClient(
        session=async_get_clientsession(hass),
        url=entry.data[CONF_URL],
        token=entry.data.get(CONF_TOKEN),
    )

    try:
        health = await client.async_health()
    except SidecarError as err:
        # Not ready rather than failed: a sidecar that is merely restarting should
        # not need the user to reload anything.
        raise ConfigEntryNotReady(f"sidecar at {entry.data[CONF_URL]} is unreachable: {err}") from err

    await _async_warn_on_version_mismatch(hass, health.get("version"))

    await client.async_start()
    entry.runtime_data = PbxPageData(client=client)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PbxPageConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.async_stop()
    return unloaded


async def _async_warn_on_version_mismatch(
    hass: HomeAssistant, sidecar_version: str | None
) -> None:
    """The two halves are released as a pair and meant to be run as one.

    Not an error: a mismatch usually just means half an upgrade, and refusing to
    start would take paging down over something that probably still works. But it
    must be visible, because the alternative is debugging a behaviour change with
    no idea the versions drifted.
    """
    integration = await async_get_integration(hass, DOMAIN)
    ours = str(integration.version) if integration.version else None
    if not ours or not sidecar_version or ours == sidecar_version:
        return
    if sidecar_version == DEV_VERSION:
        # A sidecar built from a working tree rather than a release. Its operator
        # already knows it is unversioned; warning on every start would train them
        # to ignore the message that matters.
        _LOGGER.debug("sidecar reports an unreleased build; not comparing versions")
        return
    _LOGGER.warning(
        "version mismatch: integration %s is talking to sidecar %s. They are "
        "released as a pair; update whichever is behind",
        ours, sidecar_version,
    )


async def _async_reload_entry(hass: HomeAssistant, entry: PbxPageConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register `pbx_page.page`, for automations that do not want media player semantics."""
    if hass.services.has_service(DOMAIN, SERVICE_PAGE):
        return

    async def async_page(call: ServiceCall) -> None:
        from .media_player import PbxPageMediaPlayer  # local import avoids a cycle

        targets: list[str] = call.data[ATTR_TARGETS]
        urgent = call.data[ATTR_PRIORITY] == PRIORITY_URGENT

        entities: list[PbxPageMediaPlayer] = []
        for entity_id in targets:
            entity = _find_entity(hass, entity_id)
            if entity is None:
                raise HomeAssistantError(f"{entity_id} is not a pbx_page media player")
            entities.append(entity)

        results = await asyncio.gather(
            *(
                entity.async_page(
                    text=call.data.get(ATTR_TEXT),
                    sound=call.data.get(ATTR_SOUND),
                    chime=call.data.get(ATTR_CHIME),
                    urgent=urgent,
                )
                for entity in entities
            ),
            return_exceptions=True,
        )
        # One dead target must not hide the others having worked, but it must not
        # be swallowed either.
        failures = [
            f"{entity.entity_id}: {result}"
            for entity, result in zip(entities, results, strict=True)
            if isinstance(result, Exception)
        ]
        if failures:
            raise HomeAssistantError("; ".join(failures))

    hass.services.async_register(DOMAIN, SERVICE_PAGE, async_page, schema=SERVICE_PAGE_SCHEMA)


def _find_entity(hass: HomeAssistant, entity_id: str):
    """Find one of our entities by entity_id, across every loaded config entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = getattr(entry, "runtime_data", None)
        if data is not None and entity_id in data.entities:
            return data.entities[entity_id]
    return None
