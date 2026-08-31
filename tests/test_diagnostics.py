"""Diagnostics must explain a failure without leaking a credential."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.pbx_page.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_the_token(
    hass: HomeAssistant, setup_integration
) -> None:
    hass.config_entries.async_update_entry(
        setup_integration,
        data={**setup_integration.data, "token": "super-secret"},
    )
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert "super-secret" not in str(diagnostics)
    assert diagnostics["entry"]["data"]["token"] == "**REDACTED**"
    assert diagnostics["connection"]["available"] is True
    assert diagnostics["accounts"]["9901"]["registered"] is True


async def test_diagnostics_reports_an_unreachable_sidecar(
    hass: HomeAssistant, setup_integration, sidecar
) -> None:
    from custom_components.pbx_page.client import SidecarError

    async def boom() -> None:
        raise SidecarError("connection refused")

    sidecar.async_health = boom
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert "connection refused" in diagnostics["health"]["error"]


async def test_diagnostics_includes_recent_calls(
    hass: HomeAssistant, setup_integration
) -> None:
    """Target, SIP reason code and latency are what explain a failed page."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    call = diagnostics["recent_calls"][0]
    assert call["target"] == "991"
    assert call["sip_code"] == 200
    assert call["answer_latency"] == 0.2
    assert call["audio_sent"] is True


async def test_diagnostics_carries_no_tts_url(
    hass: HomeAssistant, setup_integration
) -> None:
    """A TTS proxy URL carries a token that grants access to the audio, and
    diagnostics downloads get shared around."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    blob = str(diagnostics)
    assert "tts_proxy" not in blob
    assert "http://" not in diagnostics["recent_calls"][0]["media"]


async def test_diagnostics_includes_entity_state(
    hass: HomeAssistant, setup_integration
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    entities = {e["entity_id"]: e for e in diagnostics["entities"]}
    assert entities["media_player.working_zone"]["extension"] == "991"
    assert entities["media_player.working_zone"]["available"] is True


async def test_diagnostics_reports_both_versions(
    hass: HomeAssistant, setup_integration, sidecar, integration_version
) -> None:
    """A mismatch should be obvious in a bug report without being asked for."""
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert diagnostics["versions"]["integration"] == integration_version
    assert diagnostics["versions"]["sidecar"] == "test"
