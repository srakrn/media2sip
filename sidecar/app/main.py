"""Control API: the only thing Home Assistant talks to.

HTTP for commands, a websocket for events. MQTT is a plausible second transport
(a broker is already running on this network) but HTTP keeps the sidecar
deployable with no external dependency, which matters more for an announcement
system than saving a connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import config as config_module
from . import events as ev
from .media import MediaError, MediaResolver
from .models import AccountHealth, CallRequest, CallResponse, Health
from .sip import SipError, SipWorker

# Stamped in at image build time (Dockerfile ARG -> ENV), never written here.
# A literal in the source is one more thing to forget on release day, and a stale
# one is worse than none: the integration compares versions to spot a
# half-finished upgrade, and would be comparing against a lie.
#
# "dev" is honest for a build nobody stamped - a working tree, a local
# `docker compose build` - and the integration knows not to cry mismatch at it.
VERSION = os.environ.get("APP_VERSION", "dev")

_LOGGER = logging.getLogger(__name__)


class Sidecar:
    """Wires the three moving parts together and owns their lifecycle."""

    def __init__(self) -> None:
        self._config: config_module.Config | None = None
        self.bus: ev.EventBus | None = None
        self.sip: SipWorker | None = None
        self.media: MediaResolver | None = None

    @property
    def config(self) -> config_module.Config:
        """Loaded on first use, not at import.

        Importing a module should not be able to fail because an environment
        variable is missing - that turns a configuration mistake into an import
        traceback, and makes the app untestable without a full environment.
        """
        if self._config is None:
            self._config = config_module.load()
        return self._config

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.bus = ev.EventBus(loop)

        self.media = MediaResolver(
            cache_dir=self.config.cache_dir,
            sounds_dir=self.config.sounds_dir,
            sample_rate=8000,
            cache_max_bytes=self.config.cache_max_bytes,
            builtin_dir=self.config.builtin_sounds_dir,
        )
        # Fail loudly at startup rather than silently at page time.
        await MediaResolver.assert_ffmpeg()

        self.sip = SipWorker(self.config, self.bus, loop)
        await self.sip.start()

    async def stop(self) -> None:
        if self.sip is not None:
            await self.sip.stop()


sidecar = Sidecar()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await sidecar.start()
    try:
        yield
    finally:
        await sidecar.stop()


app = FastAPI(title="pbx-page sidecar", version=VERSION, lifespan=lifespan)


async def require_token(request: Request) -> None:
    """Optional bearer auth. Empty API_TOKEN means an open API, which is only
    acceptable on a private network - so say so at startup, not here."""
    expected = sidecar.config.api_token
    if not expected:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


@app.exception_handler(MediaError)
async def _media_error(request: Request, exc: MediaError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc), "kind": "media"})


@app.exception_handler(SipError)
async def _sip_error(request: Request, exc: SipError) -> JSONResponse:
    status = 503 if exc.code in (0, 503) else 409
    return JSONResponse(
        status_code=status, content={"detail": str(exc), "kind": "sip", "sip_code": exc.code}
    )


@app.get("/health", response_model=Health)
async def health() -> Health:
    accounts = [AccountHealth(**a) for a in sidecar.sip.registration_state().values()]
    return Health(
        status="ok" if accounts and all(a.registered for a in accounts) else "degraded",
        version=VERSION,
        accounts=accounts,
        active_calls=sidecar.sip.active_calls(),
        sounds=sidecar.media.list_sounds(),
        lead_in=sidecar.config.lead_in,
        codecs=sidecar.config.codecs,
    )


@app.post("/call", response_model=CallResponse, dependencies=[Depends(require_token)])
async def place_call(request: CallRequest) -> CallResponse:
    if not request.media and not request.chime:
        raise HTTPException(status_code=400, detail="one of media or chime is required")

    # Resolve media before touching the SIP stack. A missing sound or an
    # unreachable TTS URL should fail as a 400, not as a call the handsets
    # answer to silence.
    clips = []
    if request.chime:
        clips.append(await sidecar.media.resolve(request.chime))
    if request.media:
        clips.append(await sidecar.media.resolve(request.media, request.headers))
    clip = await sidecar.media.concat(clips)

    preempted: list[str] = []
    in_flight = [c for c in sidecar.sip.active_calls() if c["target"] == request.target]
    if in_flight:
        if request.policy == "reject":
            raise HTTPException(
                status_code=409,
                detail=f"{request.target} already has a call in flight",
                headers={"X-Call-Id": in_flight[0]["call_id"]},
            )
        preempted = await sidecar.sip.hangup_target(request.target)
        _LOGGER.info("preempted %s on %s", preempted, request.target)

    call_id = uuid.uuid4().hex[:12]
    result = await sidecar.sip.place_call(
        call_id=call_id,
        target=request.target,
        clip=clip.path,
        duration=clip.duration,
        lead_in=request.lead_in,
        account_id=request.account_id,
        media_label=clip.label,
    )
    return CallResponse(
        **{k: result[k] for k in ("call_id", "target", "account_id", "state", "duration", "lead_in")},
        media=clip.source,
        cached=clip.cached,
        preempted=preempted,
    )


@app.delete("/call/{call_id}", dependencies=[Depends(require_token)])
async def hangup(call_id: str) -> dict:
    return await sidecar.sip.hangup(call_id)


@app.get("/calls", dependencies=[Depends(require_token)])
async def calls() -> list[dict]:
    return sidecar.sip.active_calls()


@app.get("/calls/history", dependencies=[Depends(require_token)])
async def call_history(limit: int = 20) -> list[dict]:
    """Recent finished calls: target, SIP reason code, latency, and whether any
    RTP actually went down the wire. This is what explains a failed page."""
    return sidecar.sip.history(limit)


@app.get("/sounds", dependencies=[Depends(require_token)])
async def sounds() -> dict:
    return {"sounds": sidecar.media.list_sounds()}


@app.get("/events/recent", dependencies=[Depends(require_token)])
async def recent_events(limit: int = 20) -> list[dict]:
    """Lets a reconnecting client see what it missed rather than guessing."""
    return sidecar.bus.recent(limit)


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    expected = sidecar.config.api_token
    if expected:
        supplied = ws.query_params.get("token") or ""
        header = ws.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:]
        if not secrets.compare_digest(supplied, expected):
            await ws.close(code=4401)
            return

    await ws.accept()
    queue = sidecar.bus.subscribe()
    try:
        # Registration state up front, so a freshly connected integration knows
        # whether the entity is available without waiting for a state change.
        await ws.send_json({"type": "hello", "version": VERSION,
                            "accounts": list(sidecar.sip.registration_state().values())})
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sidecar.bus.unsubscribe(queue)


def main() -> None:
    import uvicorn

    cfg = sidecar.config
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    if not cfg.api_token:
        _LOGGER.warning("API_TOKEN is not set - the control API is unauthenticated")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
