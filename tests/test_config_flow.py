"""Config and options flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.media2sip.client import SidecarError
from custom_components.media2sip.const import (
    CONF_CHIME,
    CONF_EXTENSION,
    CONF_LEAD_IN,
    CONF_NAME,
    CONF_POLICY,
    CONF_TARGETS,
    DOMAIN,
    POLICY_PREEMPT,
)

HEALTH_OK = {
    "status": "ok",
    "version": "test",
    "accounts": [{"account_id": "9901", "uri": "sip:9901@pbx", "registered": True,
                  "code": 200, "reason": "OK", "since": 0.0}],
    "active_calls": [],
    "sounds": ["chime"],
    "lead_in": 1.0,
    "codecs": ["PCMU/8000/1"],
}


async def test_full_flow(hass: HomeAssistant) -> None:
    """Endpoint, then targets, then an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.media2sip.config_flow.PbxPageClient.async_health",
        return_value=HEALTH_OK,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://sidecar.test:8080/"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "target"

    # Add one, ask for another, then finish.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Working Zone", CONF_EXTENSION: "991", "add_another": True},
    )
    assert result["step_id"] == "target"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Warehouse", CONF_EXTENSION: "992", "add_another": False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The trailing slash is normalised away, so the unique id is stable.
    assert result["data"][CONF_URL] == "http://sidecar.test:8080"
    assert result["data"][CONF_TARGETS] == [
        {CONF_NAME: "Working Zone", CONF_EXTENSION: "991"},
        {CONF_NAME: "Warehouse", CONF_EXTENSION: "992"},
    ]


async def test_unreachable_sidecar_is_caught_in_the_flow(hass: HomeAssistant) -> None:
    """A typo shows up here, not as an entity that is permanently unavailable."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.media2sip.config_flow.PbxPageClient.async_health",
        side_effect=SidecarError("connection refused"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://nope.test:8080"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_sidecar_with_no_accounts_is_rejected(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.media2sip.config_flow.PbxPageClient.async_health",
        return_value={**HEALTH_OK, "accounts": []},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://sidecar.test:8080"}
        )
    assert result["errors"] == {"base": "no_accounts"}


async def test_duplicate_extension_rejected(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.media2sip.config_flow.PbxPageClient.async_health",
        return_value=HEALTH_OK,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://sidecar.test:8080"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Working Zone", CONF_EXTENSION: "991", "add_another": True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Duplicate", CONF_EXTENSION: "991", "add_another": False},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EXTENSION: "duplicate_target"}


async def test_same_sidecar_cannot_be_added_twice(
    hass: HomeAssistant, setup_integration
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.media2sip.config_flow.PbxPageClient.async_health",
        return_value=HEALTH_OK,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://sidecar.test:8080"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, setup_integration) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_LEAD_IN: 2.0, CONF_CHIME: "chime", CONF_POLICY: POLICY_PREEMPT},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options[CONF_LEAD_IN] == 2.0
    assert setup_integration.options[CONF_POLICY] == POLICY_PREEMPT


async def test_options_flow_empty_chime_means_none(
    hass: HomeAssistant, setup_integration
) -> None:
    """vol cannot express 'no chime' as a default, so an empty string is dropped."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LEAD_IN: 1.0, CONF_CHIME: ""}
    )
    assert CONF_CHIME not in setup_integration.options
