"""Version pairing is enforced at runtime, not just in CI."""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pbx_page import _async_warn_on_version_mismatch


async def test_mismatch_is_warned_about(
    hass: HomeAssistant, enable_custom_integrations, caplog
) -> None:
    with caplog.at_level(logging.WARNING):
        await _async_warn_on_version_mismatch(hass, "9.9.9")
    assert "version mismatch" in caplog.text
    assert "9.9.9" in caplog.text


async def test_matching_versions_are_quiet(
    hass: HomeAssistant, enable_custom_integrations, caplog
) -> None:
    with caplog.at_level(logging.WARNING):
        await _async_warn_on_version_mismatch(hass, "0.1.0")
    assert "version mismatch" not in caplog.text


async def test_unknown_sidecar_version_is_not_an_alarm(
    hass: HomeAssistant, enable_custom_integrations, caplog
) -> None:
    """An older sidecar that does not report a version must not cry wolf."""
    with caplog.at_level(logging.WARNING):
        await _async_warn_on_version_mismatch(hass, None)
    assert "version mismatch" not in caplog.text
