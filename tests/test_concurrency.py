"""Concurrency policy: queue, preempt, reject, and the global lock."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import settle
from custom_components.media2sip.const import (
    CONF_GLOBAL_LOCK,
    CONF_POLICY,
    DOMAIN,
    POLICY_PREEMPT,
    POLICY_QUEUE,
    POLICY_REJECT,
    QUEUE_DEPTH,
)

ENTITY = "media_player.working_zone"


def _play(hass: HomeAssistant, media_id: str = "sound:chime", entity: str = ENTITY):
    return hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: entity,
            ATTR_MEDIA_CONTENT_TYPE: "music",
            ATTR_MEDIA_CONTENT_ID: media_id,
        },
        blocking=True,
    )


async def _set_policy(hass: HomeAssistant, entry, **options) -> None:
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()


async def _in_flight(hass: HomeAssistant) -> asyncio.Task:
    """Start a page and leave it hanging, so the next one has to contend."""
    task = asyncio.create_task(_play(hass))
    for _ in range(60):
        await asyncio.sleep(0)
        if hass.states.get(ENTITY).attributes.get("call_id"):
            break
    return task


async def test_reject_fails_fast_when_busy(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """Cheap and correct, now that a busy page group returns a real 486."""
    await _set_policy(hass, setup_integration, **{CONF_POLICY: POLICY_REJECT})
    sidecar.auto_disconnect = False

    first = await _in_flight(hass)
    with pytest.raises(HomeAssistantError, match="busy"):
        await _play(hass)

    assert len(sidecar.calls) == 1
    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    await first


async def test_queue_serialises_pages(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """One target is one set of handsets, so pages must not overlap."""
    await _set_policy(hass, setup_integration, **{CONF_POLICY: POLICY_QUEUE})
    sidecar.auto_disconnect = False

    first = await _in_flight(hass)
    second = asyncio.create_task(_play(hass, "sound:evacuate"))
    await settle(hass)

    # Still only one call placed: the second is waiting its turn.
    assert len(sidecar.calls) == 1
    assert hass.states.get(ENTITY).attributes["queued"] == 1

    sidecar.next_call_id = "call-2"
    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)

    assert len(sidecar.calls) == 2
    assert sidecar.calls[1]["media"] == "sound:evacuate"

    sidecar.emit("disconnected", call_id="call-2", reason="playback_complete")
    await settle(hass)
    await first
    await second


async def test_queue_overflow_drops_oldest_and_tells_its_caller(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """A dropped page must not leave its caller waiting forever on something
    that will never happen."""
    await _set_policy(hass, setup_integration, **{CONF_POLICY: POLICY_QUEUE})
    sidecar.auto_disconnect = False

    first = await _in_flight(hass)

    # Fill the queue past its bound.
    queued = [asyncio.create_task(_play(hass, f"sound:q{i}")) for i in range(QUEUE_DEPTH + 1)]
    await settle(hass)

    assert hass.states.get(ENTITY).attributes["queued"] == QUEUE_DEPTH

    # The oldest queued page was dropped, and told so.
    with pytest.raises(HomeAssistantError, match="overflow"):
        await queued[0]

    for task in (first, *queued[1:]):
        task.cancel()


async def test_preempt_hangs_up_and_plays_the_newest(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """A routine announcement must never delay an alarm."""
    await _set_policy(hass, setup_integration, **{CONF_POLICY: POLICY_PREEMPT})
    sidecar.auto_disconnect = False

    first = await _in_flight(hass)
    queued = [asyncio.create_task(_play(hass, f"sound:q{i}")) for i in range(2)]
    await settle(hass)

    # Preempt drains whatever was waiting rather than letting it pile up.
    assert hass.states.get(ENTITY).attributes["queued"] <= 1
    with pytest.raises(HomeAssistantError, match="preempted"):
        await queued[0]

    first.cancel()
    for task in queued[1:]:
        task.cancel()


async def test_urgent_forces_preempt_regardless_of_policy(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """`priority: urgent` is for the smoke, leak and siren chain."""
    await _set_policy(hass, setup_integration, **{CONF_POLICY: POLICY_QUEUE})

    await hass.services.async_call(
        DOMAIN,
        "page",
        {"targets": [ENTITY], "sound": "evacuate", "priority": "urgent"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert sidecar.calls[0]["policy"] == "preempt"
    assert sidecar.calls[0]["media"] == "sound:evacuate"


async def test_normal_priority_does_not_preempt(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    await hass.services.async_call(
        DOMAIN, "page", {"targets": [ENTITY], "sound": "chime"}, blocking=True
    )
    await hass.async_block_till_done()
    assert sidecar.calls[0]["policy"] == "reject"


async def test_page_service_reports_a_failed_target(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """One dead target must not hide the others working, nor be swallowed."""
    with pytest.raises(HomeAssistantError, match="not a media2sip media player"):
        await hass.services.async_call(
            DOMAIN,
            "page",
            {"targets": [ENTITY, "media_player.nope"], "sound": "chime"},
            blocking=True,
        )


async def test_page_service_needs_text_or_sound(
    hass: HomeAssistant, setup_integration
) -> None:
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN, "page", {"targets": [ENTITY]}, blocking=True
        )


async def test_global_lock_serialises_across_targets(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    """For targets that share physical handsets - explicit, never inferred."""
    await _set_policy(hass, setup_integration, **{CONF_GLOBAL_LOCK: True})
    sidecar.auto_disconnect = False

    first = await _in_flight(hass)
    other = asyncio.create_task(_play(hass, entity="media_player.warehouse"))
    await settle(hass)

    # The second target is held off even though it is a different entity.
    assert len(sidecar.calls) == 1

    sidecar.next_call_id = "call-2"
    sidecar.emit("disconnected", call_id="call-1", reason="playback_complete")
    await settle(hass)
    assert len(sidecar.calls) == 2
    assert sidecar.calls[1]["target"] == "992"

    sidecar.emit("disconnected", call_id="call-2", reason="playback_complete")
    await settle(hass)
    await first
    await other
