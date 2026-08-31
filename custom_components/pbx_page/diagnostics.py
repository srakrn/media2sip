"""Diagnostics: enough to tell whether paging works, with no credentials in it."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import PbxPageConfigEntry
from .const import DOMAIN
from .client import SidecarError

TO_REDACT = {CONF_TOKEN, "password", "secret", "authorization"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PbxPageConfigEntry
) -> dict[str, Any]:
    client = entry.runtime_data.client

    health: Any
    try:
        health = await client.async_health()
    except SidecarError as err:
        health = {"error": str(err)}

    history: Any
    try:
        history = await client.async_history()
    except SidecarError as err:
        history = {"error": str(err)}

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "connection": {
            "connected": client.connected,
            "registered": client.registered,
            "available": client.available,
        },
        # Both halves, so a mismatch is obvious in a bug report rather than
        # something the reader has to think to ask about.
        "versions": {
            "integration": str(
                (await async_get_integration(hass, DOMAIN)).version or "unknown"
            ),
            "sidecar": client.sidecar_version,
        },
        # Registration state and recent calls with target, SIP reason code and
        # latency - the three things that explain a failed page. The sidecar
        # labels media by content hash rather than URL, so no TTS token reaches
        # a diagnostics download that gets shared around.
        "accounts": async_redact_data(client.accounts, TO_REDACT),
        "health": async_redact_data(health, TO_REDACT),
        "recent_calls": async_redact_data(history, TO_REDACT),
        "entities": [
            {
                "entity_id": entity_id,
                "state": str(entity.state),
                "available": entity.available,
                **entity.extra_state_attributes,
            }
            for entity_id, entity in entry.runtime_data.entities.items()
        ],
    }
