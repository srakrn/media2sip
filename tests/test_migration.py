"""Config entry migration.

Version 1 keyed the entity on the extension, so an extension could never be
edited. Version 2 keys on an opaque target id. The migration exists to make that
change without recreating anyone's entities, so what these tests actually check
is that the identity carries across.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_TOKEN, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.media2sip.const import (
    CONF_EXTENSION,
    CONF_NAME,
    CONF_TARGET_ID,
    CONF_TARGETS,
    DOMAIN,
)


@pytest.fixture
def v1_entry() -> MockConfigEntry:
    """An entry as version 1 wrote it: targets with no id."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Media2SIP (test)",
        version=1,
        unique_id="http://sidecar.test:8080",
        data={
            CONF_URL: "http://sidecar.test:8080",
            CONF_TOKEN: None,
            CONF_TARGETS: [
                {CONF_NAME: "Working Zone", CONF_EXTENSION: "991"},
                {CONF_NAME: "Warehouse", CONF_EXTENSION: "992"},
            ],
        },
        options={},
    )


async def test_migration_assigns_ids_and_keeps_the_entity(
    hass: HomeAssistant, v1_entry: MockConfigEntry, sidecar
) -> None:
    """The pre-existing entity keeps its entity id, and follows its new unique id."""
    v1_entry.add_to_hass(hass)

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    # What version 1 would have registered: unique id built from the extension.
    old_unique_id = f"{v1_entry.entry_id}_991"
    existing = entities.async_get_or_create(
        Platform.MEDIA_PLAYER,
        DOMAIN,
        old_unique_id,
        config_entry=v1_entry,
        suggested_object_id="working_zone",
    )
    device = devices.async_get_or_create(
        config_entry_id=v1_entry.entry_id, identifiers={(DOMAIN, old_unique_id)}
    )

    with patch("custom_components.media2sip.PbxPageClient", return_value=sidecar):
        assert await hass.config_entries.async_setup(v1_entry.entry_id)
        await hass.async_block_till_done()

    assert v1_entry.version == 2

    targets = v1_entry.data[CONF_TARGETS]
    ids = [t[CONF_TARGET_ID] for t in targets]
    assert all(ids) and len(set(ids)) == 2
    # Nothing else about the targets moved.
    assert [(t[CONF_NAME], t[CONF_EXTENSION]) for t in targets] == [
        ("Working Zone", "991"),
        ("Warehouse", "992"),
    ]

    # The entity is the same one, under its new unique id - not a second entity
    # alongside an orphan.
    migrated = entities.async_get(existing.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{v1_entry.entry_id}_{ids[0]}"
    assert entities.async_get_entity_id(Platform.MEDIA_PLAYER, DOMAIN, old_unique_id) is None

    # The device follows too, so an area assignment survives.
    assert devices.async_get_device(identifiers={(DOMAIN, old_unique_id)}) is None
    assert devices.async_get(device.id).identifiers == {
        (DOMAIN, f"{v1_entry.entry_id}_{ids[0]}")
    }


async def test_migration_is_idempotent(
    hass: HomeAssistant, v1_entry: MockConfigEntry, sidecar
) -> None:
    """Targets that already carry an id are left exactly as they are."""
    v1_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        v1_entry,
        data={
            **v1_entry.data,
            CONF_TARGETS: [
                {CONF_TARGET_ID: "keepme01", CONF_NAME: "Working Zone", CONF_EXTENSION: "991"},
            ],
        },
    )

    with patch("custom_components.media2sip.PbxPageClient", return_value=sidecar):
        assert await hass.config_entries.async_setup(v1_entry.entry_id)
        await hass.async_block_till_done()

    assert v1_entry.version == 2
    assert v1_entry.data[CONF_TARGETS][0][CONF_TARGET_ID] == "keepme01"


async def test_a_newer_entry_is_refused_rather_than_downgraded(
    hass: HomeAssistant, v1_entry: MockConfigEntry, sidecar
) -> None:
    """An entry written by a future version must not be silently rewritten."""
    v1_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(v1_entry, version=3)

    with patch("custom_components.media2sip.PbxPageClient", return_value=sidecar):
        assert not await hass.config_entries.async_setup(v1_entry.entry_id)
        await hass.async_block_till_done()

    assert v1_entry.data[CONF_TARGETS][0].get(CONF_TARGET_ID) is None
