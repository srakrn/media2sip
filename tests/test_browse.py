"""Browsing.

This is what puts the entity in the Media panel's player picker: that picker
filters on MediaPlayerEntityFeature.BROWSE_MEDIA and nothing else, so an entity
without it is reachable only from services and automations.
"""

from __future__ import annotations

import pytest
from homeassistant.components.media_player import MediaClass, MediaPlayerEntityFeature
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from custom_components.media2sip.media_player import SOUNDS_ROOT

ENTITY = "media_player.working_zone"


def _entity(hass: HomeAssistant, config_entry):
    return config_entry.runtime_data.entities[ENTITY]


async def test_browse_media_is_advertised(hass: HomeAssistant, setup_integration) -> None:
    features = hass.states.get(ENTITY).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & MediaPlayerEntityFeature.BROWSE_MEDIA


async def test_root_lists_the_sidecar_sounds(
    hass: HomeAssistant, setup_integration
) -> None:
    root = await _entity(hass, setup_integration).async_browse_media()
    assert root.can_expand
    assert not root.can_play
    titles = [child.title for child in root.children]
    assert "Sidecar sounds" in titles


async def test_sounds_folder_lists_every_sound(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    folder = await _entity(hass, setup_integration).async_browse_media(
        media_content_id=SOUNDS_ROOT
    )
    assert [child.title for child in folder.children] == sidecar.sounds
    for child in folder.children:
        assert child.can_play
        assert not child.can_expand
        assert child.media_content_id.startswith("sound:")


async def test_a_sound_is_not_a_folder(hass: HomeAssistant, setup_integration) -> None:
    with pytest.raises(BrowseError):
        await _entity(hass, setup_integration).async_browse_media(
            media_content_id="sound:chime"
        )


async def test_browsed_sound_plays_without_a_fetch(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """A static clip goes straight to the sidecar's cache, no URL involved."""
    entity = _entity(hass, setup_integration)
    folder = await entity.async_browse_media(media_content_id=SOUNDS_ROOT)
    chosen = folder.children[0]

    await entity.async_play_media(chosen.media_content_type, chosen.media_content_id)
    await hass.async_block_till_done()

    assert sidecar.calls[0]["media"] == chosen.media_content_id


async def test_root_survives_an_empty_media_library(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """No media_source content must not make the entity unbrowsable."""
    sidecar.sounds = []
    root = await _entity(hass, setup_integration).async_browse_media()
    assert root.media_class is MediaClass.DIRECTORY
