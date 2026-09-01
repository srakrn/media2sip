"""The sidecar client's HTTP surface and its error mapping."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.media2sip.client import PbxPageClient, SidecarBusy, SidecarError

URL = "http://sidecar.test:8080"


def _client(hass: HomeAssistant, token: str | None = None) -> PbxPageClient:
    return PbxPageClient(async_get_clientsession(hass), URL, token)


async def test_health(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/health", json={"status": "ok", "accounts": []})
    assert (await _client(hass).async_health())["status"] == "ok"


async def test_busy_maps_to_its_own_exception(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """409 has to be distinguishable, because `reject` is a normal outcome
    rather than a fault."""
    aioclient_mock.post(
        f"{URL}/call", status=409, json={"detail": "991 already has a call in flight"}
    )
    with pytest.raises(SidecarBusy, match="already has a call"):
        await _client(hass).async_place_call(target="991", media="sound:chime")


async def test_sip_failure_carries_its_code(hass: HomeAssistant, aioclient_mock) -> None:
    """The SIP reason code is the thing that explains a failed page."""
    aioclient_mock.post(
        f"{URL}/call", status=503, json={"detail": "no answer", "kind": "sip", "sip_code": 480}
    )
    with pytest.raises(SidecarError) as err:
        await _client(hass).async_place_call(target="991", media="sound:chime")
    assert err.value.sip_code == 480
    assert err.value.status == 503


async def test_media_failure_surfaces_the_reason(
    hass: HomeAssistant, aioclient_mock
) -> None:
    aioclient_mock.post(
        f"{URL}/call", status=400, json={"detail": "no such sound: 'nope'", "kind": "media"}
    )
    with pytest.raises(SidecarError, match="no such sound"):
        await _client(hass).async_place_call(target="991", media="sound:nope")


async def test_unreachable_sidecar(hass: HomeAssistant, aioclient_mock) -> None:
    import aiohttp

    aioclient_mock.get(
        f"{URL}/health", exc=aiohttp.ClientConnectionError("connection refused")
    )
    with pytest.raises(SidecarError, match="cannot reach the sidecar"):
        await _client(hass).async_health()


async def test_bearer_token_is_sent(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{URL}/health", json={"status": "ok"})
    await _client(hass, "s3cret").async_health()
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer s3cret"


async def test_availability_requires_connection_and_registration() -> None:
    """Both halves matter: a reachable sidecar whose account has dropped cannot page."""
    client = PbxPageClient.__new__(PbxPageClient)
    client.connected = True
    client.accounts = {"a": {"registered": True}}
    assert client.available

    client.accounts = {"a": {"registered": False}}
    assert not client.available

    client.accounts = {"a": {"registered": True}}
    client.connected = False
    assert not client.available

    client.accounts = {}
    client.connected = True
    assert not client.available


async def test_losing_the_connection_drops_registration_claims(
    hass: HomeAssistant,
) -> None:
    """Do not keep claiming a registration we can no longer observe."""
    client = _client(hass)
    client.accounts = {"9901": {"account_id": "9901", "registered": True}}
    client.connected = True

    seen: list[dict] = []
    client.add_listener(seen.append)
    client._set_connected(False)

    assert not client.accounts["9901"]["registered"]
    assert seen == [{"type": "connection", "connected": False}]


async def test_health_records_the_sidecar_version(
    hass: HomeAssistant, aioclient_mock
) -> None:
    """The integration compares this with its own, so a half-finished upgrade
    shows up in the log rather than as unexplained behaviour."""
    aioclient_mock.get(f"{URL}/health", json={"status": "ok", "accounts": [], "version": "9.9.9"})
    client = _client(hass)
    client._apply_health(await client.async_health())
    assert client.sidecar_version == "9.9.9"
