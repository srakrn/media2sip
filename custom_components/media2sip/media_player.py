"""One media player entity per paging target.

The entity advertises exactly what a paging target can honour and nothing else.
For clips of a few seconds, PAUSE / SEEK / VOLUME_SET / BROWSE_MEDIA are theatre,
and advertising features the backend cannot honour breaks automations that trust
them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PbxPageConfigEntry
from .client import SidecarBusy, SidecarError
from .const import (
    CONF_CHIME,
    CONF_EXTENSION,
    CONF_GLOBAL_LOCK,
    CONF_LEAD_IN,
    CONF_NAME,
    CONF_POLICY,
    CONF_TARGET_ID,
    CONF_TARGETS,
    DEFAULT_POLICY,
    DOMAIN,
    EVENT_CONFIRMED,
    EVENT_DISCONNECTED,
    EVENT_EARLY,
    EVENT_CALLING,
    EVENT_PLAYBACK_STARTED,
    EVENT_PLAYBACK_PAUSED,
    EVENT_PLAYBACK_RESUMED,
    POLICY_PREEMPT,
    POLICY_QUEUE,
    POLICY_REPLACE,
    POLICY_REJECT,
    QUEUE_DEPTH,
    SOUND_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

# The virtual folder holding the sidecar's static clips.
SOUNDS_ROOT = "media2sip://sounds"


def _is_audio(item: media_source.BrowseMediaSource) -> bool:
    return item.media_content_type.startswith("audio/")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PbxPageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one entity per configured paging target."""
    async_add_entities(
        PbxPageMediaPlayer(entry, target) for target in entry.data[CONF_TARGETS]
    )


class _Page:
    """One queued page, and the future its caller is waiting on."""

    def __init__(self, media: str | None, chime: str | None, urgent: bool) -> None:
        self.media = media
        self.chime = chime
        self.urgent = urgent
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def resolve(self, result: Any) -> None:
        if not self.done.done():
            self.done.set_result(result)

    def fail(self, err: Exception) -> None:
        if not self.done.done():
            self.done.set_exception(err)


class PbxPageMediaPlayer(MediaPlayerEntity):
    """A paging target, presented as a media player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC
    _attr_should_poll = False

    def __init__(self, entry: PbxPageConfigEntry, target: dict[str, Any]) -> None:
        self._entry = entry
        self._data = entry.runtime_data
        self._client = self._data.client
        self._extension: str = str(target[CONF_EXTENSION])
        self._target_name: str = target[CONF_NAME]

        # Keyed on the target's id, not its extension, so pointing a target at a
        # different extension edits this entity rather than replacing it. The
        # fallback is only reached for an entry that predates the migration.
        target_id = str(target.get(CONF_TARGET_ID) or self._extension)
        self._attr_unique_id = f"{entry.entry_id}_{target_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self._target_name,
            manufacturer="Media2SIP",
            model=f"Paging target {self._extension}",
        )

        self._enabled = True
        self._call_id: str | None = None
        self._call_state: str | None = None
        self._paused = False
        self._last_error: str | None = None

        self._pending_finish: asyncio.Future | None = None
        self._queue: deque[_Page] = deque(maxlen=QUEUE_DEPTH)
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        self._data.entities[self.entity_id] = self
        self.async_on_remove(lambda: self._data.entities.pop(self.entity_id, None))
        self.async_on_remove(self._client.add_listener(self._handle_event))
        self._worker = self.hass.async_create_background_task(
            self._run_queue(), f"media2sip queue {self._extension}"
        )
        self.async_on_remove(self._cancel_worker)

    def _cancel_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    # -- presentation ----------------------------------------------------

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = (
            MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            # Browsing is honoured for real - the sidecar's own sounds plus
            # anything media_source offers - so it is advertised, unlike PAUSE
            # and SEEK which the backend could only pretend to support.
            #
            # It is also what puts the entity in the Media panel's player picker,
            # which filters on exactly this feature. Without it the entity is
            # reachable only from services and automations.
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            # Pause is honoured by disconnecting the player from the call rather
            # than stopping RTP: the call stays up, the clip does not advance,
            # and it resumes where it stopped. Worth having now that browsing can
            # put something longer than a chime down the line.
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.PLAY
        )
        if self._client.sounds:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        return features

    @property
    def available(self) -> bool:
        """Unavailable when the sidecar is unreachable or a registration is lost.

        This is the point of the whole design: a page group that silently stops
        working is worse than one that visibly breaks, and this gives you
        something to alert on.
        """
        return self._client.available

    @property
    def state(self) -> MediaPlayerState:
        if not self._enabled:
            return MediaPlayerState.OFF
        if self._call_state in (EVENT_CALLING, EVENT_EARLY, EVENT_CONFIRMED):
            return MediaPlayerState.BUFFERING
        if self._call_state == EVENT_PLAYBACK_STARTED:
            return MediaPlayerState.PAUSED if self._paused else MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def source_list(self) -> list[str] | None:
        return self._client.sounds or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "extension": self._extension,
            "call_id": self._call_id,
            "queued": len(self._queue),
            "paused": self._paused,
            "policy": self._policy,
            "last_error": self._last_error,
        }

    # -- options ---------------------------------------------------------

    @property
    def _options(self) -> dict[str, Any]:
        return self._entry.options

    @property
    def _policy(self) -> str:
        return self._options.get(CONF_POLICY, DEFAULT_POLICY)

    # -- commands --------------------------------------------------------

    async def async_turn_on(self) -> None:
        self._enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Soft disable. Also drops anything queued - a disabled target that
        later blurts out a backlog is worse than one that stayed quiet."""
        self._enabled = False
        self._drain_queue(HomeAssistantError(f"{self.entity_id} is turned off"))
        await self.async_media_stop()
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Hold the clip without dropping the call.

        The page group stays seized meanwhile, so the sidecar's max call length
        still applies - a forgotten pause cannot hold the handsets forever.
        """
        await self._async_set_paused(True)

    async def async_media_play(self) -> None:
        """Resume where the clip stopped."""
        await self._async_set_paused(False)

    async def _async_set_paused(self, paused: bool) -> None:
        if self._call_id is None:
            raise HomeAssistantError(f"{self.entity_id} is not playing anything")
        try:
            await self._client.async_set_paused(self._call_id, paused)
        except SidecarError as err:
            raise HomeAssistantError(f"cannot change playback: {err}") from err

    async def async_media_stop(self) -> None:
        if self._call_id is None:
            return
        try:
            await self._client.async_hangup(self._call_id)
        except SidecarError as err:
            _LOGGER.debug("hangup of %s failed: %s", self._call_id, err)

    async def async_select_source(self, source: str) -> None:
        await self.async_page(sound=source)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the sidecar's own sounds, then everything media_source offers.

        Audio only. A paging target has no use for a video library, and offering
        one would be the sort of empty gesture this entity otherwise avoids.
        """
        if media_content_id and media_content_id.startswith(SOUND_PREFIX):
            raise BrowseError(f"{media_content_id} is a sound, not a folder")

        if media_content_id == SOUNDS_ROOT:
            return self._browse_sounds()

        if media_content_id:
            return await media_source.async_browse_media(
                self.hass, media_content_id, content_filter=_is_audio
            )

        children: list[BrowseMedia] = []
        if self._client.sounds:
            children.append(self._browse_sounds())
        try:
            library = await media_source.async_browse_media(
                self.hass, None, content_filter=_is_audio
            )
        except BrowseError:
            library = None
        if library is not None and library.children:
            children.extend(library.children)

        return BrowseMedia(
            title=self._target_name,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            media_content_id="",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _browse_sounds(self) -> BrowseMedia:
        """The sidecar's static clips: already transcoded, played with no fetch."""
        return BrowseMedia(
            title="Sidecar sounds",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            media_content_id=SOUNDS_ROOT,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.MUSIC,
            children=[
                BrowseMedia(
                    title=name,
                    media_class=MediaClass.MUSIC,
                    media_content_type=MediaType.MUSIC,
                    media_content_id=f"{SOUND_PREFIX}{name}",
                    can_play=True,
                    can_expand=False,
                )
                for name in self._client.sounds
            ],
        )

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a clip.

        `announce: true` is treated as ordinary playback. There is nothing to duck
        or resume on a page, and raising on it would break `tts.speak` callers,
        which set it by default.
        """
        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = async_process_play_media_url(self.hass, play_item.url)
        elif not media_id.startswith(SOUND_PREFIX):
            media_id = async_process_play_media_url(self.hass, media_id)

        await self.async_page(media=media_id)

    async def async_page(
        self,
        text: str | None = None,
        sound: str | None = None,
        media: str | None = None,
        chime: str | None = None,
        urgent: bool = False,
    ) -> None:
        """Queue a page and wait for it to be placed.

        Used by `async_play_media`, `async_select_source`, and the `media2sip.page`
        service, so the concurrency policy applies uniformly however a page arrives.
        """
        if not self._enabled:
            raise HomeAssistantError(f"{self.entity_id} is turned off")
        if text is not None:
            raise HomeAssistantError(
                "media2sip.page with `text` needs a TTS entity; call tts.speak against "
                "this media player instead, or pass `sound`"
            )
        if sound is not None:
            media = sound if sound.startswith(SOUND_PREFIX) else f"{SOUND_PREFIX}{sound}"
        if media is None:
            raise HomeAssistantError("nothing to play")

        page = _Page(media=media, chime=chime or self._options.get(CONF_CHIME), urgent=urgent)

        policy = POLICY_PREEMPT if urgent else self._policy

        if policy == POLICY_REPLACE and self._call_id is not None:
            # Swap the audio on the call that is already up. It starts on the
            # next frame, with no re-dial and no lead-in, because the handsets
            # are already listening. The call id does not change, so the queue
            # worker still holding this call stays correct.
            await self._replace(page)
            return

        if policy == POLICY_REJECT and (self._call_id is not None or self._queue):
            raise HomeAssistantError(f"{self.entity_id} is busy")
        if policy == POLICY_PREEMPT:
            # The newest message is the one that matters. A routine announcement
            # must never delay an alarm.
            self._drain_queue(HomeAssistantError("preempted by a newer page"))

        if len(self._queue) == self._queue.maxlen:
            # deque drops the oldest on overflow, which is the behaviour we want;
            # tell its caller rather than leaving it waiting forever.
            self._queue[0].fail(HomeAssistantError("dropped: page queue overflowed"))
        self._queue.append(page)
        self._wake.set()
        self.async_write_ha_state()

        await page.done

    async def _replace(self, page: _Page) -> None:
        try:
            result = await self._client.async_place_call(**self._payload(page, "replace"))
        except SidecarError as err:
            self._last_error = str(err)
            self.async_write_ha_state()
            raise HomeAssistantError(f"cannot replace what is playing: {err}") from err
        self._call_id = result["call_id"]
        self._paused = False
        self._last_error = None
        self.async_write_ha_state()

    def _payload(self, page: _Page, policy: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": self._extension,
            "media": page.media,
            "policy": policy,
        }
        if page.chime:
            payload["chime"] = (
                page.chime if page.chime.startswith(SOUND_PREFIX) else f"{SOUND_PREFIX}{page.chime}"
            )
        if (lead_in := self._options.get(CONF_LEAD_IN)) is not None:
            payload["lead_in"] = lead_in
        return payload

    # -- queue worker ----------------------------------------------------

    async def _run_queue(self) -> None:
        """Serialise pages for this target.

        One target is one set of handsets, so two overlapping calls would collide
        even if the PBX allowed it.
        """
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue

            page = self._queue.popleft()
            self.async_write_ha_state()
            try:
                await self._place(page)
                page.resolve(None)
            except asyncio.CancelledError:
                page.fail(HomeAssistantError("shutting down"))
                raise
            except Exception as err:  # noqa: BLE001
                self._last_error = str(err)
                page.fail(err)
                self.async_write_ha_state()

    async def _place(self, page: _Page) -> None:
        lock = self._data.global_lock if self._options.get(CONF_GLOBAL_LOCK) else None
        if lock is not None:
            async with lock:
                await self._place_locked(page)
        else:
            await self._place_locked(page)

    async def _place_locked(self, page: _Page) -> None:
        payload = self._payload(page, "preempt" if page.urgent else "reject")

        finished = asyncio.get_running_loop().create_future()
        self._pending_finish = finished
        try:
            result = await self._client.async_place_call(**payload)
        except SidecarBusy as err:
            raise HomeAssistantError(f"{self.entity_id} is busy: {err}") from err
        except SidecarError as err:
            raise HomeAssistantError(f"page to {self._extension} failed: {err}") from err

        self._call_id = result["call_id"]
        # Not result["state"]: the sidecar emits `calling` while the POST is still
        # in flight, so that event arrives before we know the call id and is
        # filtered out. Without this the entity reports `idle` for a beat with a
        # call genuinely in progress, and an automation reading state right then
        # gets the wrong answer.
        self._call_state = EVENT_CALLING
        self._last_error = None
        self.async_write_ha_state()

        # Hold the queue slot until the call really ends, so the next page cannot
        # be placed on top of this one.
        try:
            async with asyncio.timeout(self._call_timeout(result)):
                await finished
        except TimeoutError:
            _LOGGER.warning(
                "no disconnect event for call %s; releasing the queue anyway", self._call_id
            )
        finally:
            self._pending_finish = None

    @staticmethod
    def _call_timeout(result: dict[str, Any]) -> float:
        """Never wait on the sidecar forever - a lost event must not wedge the queue."""
        return float(result.get("duration", 0)) + float(result.get("lead_in", 0)) + 30.0

    def _drain_queue(self, err: Exception) -> None:
        while self._queue:
            self._queue.popleft().fail(err)

    # -- events ----------------------------------------------------------

    @callback
    def _handle_event(self, event: dict[str, Any]) -> None:
        """Drive entity state from sidecar events, and nothing else."""
        event_type = event.get("type")

        if event_type == "connection" or event_type in ("registered", "unregistered"):
            if not self._client.available:
                self._call_id = None
                self._call_state = None
                self._paused = False
            self.async_write_ha_state()
            return

        if event.get("call_id") is None or event["call_id"] != self._call_id:
            return

        if event_type == EVENT_PLAYBACK_PAUSED:
            self._paused = True
            self.async_write_ha_state()
            return
        if event_type == EVENT_PLAYBACK_RESUMED:
            self._paused = False
            self.async_write_ha_state()
            return

        if event_type == EVENT_DISCONNECTED:
            self._call_state = None
            self._call_id = None
            self._paused = False
            if (reason := event.get("reason")) not in (None, "disconnected", "playback_complete"):
                self._last_error = f"{reason} ({event.get('sip_code')} {event.get('sip_reason')})"
            finish = self._pending_finish
            if finish is not None and not finish.done():
                finish.set_result(None)
        else:
            self._call_state = event_type

        self.async_write_ha_state()
