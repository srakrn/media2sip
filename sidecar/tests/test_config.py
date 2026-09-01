"""Configuration: one image has to serve both deployment shapes."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _load(monkeypatch, env: dict[str, str], options: dict | None, tmp_path: Path):
    """Reload the config module with a given environment and options file."""
    from app import config as config_module

    for key in list(config_module.os.environ):
        if key.startswith(("SIP_", "PBX_", "SIDECAR_", "RTP_", "LEAD_", "API_", "LOG_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    options_path = tmp_path / "options.json"
    if options is not None:
        options_path.write_text(json.dumps(options))
    monkeypatch.setattr(config_module, "ADDON_OPTIONS", options_path)

    reloaded = importlib.reload(config_module)
    monkeypatch.setattr(reloaded, "ADDON_OPTIONS", options_path)
    reloaded._OPTIONS = reloaded._load_addon_options()
    return reloaded


def test_docker_shape_reads_the_environment(monkeypatch, tmp_path) -> None:
    cfg = _load(monkeypatch, {
        "SIP_USERNAME": "9901", "SIP_PASSWORD": "secret", "SIP_HOST": "10.1.2.99",
    }, None, tmp_path).load()

    account = cfg.accounts[0]
    assert (account.username, account.password, account.host) == ("9901", "secret", "10.1.2.99")
    assert account.uri == "sip:9901@10.1.2.99"


def test_legacy_env_names_still_work(monkeypatch, tmp_path) -> None:
    """SIP_EXTENSION/SIP_SECRET/PBX_HOST are the original env var names."""
    cfg = _load(monkeypatch, {
        "SIP_EXTENSION": "9901", "SIP_SECRET": "secret", "PBX_HOST": "10.1.2.99",
        "PBX_PORT": "5060",
    }, None, tmp_path).load()
    assert cfg.accounts[0].username == "9901"
    assert cfg.accounts[0].port == 5060


def test_addon_options_win_over_environment(monkeypatch, tmp_path) -> None:
    """The options file is what the user edited most recently in the HA UI."""
    cfg = _load(
        monkeypatch,
        {"SIP_USERNAME": "from-env", "SIP_PASSWORD": "x", "SIP_HOST": "h"},
        {"sip_username": "from-options", "sip_password": "y", "sip_host": "pbx.local",
         "lead_in": 2.5},
        tmp_path,
    ).load()
    assert cfg.accounts[0].username == "from-options"
    assert cfg.accounts[0].host == "pbx.local"
    assert cfg.lead_in == 2.5


def test_missing_credentials_fail_loudly(monkeypatch, tmp_path) -> None:
    module = _load(monkeypatch, {}, None, tmp_path)
    with pytest.raises(RuntimeError, match="SIP_USERNAME"):
        module.load()


def test_multiple_accounts_from_json(monkeypatch, tmp_path) -> None:
    """One account per configured PBX. Targets are not accounts."""
    cfg = _load(monkeypatch, {
        "SIP_ACCOUNTS": json.dumps([
            {"id": "main", "username": "9901", "password": "a", "host": "pbx1"},
            {"id": "annex", "username": "9902", "password": "b", "host": "pbx2", "port": 5070},
        ])
    }, None, tmp_path).load()

    assert [a.id for a in cfg.accounts] == ["main", "annex"]
    assert cfg.accounts[1].port == 5070


def test_codecs_are_pinned_by_default(monkeypatch, tmp_path) -> None:
    cfg = _load(monkeypatch, {
        "SIP_USERNAME": "9901", "SIP_PASSWORD": "x", "SIP_HOST": "h"
    }, None, tmp_path).load()
    assert cfg.codecs == ["PCMU/8000/1", "PCMA/8000/1"]


def test_unreadable_options_file_is_ignored(monkeypatch, tmp_path) -> None:
    """A corrupt options file must not take paging down; the environment stands in."""
    options_path = tmp_path / "options.json"
    options_path.write_text("{not json")
    from app import config as config_module

    monkeypatch.setattr(config_module, "ADDON_OPTIONS", options_path)
    assert config_module._load_addon_options() == {}


def test_startup_fails_loudly_without_credentials(monkeypatch, tmp_path) -> None:
    """The message has to name the setting, or the operator is left guessing."""
    module = _load(monkeypatch, {}, None, tmp_path)
    with pytest.raises(RuntimeError) as err:
        module.load()
    assert "SIP_USERNAME" in str(err.value)
    assert "SIP_ACCOUNTS" in str(err.value)
