"""Entity behaviour, driven entirely through the fake sidecar's event stream."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    MediaPlayerEntityFeature,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_IDLE,
    STATE_OFF,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.media2sip.client import SidecarBusy, SidecarError

from .conftest import settle

ENTITY = "media_player.working_zone"


async def _play(hass: HomeAssistant, media_id: str = "sound:chime", entity: str = ENTITY) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: entity,
            ATTR_MEDIA_CONTENT_TYPE: "music",
            ATTR_MEDIA_CONTENT_ID: media_id,
        },
        blocking=True,
    )


async def test_entities_created(hass: HomeAssistant, setup_integration) -> None:
    """One entity per configured target, idle to start."""
    assert hass.states.get(ENTITY).state == STATE_IDLE
    assert hass.states.get("media_player.warehouse").state == STATE_IDLE


async def test_advertises_only_supportable_features(
    hass: HomeAssistant, setup_integration
) -> None:
    """Advertising a feature the backend cannot honour breaks automations that
    trust it, so the set is exact rather than generous.

    BROWSE_MEDIA, PAUSE and PLAY are in the set because they are genuinely
    honoured. SEEK, volume and track navigation are not: the sidecar has no
    position control, no mixer, and no notion of a playlist, so advertising them
    would be a promise it could not keep.
    """
    features = hass.states.get(ENTITY).attributes[ATTR_SUPPORTED_FEATURES]
    expected = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PLAY
    )
    assert features == expected
    for unsupported in (
        MediaPlayerEntityFeature.SEEK,
        MediaPlayerEntityFeature.VOLUME_SET,
        MediaPlayerEntityFeature.VOLUME_MUTE,
        MediaPlayerEntityFeature.NEXT_TRACK,
        MediaPlayerEntityFeature.SHUFFLE_SET,
        MediaPlayerEntityFeature.REPEAT_SET,
    ):
        assert not features & unsupported


async def test_call_success_returns_to_idle(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """A healthy page places one call and ends idle."""
    await _play(hass)
    await hass.async_block_till_done()

    assert len(sidecar.calls) == 1
    assert sidecar.calls[0]["target"] == "991"
    assert sidecar.calls[0]["media"] == "sound:chime"
    assert hass.states.get(ENTITY).state == STATE_IDLE


async def _start_page(hass: HomeAssistant, **kwargs) -> asyncio.Task:
    """Begin a page and return once it is in flight.

    A plain asyncio task, not hass.async_create_task: the latter is tracked by
    async_block_till_done, which would then wait forever on a page these tests
    deliberately leave unfinished.
    """
    task = asyncio.create_task(_play(hass, **kwargs))
    for _ in range(60):
        await asyncio.sleep(0)
        if hass.states.get(ENTITY).attributes.get("call_id"):
            break
    return task


async def test_state_follows_sidecar_events(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """calling/early/confirmed are buffering; playback_started is playing."""
    sidecar.auto_disconnect = False
    task = await _start_page(hass)
    assert hass.states.get(ENTITY).state == "buffering"

    sidecar.emit("early", call_id="call-1", target="991")
    await settle(hass)
    assert hass.states.get(ENTITY).state == "buffering"

    sidecar.emit("confirmed", call_id="call-1", target="991")
    await settle(hass)
    assert hass.states.get(ENTITY).state == "buffering"

    sidecar.emit("playback_started", call_id="call-1", target="991")
    await settle(hass)
    assert hass.states.get(ENTITY).state == "playing"

    sidecar.emit("disconnected", call_id="call-1", target="991",
                 reason="playback_complete", sip_code=200, sip_reason="OK")
    await settle(hass)
    assert hass.states.get(ENTITY).state == STATE_IDLE
    await task


async def test_events_for_another_call_are_ignored(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """Two targets share one event stream, so each entity must filter by call_id."""
    sidecar.auto_disconnect = False
    task = await _start_page(hass)

    # An event for a different call must not move this entity.
    sidecar.emit("playback_started", call_id="someone-else", target="992")
    await settle(hass)
    assert hass.states.get(ENTITY).state == "buffering"

    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    await task


async def test_busy_486_surfaces_as_an_error(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """A busy page group returns 486; the caller must be told, not left guessing."""
    sidecar.place_error = SidecarBusy("991 already has a call in flight", 409, 486)

    with pytest.raises(HomeAssistantError, match="busy"):
        await _play(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY).state == STATE_IDLE
    assert "busy" in hass.states.get(ENTITY).attributes["last_error"]


async def test_unavailable_480_surfaces_as_an_error(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """480 temporarily unavailable is a failure, not a silent no-op."""
    sidecar.place_error = SidecarError("no answer", 503, 480)

    with pytest.raises(HomeAssistantError):
        await _play(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_IDLE


async def test_registration_loss_makes_entity_unavailable(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """The failure this project most needs to be alertable on."""
    assert hass.states.get(ENTITY).state == STATE_IDLE

    sidecar.set_registered(False, code=403, reason="Forbidden")
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE

    sidecar.set_registered(True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_IDLE


async def test_registration_loss_mid_call_clears_the_call(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """Losing registration during a page must not leave a stale call id behind."""
    sidecar.auto_disconnect = False
    task = await _start_page(hass)
    assert hass.states.get(ENTITY).attributes["call_id"] == "call-1"

    sidecar.set_registered(False, code=408, reason="Request Timeout")
    await settle(hass)

    # Home Assistant strips extra_state_attributes while an entity is
    # unavailable, so the call id is checked once it comes back.
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE

    sidecar.set_registered(True)
    await settle(hass)

    state = hass.states.get(ENTITY)
    assert state.state == STATE_IDLE
    assert state.attributes["call_id"] is None

    # Release the queue worker so it is not left waiting on a call that ended
    # while we were not looking.
    sidecar.emit("disconnected", call_id="call-1", reason="registration_lost")
    await settle(hass)
    task.cancel()


async def test_sidecar_disconnect_makes_entity_unavailable(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    sidecar.set_connected(False)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE

    sidecar.set_connected(True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_IDLE


async def test_turn_off_rejects_pages_and_turn_on_restores(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    await hass.services.async_call(
        MP_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    assert hass.states.get(ENTITY).state == STATE_OFF

    with pytest.raises(HomeAssistantError, match="turned off"):
        await _play(hass)
    assert not sidecar.calls

    await hass.services.async_call(
        MP_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    assert hass.states.get(ENTITY).state == STATE_IDLE

    await _play(hass)
    await hass.async_block_till_done()
    assert len(sidecar.calls) == 1


async def test_source_list_comes_from_the_sidecar(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    assert hass.states.get(ENTITY).attributes["source_list"] == ["chime", "evacuate"]

    await hass.services.async_call(
        MP_DOMAIN,
        "select_source",
        {ATTR_ENTITY_ID: ENTITY, "source": "evacuate"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert sidecar.calls[0]["media"] == "sound:evacuate"


async def test_default_chime_and_lead_in_from_options(
    hass: HomeAssistant, setup_integration, sidecar, config_entry
) -> None:
    hass.config_entries.async_update_entry(
        config_entry, options={"chime": "chime", "lead_in": 2.5}
    )
    await hass.async_block_till_done()

    await _play(hass)
    await hass.async_block_till_done()

    assert sidecar.calls[0]["chime"] == "sound:chime"
    assert sidecar.calls[0]["lead_in"] == 2.5
