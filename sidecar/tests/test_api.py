"""The control API's contract, with the SIP worker stubbed.

This is the surface the integration depends on, so its shape is worth pinning
down independently of whether a PBX is reachable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.media import MediaResolver
from app.sip import SipError


class StubSip:
    """Stands in for the pjsua2 worker."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.hungup: list[str] = []
        self.registered = True
        self.active: list[dict] = []
        self.recorded: list[dict] = []
        self.replaced: list[dict] = []
        self.paused: list[tuple[str, bool]] = []
        self.place_error: Exception | None = None
        self.pause_error: Exception | None = None

    def registration_state(self) -> dict[str, dict]:
        return {
            "9901": {"account_id": "9901", "uri": "sip:9901@pbx",
                     "registered": self.registered, "code": 200, "reason": "OK", "since": 0.0}
        }

    def active_calls(self) -> list[dict]:
        return self.active

    async def place_call(self, call_id: str, target: str, clip: Path, duration: float,
                         lead_in: float | None = None, account_id: str | None = None,
                         media_label: str = "") -> dict:
        if self.place_error is not None:
            raise self.place_error
        self.placed.append({"call_id": call_id, "target": target, "clip": clip,
                            "duration": duration, "lead_in": lead_in,
                            "media_label": media_label})
        call = {"call_id": call_id, "target": target, "account_id": "9901", "state": "new",
                "duration": duration, "lead_in": lead_in or 1.0, "sip_code": 0, "sip_reason": ""}
        self.active.append(call)
        return call

    async def replace_media(self, call_id: str, clip: Path, duration: float,
                            media_label: str = "") -> dict:
        self.replaced.append({"call_id": call_id, "clip": clip, "duration": duration})
        call = next(c for c in self.active if c["call_id"] == call_id)
        return {**call, "duration": duration}

    async def set_paused(self, call_id: str, paused: bool) -> dict:
        if self.pause_error is not None:
            raise self.pause_error
        self.paused.append((call_id, paused))
        return {"call_id": call_id, "paused": paused}

    async def hangup(self, call_id: str) -> dict:
        self.hungup.append(call_id)
        return {"call_id": call_id}

    def history(self, limit: int = 20) -> list[dict]:
        return self.recorded[-limit:]

    async def hangup_target(self, target: str) -> list[str]:
        hung = [c["call_id"] for c in self.active if c["target"] == target]
        self.active = [c for c in self.active if c["target"] != target]
        self.hungup.extend(hung)
        return hung

    async def stop(self) -> None:
        return None


@pytest.fixture
def client(monkeypatch, cache_dir: Path, sounds_dir: Path, make_wav):
    make_wav(sounds_dir / "chime.wav", seconds=1.0)
    stub = StubSip()

    async def fake_start(self) -> None:
        loop = asyncio.get_running_loop()
        from app import events as ev

        self.bus = ev.EventBus(loop)
        self.media = MediaResolver(cache_dir=cache_dir, sounds_dir=sounds_dir, sample_rate=8000)
        self.sip = stub

    monkeypatch.setattr(main_module.Sidecar, "start", fake_start)
    # Supply config directly rather than through the environment, so the API's
    # contract is tested without a PBX anywhere in sight.
    from app.config import Config, SipAccount

    monkeypatch.setattr(
        main_module.sidecar,
        "_config",
        Config(
            accounts=[SipAccount(id="9901", username="9901", password="x", host="pbx")],
            cache_dir=cache_dir,
            sounds_dir=sounds_dir,
        ),
    )

    with TestClient(main_module.app) as test_client:
        test_client.stub = stub
        yield test_client


def test_health_reports_registration_and_sounds(client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["accounts"][0]["registered"] is True
    assert body["sounds"] == ["chime"]
    assert body["codecs"] == ["PCMU/8000/1", "PCMA/8000/1"]


def test_health_is_degraded_when_unregistered(client) -> None:
    client.stub.registered = False
    assert client.get("/health").json()["status"] == "degraded"


def test_place_a_call(client) -> None:
    response = client.post("/call", json={"target": "991", "chime": "sound:chime"})
    assert response.status_code == 200

    body = response.json()
    assert body["target"] == "991"
    assert body["duration"] == pytest.approx(1.0, abs=0.05)
    assert client.stub.placed[0]["target"] == "991"


def test_call_needs_something_to_play(client) -> None:
    response = client.post("/call", json={"target": "991"})
    assert response.status_code == 400
    assert "media or chime" in response.json()["detail"]


def test_bad_media_fails_before_the_phone_rings(client) -> None:
    """A missing clip must be a 400, not handsets answering to silence."""
    response = client.post("/call", json={"target": "991", "chime": "sound:nope"})
    assert response.status_code == 400
    assert response.json()["kind"] == "media"
    assert not client.stub.placed


def test_sound_name_traversal_is_rejected(client) -> None:
    response = client.post("/call", json={"target": "991", "chime": "sound:../../etc/passwd"})
    assert response.status_code == 400
    assert not client.stub.placed


def test_reject_policy_returns_409_when_busy(client) -> None:
    client.post("/call", json={"target": "991", "chime": "sound:chime"})
    response = client.post("/call", json={"target": "991", "chime": "sound:chime"})
    assert response.status_code == 409
    assert len(client.stub.placed) == 1


def test_preempt_policy_hangs_up_the_old_call(client) -> None:
    first = client.post("/call", json={"target": "991", "chime": "sound:chime"}).json()
    second = client.post(
        "/call", json={"target": "991", "chime": "sound:chime", "policy": "preempt"}
    )
    assert second.status_code == 200
    assert second.json()["preempted"] == [first["call_id"]]
    assert client.stub.hungup == [first["call_id"]]


def test_unregistered_account_refuses_the_call(client) -> None:
    client.stub.place_error = SipError("account 9901 is not registered", 503)
    response = client.post("/call", json={"target": "991", "chime": "sound:chime"})
    assert response.status_code == 503
    assert response.json()["kind"] == "sip"


def test_busy_sip_code_is_carried_through(client) -> None:
    """486 has to reach the integration, because it is what explains the failure."""
    client.stub.place_error = SipError("busy here", 486)
    response = client.post("/call", json={"target": "991", "chime": "sound:chime"})
    assert response.status_code == 409
    assert response.json()["sip_code"] == 486


def test_hangup(client) -> None:
    call_id = client.post("/call", json={"target": "991", "chime": "sound:chime"}).json()["call_id"]
    assert client.delete(f"/call/{call_id}").status_code == 200
    assert client.stub.hungup == [call_id]


def test_sounds_listing(client) -> None:
    assert client.get("/sounds").json() == {"sounds": ["chime"]}


def test_recent_events_are_available_to_a_reconnecting_client(client) -> None:
    assert client.get("/events/recent").status_code == 200


def test_websocket_greets_with_registration_state(client) -> None:
    """A freshly connected integration must know availability without waiting
    for a state change."""
    with client.websocket_connect("/ws") as socket:
        hello = socket.receive_json()
    assert hello["type"] == "hello"
    assert hello["accounts"][0]["registered"] is True


def test_history_endpoint(client) -> None:
    client.stub.recorded = [
        {"call_id": "a", "target": "991", "sip_code": 200, "audio_sent": True},
        {"call_id": "b", "target": "992", "sip_code": 486, "audio_sent": False},
    ]
    body = client.get("/calls/history").json()
    assert [c["call_id"] for c in body] == ["a", "b"]
    assert body[1]["sip_code"] == 486


def test_history_respects_its_limit(client) -> None:
    client.stub.recorded = [{"call_id": str(i)} for i in range(10)]
    assert len(client.get("/calls/history?limit=3").json()) == 3


def test_media_label_reaches_the_sip_layer(client) -> None:
    """So the history record can say what played without holding a URL token."""
    client.post("/call", json={"target": "991", "chime": "sound:chime"})
    assert client.stub.placed[0]["media_label"] == "sound:chime"


def test_version_comes_from_the_build_not_the_source(monkeypatch) -> None:
    """Nothing in the sidecar's source declares a version.

    It is stamped into the image at build time, so there is no literal here to go
    stale, and an unstamped build says so plainly instead of claiming a release.
    """
    import importlib

    from app import main

    monkeypatch.delenv("APP_VERSION", raising=False)
    assert importlib.reload(main).VERSION == "dev"

    monkeypatch.setenv("APP_VERSION", "1.2.3")
    assert importlib.reload(main).VERSION == "1.2.3"


def test_replace_swaps_audio_on_the_live_call(client) -> None:
    """No new call: the handsets are already listening, so re-dialling would
    only drop them and make them answer again."""
    first = client.post("/call", json={"target": "991", "chime": "sound:chime"}).json()
    second = client.post(
        "/call", json={"target": "991", "chime": "sound:chime", "policy": "replace"}
    )
    assert second.status_code == 200
    body = second.json()
    assert body["replaced"] is True
    assert body["call_id"] == first["call_id"]
    assert client.stub.hungup == []


def test_replace_with_nothing_in_flight_places_a_call(client) -> None:
    response = client.post(
        "/call", json={"target": "991", "chime": "sound:chime", "policy": "replace"}
    )
    assert response.status_code == 200
    assert response.json()["replaced"] is False
    assert len(client.stub.placed) == 1


def test_replace_still_validates_the_media_first(client) -> None:
    client.post("/call", json={"target": "991", "chime": "sound:chime"})
    response = client.post(
        "/call", json={"target": "991", "chime": "sound:nope", "policy": "replace"}
    )
    assert response.status_code == 400
    assert client.stub.replaced == []


def test_pause_and_resume(client) -> None:
    call_id = client.post("/call", json={"target": "991", "chime": "sound:chime"}).json()["call_id"]
    assert client.post(f"/call/{call_id}/pause", json={"paused": True}).status_code == 200
    assert client.stub.paused == [(call_id, True)]
    assert client.post(f"/call/{call_id}/pause", json={"paused": False}).status_code == 200
    assert client.stub.paused[-1] == (call_id, False)


def test_pause_needs_a_call(client) -> None:
    from app.sip import SipError

    client.stub.pause_error = SipError("no such call: nope", 404)
    assert client.post("/call/nope/pause", json={"paused": True}).status_code == 409
