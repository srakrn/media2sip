"""Pause, resume, and swapping the clip on a call that is already up.

Both exist because browsing can put something longer than a chime down the line.
For a two-second announcement they would be theatre; for a track someone picked
out of their media library they are what a media player is expected to do.
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_IDLE, STATE_PAUSED, STATE_PLAYING
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import settle

ENTITY = "media_player.working_zone"


def _play(hass: HomeAssistant, media_id: str = "sound:chime"):
    return hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY,
            ATTR_MEDIA_CONTENT_TYPE: "music",
            ATTR_MEDIA_CONTENT_ID: media_id,
        },
        blocking=True,
    )


async def _in_flight(hass: HomeAssistant, media_id: str = "sound:chime") -> asyncio.Task:
    task = asyncio.create_task(_play(hass, media_id))
    for _ in range(60):
        await asyncio.sleep(0)
        if hass.states.get(ENTITY).attributes.get("call_id"):
            break
    return task


async def _call(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )


# -- pause ---------------------------------------------------------------


async def test_pause_and_resume(hass: HomeAssistant, setup_integration, sidecar) -> None:
    sidecar.auto_disconnect = False
    task = await _in_flight(hass)
    sidecar.emit("playback_started", call_id="call-1", target="991")
    await settle(hass)
    assert hass.states.get(ENTITY).state == STATE_PLAYING

    await _call(hass, SERVICE_MEDIA_PAUSE)
    await settle(hass)
    assert hass.states.get(ENTITY).state == STATE_PAUSED
    assert hass.states.get(ENTITY).attributes["paused"] is True

    await _call(hass, SERVICE_MEDIA_PLAY)
    await settle(hass)
    assert hass.states.get(ENTITY).state == STATE_PLAYING

    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    await task


async def test_pause_with_nothing_playing_is_an_error(
    hass: HomeAssistant, setup_integration
) -> None:
    """Better than silently doing nothing, which reads as a broken entity."""
    with pytest.raises(HomeAssistantError, match="not playing"):
        await _call(hass, SERVICE_MEDIA_PAUSE)


async def test_a_disconnect_clears_the_paused_flag(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    sidecar.auto_disconnect = False
    task = await _in_flight(hass)
    sidecar.emit("playback_started", call_id="call-1", target="991")
    await _call(hass, SERVICE_MEDIA_PAUSE)
    await settle(hass)
    assert hass.states.get(ENTITY).state == STATE_PAUSED

    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    assert hass.states.get(ENTITY).state == STATE_IDLE
    assert hass.states.get(ENTITY).attributes["paused"] is False
    await task


# -- replace -------------------------------------------------------------


async def test_playing_something_new_replaces_it_immediately(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """The default. A media player everywhere else in Home Assistant behaves
    this way, and it needs no re-dial: the handsets are already listening."""
    sidecar.auto_disconnect = False
    task = await _in_flight(hass, "sound:chime")
    await settle(hass)

    await _play(hass, "sound:evacuate")
    await settle(hass)

    assert len(sidecar.calls) == 2
    assert sidecar.calls[1]["policy"] == "replace"
    assert sidecar.calls[1]["media"] == "sound:evacuate"
    # Same call, so the handsets never had to answer again.
    assert not sidecar.hangups
    assert hass.states.get(ENTITY).attributes["call_id"] == "call-1"
    assert hass.states.get(ENTITY).attributes["queued"] == 0

    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    task.cancel()


async def test_replace_does_not_queue(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """Three in a row leave one call and the last clip, not a backlog."""
    sidecar.auto_disconnect = False
    task = await _in_flight(hass, "sound:chime")
    for name in ("a", "b", "c"):
        await _play(hass, f"sound:{name}")
    await settle(hass)

    assert hass.states.get(ENTITY).attributes["queued"] == 0
    assert sidecar.calls[-1]["media"] == "sound:c"
    assert not sidecar.hangups

    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    task.cancel()


async def test_replace_with_nothing_playing_places_a_call(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    await _play(hass)
    await hass.async_block_till_done()
    assert len(sidecar.calls) == 1
    assert hass.states.get(ENTITY).state == STATE_IDLE


async def test_queue_policy_still_serialises(
    hass: HomeAssistant, setup_integration, sidecar, config_entry
) -> None:
    """The old behaviour is still available for announcement-heavy setups."""
    hass.config_entries.async_update_entry(config_entry, options={"policy": "queue"})
    await hass.async_block_till_done()
    sidecar.auto_disconnect = False

    task = await _in_flight(hass, "sound:chime")
    second = asyncio.create_task(_play(hass, "sound:evacuate"))
    await settle(hass)

    assert len(sidecar.calls) == 1
    assert hass.states.get(ENTITY).attributes["queued"] == 1

    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    task.cancel()
    second.cancel()
