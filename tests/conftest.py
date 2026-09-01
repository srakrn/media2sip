"""Shared fixtures.

Everything here mocks the *sidecar*, not the SIP stack. That is the whole point of
splitting the two: the integration can be tested exhaustively against a fake
control API, including the failure modes that are impractical to rehearse on real
handsets.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.media2sip.const import (
    CONF_EXTENSION,
    CONF_NAME,
    CONF_TARGETS,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


class FakeSidecar:
    """A stand-in for the sidecar's control API.

    Records what the integration sent and lets a test drive the event stream by
    hand, which is how the interesting cases (registration lost mid-call, a call
    that never disconnects) become testable at all.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.hangups: list[str] = []
        self.place_error: Exception | None = None
        self.next_call_id = "call-1"
        self.auto_disconnect = True
        self.paused = False

        self.connected = True
        self.sounds: list[str] = ["chime", "evacuate"]
        self.lead_in = 1.0
        self.sidecar_version = "test"
        self.in_flight: str | None = None
        self.accounts: dict[str, dict] = {
            "9901": {"account_id": "9901", "uri": "sip:9901@pbx", "registered": True,
                     "code": 200, "reason": "OK", "since": 0.0}
        }
        self._listeners: list[Callable[[dict], None]] = []

    # -- the client interface the integration actually uses --------------

    @property
    def registered(self) -> bool:
        return bool(self.accounts) and all(a["registered"] for a in self.accounts.values())

    @property
    def available(self) -> bool:
        return self.connected and self.registered

    def add_listener(self, callback):
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    async def async_start(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None

    async def async_health(self) -> dict:
        return {
            "status": "ok" if self.registered else "degraded",
            "version": "test",
            "accounts": list(self.accounts.values()),
            "active_calls": [],
            "sounds": self.sounds,
            "lead_in": self.lead_in,
            "codecs": ["PCMU/8000/1"],
        }

    async def async_place_call(self, **payload: Any) -> dict:
        if self.place_error is not None:
            raise self.place_error
        self.calls.append(payload)
        replacing = payload.get("policy") == "replace" and self.in_flight is not None
        call_id = self.in_flight if replacing else self.next_call_id
        result = {
            "call_id": call_id,
            "target": payload["target"],
            "account_id": "9901",
            "state": "new",
            "duration": 1.0,
            "lead_in": payload.get("lead_in", 1.0),
            "media": payload.get("media", ""),
            "cached": True,
            "preempted": [],
        }
        result["replaced"] = replacing
        if replacing:
            self.emit("playback_started", call_id=call_id, target=payload["target"])
            return result

        self.in_flight = call_id
        self.emit("calling", call_id=call_id, target=payload["target"])
        if self.auto_disconnect:
            # Model a healthy page: confirmed, played, hung up.
            async def finish() -> None:
                await asyncio.sleep(0)
                self.emit("confirmed", call_id=call_id, target=payload["target"])
                self.emit("playback_started", call_id=call_id, target=payload["target"])
                self.in_flight = None
                self.emit("disconnected", call_id=call_id, target=payload["target"],
                          reason="playback_complete", sip_code=200, sip_reason="OK")

            asyncio.get_running_loop().create_task(finish())
        return result

    async def async_set_paused(self, call_id: str, paused: bool) -> dict:
        self.paused = paused
        self.emit("playback_paused" if paused else "playback_resumed", call_id=call_id)
        return {"call_id": call_id, "paused": paused}

    async def async_hangup(self, call_id: str) -> dict:
        self.hangups.append(call_id)
        self.in_flight = None
        self.emit("disconnected", call_id=call_id, reason="requested",
                  sip_code=200, sip_reason="OK")
        return {"call_id": call_id}

    async def async_active_calls(self) -> list[dict]:
        return []

    async def async_history(self, limit: int = 20) -> list[dict]:
        return [
            {
                "call_id": "old-1", "target": "991", "account_id": "9901",
                "media": "url(ha.test) url-abc123-8000", "at": 0.0,
                "clip_duration": 1.0, "lead_in": 1.0,
                "answer_latency": 0.2, "playback_latency": 1.2, "total": 2.5,
                "sip_code": 200, "sip_reason": "OK", "end_reason": "playback_complete",
                "rtp": {"tx_pkt": 113, "tx_bytes": 18080, "rx_pkt": 112, "rx_bytes": 17920},
                "audio_sent": True,
            }
        ]

    # -- test controls ---------------------------------------------------

    def emit(self, event_type: str, **fields: Any) -> None:
        for listener in list(self._listeners):
            listener({"type": event_type, **fields})

    def set_registered(self, registered: bool, code: int = 200, reason: str = "OK") -> None:
        for account in self.accounts.values():
            account["registered"] = registered
            account["code"] = code
            account["reason"] = reason
        self.emit("registered" if registered else "unregistered",
                  account_id="9901", code=code, reason=reason)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.emit("connection", connected=connected)


async def settle(hass, cycles: int = 60) -> None:
    """Let the event loop run, without waiting on tasks still in flight.

    `hass.async_block_till_done()` waits for the entity's queue worker to finish
    the page it is on, so calling it while a page is deliberately held open blocks
    for the full call timeout. These tests need the loop to advance, not to drain.
    """
    for _ in range(cycles):
        await asyncio.sleep(0)


@pytest.fixture
def integration_version() -> str:
    """The version the integration actually declares.

    Never hardcode it in a test: the release workflow bumps the manifest, and a
    hardcoded copy turns every release into a red build - which is exactly what
    happened the first time a release was attempted.
    """
    manifest = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components/media2sip/manifest.json"
    )
    return json.loads(manifest.read_text())["version"]


@pytest.fixture
def sidecar() -> FakeSidecar:
    return FakeSidecar()


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Media2SIP (test)",
        # The flow sets the sidecar URL as the unique id; a fixture that omits it
        # would not catch a duplicate-entry regression.
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


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, sidecar: FakeSidecar
):
    """Set up the entry with the fake sidecar in place of the real client.

    The patch stays open for the whole test: updating options triggers an entry
    reload, which would otherwise construct a real client and try to talk to the
    network.
    """
    config_entry.add_to_hass(hass)
    with patch("custom_components.media2sip.PbxPageClient", return_value=sidecar):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        yield config_entry
