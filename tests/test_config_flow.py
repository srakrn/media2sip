"""Config and options flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.media2sip.client import SidecarError
from custom_components.media2sip.const import (
    CONF_CHIME,
    CONF_EXTENSION,
    CONF_LEAD_IN,
    CONF_NAME,
    CONF_POLICY,
    CONF_TARGET_ID,
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
    targets = result["data"][CONF_TARGETS]
    assert [(t[CONF_NAME], t[CONF_EXTENSION]) for t in targets] == [
        ("Working Zone", "991"),
        ("Warehouse", "992"),
    ]
    # Each target carries its own opaque id, which is what lets its extension be
    # edited later without the entity being recreated.
    ids = [t[CONF_TARGET_ID] for t in targets]
    assert all(ids) and len(set(ids)) == 2


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
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"

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
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LEAD_IN: 1.0, CONF_CHIME: ""}
    )
    assert CONF_CHIME not in setup_integration.options


# -- managing targets after setup ----------------------------------------


async def _options_step(hass: HomeAssistant, entry, step: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_add_target_to_an_existing_entry(
    hass: HomeAssistant, setup_integration
) -> None:
    """A new target becomes a new entity, without touching the existing ones."""
    before = {t[CONF_TARGET_ID] for t in setup_integration.data[CONF_TARGETS]}

    result = await _options_step(hass, setup_integration, "add_target")
    assert result["step_id"] == "add_target"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "Loading Bay", CONF_EXTENSION: "993"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    targets = setup_integration.data[CONF_TARGETS]
    assert [t[CONF_EXTENSION] for t in targets] == ["991", "992", "993"]
    # The targets that were already there keep their ids, so their entities are
    # untouched by the addition.
    assert before < {t[CONF_TARGET_ID] for t in targets}
    assert hass.states.get("media_player.loading_bay") is not None


async def test_add_target_rejects_a_duplicate_extension(
    hass: HomeAssistant, setup_integration
) -> None:
    result = await _options_step(hass, setup_integration, "add_target")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "Also 991", CONF_EXTENSION: "991"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EXTENSION: "duplicate_target"}


async def test_edit_target_changes_the_extension_in_place(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """The entity survives the edit: same entity id, same unique id, new extension.

    This is the whole reason targets carry an id. Keying on the extension would
    make this an entity replacement, silently orphaning history and area.
    """
    entity_registry = er.async_get(hass)
    before = entity_registry.async_get("media_player.working_zone")
    assert before is not None

    result = await _options_step(hass, setup_integration, "pick_target")
    assert result["step_id"] == "pick_target"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TARGET_ID: "aaaa1111"}
    )
    assert result["step_id"] == "edit_target"
    # The form arrives filled in with what the target dials today.
    assert result["description_placeholders"] == {"extension": "991"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "Working Zone", CONF_EXTENSION: "995"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    after = entity_registry.async_get("media_player.working_zone")
    assert after is not None
    assert after.unique_id == before.unique_id

    # And the new extension is what actually gets dialled.
    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": "media_player.working_zone",
            "media_content_type": "music",
            "media_content_id": "sound:chime",
        },
        blocking=True,
    )
    assert sidecar.calls[-1]["target"] == "995"


async def test_edit_target_can_rename_without_changing_the_extension(
    hass: HomeAssistant, setup_integration
) -> None:
    result = await _options_step(hass, setup_integration, "pick_target")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TARGET_ID: "bbbb2222"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "Back Warehouse", CONF_EXTENSION: "992"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    target = next(
        t for t in setup_integration.data[CONF_TARGETS] if t[CONF_TARGET_ID] == "bbbb2222"
    )
    assert target[CONF_NAME] == "Back Warehouse"
    assert target[CONF_EXTENSION] == "992"


async def test_edit_target_rejects_another_targets_extension(
    hass: HomeAssistant, setup_integration
) -> None:
    """Its own extension is fine to keep; someone else's is not."""
    result = await _options_step(hass, setup_integration, "pick_target")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TARGET_ID: "aaaa1111"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "Working Zone", CONF_EXTENSION: "992"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EXTENSION: "duplicate_target"}


async def test_editing_targets_leaves_the_options_alone(
    hass: HomeAssistant, setup_integration
) -> None:
    """Targets live in `data`; the options must come through untouched."""
    hass.config_entries.async_update_entry(
        setup_integration, options={CONF_LEAD_IN: 2.5, CONF_POLICY: POLICY_PREEMPT}
    )
    await hass.async_block_till_done()

    result = await _options_step(hass, setup_integration, "add_target")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "Loading Bay", CONF_EXTENSION: "993"}
    )
    await hass.async_block_till_done()

    assert setup_integration.options[CONF_LEAD_IN] == 2.5
    assert setup_integration.options[CONF_POLICY] == POLICY_PREEMPT
