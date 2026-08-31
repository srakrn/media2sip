"""Sidecar configuration.

Two deployment shapes have to work from one codebase:

* **Docker** — everything comes from environment variables.
* **Home Assistant add-on** — the supervisor writes `/data/options.json`, and the
  add-on's `run.sh` does not have to translate it, because we read it here.

Add-on options win over environment variables when both are present, since the
options file is the thing the user edited most recently in the HA UI.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

ADDON_OPTIONS = Path("/data/options.json")


def _load_addon_options() -> dict:
    if not ADDON_OPTIONS.is_file():
        return {}
    try:
        data = json.loads(ADDON_OPTIONS.read_text())
    except (OSError, ValueError) as err:
        _LOGGER.warning("ignoring unreadable %s: %s", ADDON_OPTIONS, err)
        return {}
    if not isinstance(data, dict):
        _LOGGER.warning("ignoring %s: expected an object", ADDON_OPTIONS)
        return {}
    _LOGGER.info("loaded add-on options from %s", ADDON_OPTIONS)
    return data


_OPTIONS = _load_addon_options()
_UNSET = object()


def _get(key: str, default=_UNSET, cast=str):
    """Read `key` from add-on options, else the environment, else `default`.

    The default goes through `cast` too. These settings chain - SIP_PORT falls
    back to PBX_PORT, which falls back to a literal - and a default that skipped
    the cast would hand back a string where an int was declared.
    """
    if key.lower() in _OPTIONS:
        raw = _OPTIONS[key.lower()]
    elif key in os.environ and os.environ[key] != "":
        raw = os.environ[key]
    elif default is not _UNSET:
        raw = default
    else:
        raise RuntimeError(f"required setting {key} is not set")
    if raw is None:
        return None
    if cast is bool:
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return cast(raw)


@dataclass(frozen=True)
class SipAccount:
    """One registration against one PBX."""

    id: str
    username: str
    password: str
    host: str
    port: int = 5060
    transport: str = "udp"
    register_expires: int = 300

    # Advertised in Contact and SDP. Set this when the sidecar is behind NAT
    # (a bridged container) and the PBX endpoint does not do symmetric RTP;
    # leave empty when running with host networking, which is the normal case.
    public_address: str = ""

    @property
    def uri(self) -> str:
        return f"sip:{self.username}@{self.host}"

    @property
    def registrar(self) -> str:
        return f"sip:{self.host}:{self.port};transport={self.transport}"


@dataclass(frozen=True)
class Config:
    accounts: list[SipAccount]

    host: str = "0.0.0.0"
    port: int = 8080

    sip_port: int = 5060
    rtp_port_start: int = 20000
    rtp_port_count: int = 100

    # Phase 1 measured PCMU 8 kHz mono against the FreePBX page group, and no
    # G.722 on offer. Narrowband is the whole story here, so the offer is pinned
    # rather than left to pjsua2's default priority order.
    codecs: list[str] = field(default_factory=lambda: ["PCMU/8000/1", "PCMA/8000/1"])

    # Auto-answering handsets need a moment to open the audio path. Without this
    # the first word is clipped. Phase 1 confirmed it is not optional.
    lead_in: float = 1.0

    # Guard rails. Paging clips are seconds long; anything past these is a fault.
    max_call_seconds: float = 60.0
    answer_timeout: float = 20.0

    cache_dir: Path = Path("/data/cache")
    sounds_dir: Path = Path("/data/sounds")
    # Shipped in the image, outside /data so a Home Assistant add-on's persistent
    # volume cannot shadow it.
    builtin_sounds_dir: Path = Path("/opt/pbx-page/sounds")
    cache_max_bytes: int = 256 * 1024 * 1024

    log_level: str = "INFO"
    sip_log_level: int = 2

    # Optional bearer token for the control API. Empty means no auth, which is
    # only acceptable on a private network.
    api_token: str = ""


def _accounts_from_env() -> list[SipAccount]:
    """Build the account list.

    Supports the single-account shape (SIP_USERNAME/SIP_PASSWORD/...) that covers
    the overwhelmingly common case, and a JSON list in SIP_ACCOUNTS for multiple
    PBXs. The plan calls for one account per configured PBX, not per target.
    """
    raw = _get("SIP_ACCOUNTS", "")
    if raw:
        entries = raw if isinstance(raw, list) else json.loads(raw)
        return [
            SipAccount(
                id=str(e.get("id") or e["username"]),
                username=str(e["username"]),
                password=str(e["password"]),
                host=str(e["host"]),
                port=int(e.get("port", 5060)),
                transport=str(e.get("transport", "udp")),
                register_expires=int(e.get("register_expires", 300)),
                public_address=str(e.get("public_address", "")),
            )
            for e in entries
        ]

    username = _get("SIP_USERNAME", _get("SIP_EXTENSION", ""))
    if not username:
        raise RuntimeError("set SIP_USERNAME (or SIP_EXTENSION), or SIP_ACCOUNTS")
    return [
        SipAccount(
            id=_get("SIP_ACCOUNT_ID", username),
            username=username,
            password=_get("SIP_PASSWORD", _get("SIP_SECRET", "")),
            host=_get("SIP_HOST", _get("PBX_HOST", "")),
            port=_get("SIP_PORT", _get("PBX_PORT", 5060), int),
            transport=_get("SIP_TRANSPORT", "udp"),
            register_expires=_get("SIP_REGISTER_EXPIRES", 300, int),
            public_address=_get("SIP_PUBLIC_ADDRESS", ""),
        )
    ]


def load() -> Config:
    codecs = _get("SIP_CODECS", "")
    return Config(
        accounts=_accounts_from_env(),
        host=_get("SIDECAR_HOST", "0.0.0.0"),
        port=_get("SIDECAR_PORT", 8080, int),
        sip_port=_get("SIP_LOCAL_PORT", 5060, int),
        rtp_port_start=_get("RTP_PORT_START", 20000, int),
        rtp_port_count=_get("RTP_PORT_COUNT", 100, int),
        codecs=[c.strip() for c in codecs.split(",") if c.strip()] if codecs else Config.__dataclass_fields__["codecs"].default_factory(),
        lead_in=_get("LEAD_IN", 1.0, float),
        max_call_seconds=_get("MAX_CALL_SECONDS", 60.0, float),
        answer_timeout=_get("ANSWER_TIMEOUT", 20.0, float),
        cache_dir=Path(_get("CACHE_DIR", "/data/cache")),
        sounds_dir=Path(_get("SOUNDS_DIR", "/data/sounds")),
        builtin_sounds_dir=Path(_get("BUILTIN_SOUNDS_DIR", "/opt/pbx-page/sounds")),
        cache_max_bytes=_get("CACHE_MAX_BYTES", 256 * 1024 * 1024, int),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        sip_log_level=_get("SIP_LOG_LEVEL", 2, int),
        api_token=_get("API_TOKEN", ""),
    )
