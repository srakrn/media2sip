"""The SIP user agent.

pjsua2 is a C++ library with its own threading, and mixing that with asyncio is
where this kind of code usually goes wrong. The rule here is deliberately strict:

    **Every pjsua2 call and every pjsua2 callback happens on one thread.**

The endpoint is created with `threadCnt = 0`, so pjsua2 spawns no workers of its
own; a single `SipWorker` thread pumps `libHandleEvents()` and drains a command
queue between pumps. Nothing else ever touches the library, which means no
`libRegisterThread`, no locks around the stack, and no callback arriving on a
thread that is halfway through an API call.

Results travel back to asyncio through futures resolved with
`call_soon_threadsafe`; events travel back through the event bus.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections import deque
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pjsua2 as pj

from . import events as ev
from .config import Config, SipAccount

_LOGGER = logging.getLogger(__name__)

# How long the SIP thread blocks in libHandleEvents before servicing timers and
# the command queue. Small enough that the lead-in lands accurately, large enough
# not to spin a core.
_PUMP_MS = 20

# Played audio is followed by this much slack before the call is torn down, so a
# clip is never clipped by its own hangup when onEof2 does not arrive first.
_PLAYBACK_TAIL = 0.35

# How many finished calls to keep for diagnostics.
HISTORY_DEPTH = 20


class SipError(Exception):
    """A call could not be placed. Carries the SIP status code where there is one."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class CallSession:
    """One page in flight."""

    call_id: str
    target: str
    account_id: str
    clip: Path
    duration: float
    lead_in: float
    # A label for the media, safe to log. Never the raw URL: a Home Assistant TTS
    # proxy URL carries a token that grants access to the audio.
    media_label: str = ""

    call: Any = None
    player: Any = None
    audio_media: Any = None

    state: str = "new"
    media_ready: bool = False
    playback_started: bool = False
    confirmed_at: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)

    playback_deadline: float = 0.0   # when to start playing (confirmed + lead-in)
    hangup_deadline: float = 0.0     # when to tear down after playback
    answer_deadline: float = 0.0     # when to give up waiting for an answer

    playback_at: float = 0.0        # first playback, for the history record
    playback_origin: float = 0.0    # shifted by pauses, for the hangup deadline
    played: float = 0.0             # seconds heard so far, frozen while paused
    paused: bool = False
    rtp: dict[str, int] = field(default_factory=dict)

    sip_code: int = 0
    sip_reason: str = ""
    # Why *we* ended the call, as opposed to the SIP-level cause. Set at teardown
    # and preferred over the generic disconnect reason, otherwise an answer
    # timeout, a preemption and a normal hangup all report the same thing and the
    # diagnostics cannot explain a failed page.
    end_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "target": self.target,
            "account_id": self.account_id,
            "state": self.state,
            "duration": round(self.duration, 3),
            "lead_in": self.lead_in,
            "elapsed": round(time.monotonic() - self.started_at, 3),
            "sip_code": self.sip_code,
            "sip_reason": self.sip_reason,
            "paused": self.paused,
            "media": self.media_label,
        }

    def as_history(self) -> dict:
        """What a diagnostics download needs to explain this call.

        Latency is split rather than totalled: answer latency is the PBX's, and
        playback latency is ours plus the lead-in. A page that feels slow is one
        or the other, and the totals cannot tell you which.
        """
        now = time.monotonic()
        return {
            "call_id": self.call_id,
            "target": self.target,
            "account_id": self.account_id,
            "media": self.media_label,
            "at": self.started_wall,
            "clip_duration": round(self.duration, 3),
            "lead_in": self.lead_in,
            "answer_latency": round(self.confirmed_at - self.started_at, 3) if self.confirmed_at else None,
            "playback_latency": round(self.playback_at - self.started_at, 3) if self.playback_at else None,
            "total": round(now - self.started_at, 3),
            "sip_code": self.sip_code,
            "sip_reason": self.sip_reason,
            "end_reason": self.end_reason,
            "rtp": dict(self.rtp),
            # The single most useful field here. A call that sent no RTP looks
            # identical to a working one from the SIP dialog alone.
            "audio_sent": bool(self.rtp.get("tx_pkt")),
        }


class _Player(pj.AudioMediaPlayer):
    """AudioMediaPlayer that reports end-of-file.

    Phase 1 found baresip's file source gives no completion signal, leaving the
    caller to stopwatch its way through playback. pjsua2 does emit `onEof2`, so
    the duration timer becomes a backstop rather than the mechanism.
    """

    def __init__(self, on_eof: Callable[[], None]) -> None:
        super().__init__()
        self._on_eof = on_eof

    def onEof2(self) -> None:  # noqa: N802 - pjsua2 callback name
        try:
            self._on_eof()
        except Exception:
            _LOGGER.exception("error handling end of playback")


class _Call(pj.Call):
    def __init__(self, account: Any, worker: "SipWorker", session: CallSession) -> None:
        super().__init__(account)
        self._worker = worker
        self._session = session

    def onCallState(self, prm) -> None:  # noqa: N802
        try:
            self._worker._on_call_state(self._session, self.getInfo())
        except Exception:
            _LOGGER.exception("error in onCallState")

    def onCallMediaState(self, prm) -> None:  # noqa: N802
        try:
            self._worker._on_call_media_state(self._session, self.getInfo())
        except Exception:
            _LOGGER.exception("error in onCallMediaState")


class _Account(pj.Account):
    def __init__(self, worker: "SipWorker", account: SipAccount) -> None:
        super().__init__()
        self._worker = worker
        self._account = account

    def onRegState(self, prm) -> None:  # noqa: N802
        try:
            self._worker._on_reg_state(self._account, prm)
        except Exception:
            _LOGGER.exception("error in onRegState")


class SipWorker:
    """Owns the pjsua2 endpoint on its own thread."""

    def __init__(self, config: Config, bus: ev.EventBus, loop: asyncio.AbstractEventLoop) -> None:
        self.config = config
        self.bus = bus
        self.loop = loop

        self._commands: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="sip", daemon=True)
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._stopping = threading.Event()

        self._ep: Any = None
        self._accounts: dict[str, Any] = {}
        self._registration: dict[str, dict] = {}
        self._sessions: dict[str, CallSession] = {}
        # Bounded: this is a debugging aid, not a call detail record.
        self._history: deque[dict] = deque(maxlen=HISTORY_DEPTH)

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._thread.start()
        await asyncio.get_running_loop().run_in_executor(None, self._ready.wait)
        if self._startup_error is not None:
            raise self._startup_error

    async def stop(self) -> None:
        self._stopping.set()
        self._commands.put((None, None, None))
        await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 10)

    # -- public API (asyncio side) ---------------------------------------

    async def place_call(
        self, call_id: str, target: str, clip: Path, duration: float,
        lead_in: float | None = None, account_id: str | None = None,
        media_label: str = "",
    ) -> dict:
        return await self._submit(
            "place_call",
            call_id=call_id, target=target, clip=clip, duration=duration,
            lead_in=self.config.lead_in if lead_in is None else lead_in,
            account_id=account_id, media_label=media_label,
        )

    async def hangup(self, call_id: str) -> dict:
        return await self._submit("hangup", call_id=call_id)

    async def replace_media(
        self, call_id: str, clip: Path, duration: float, media_label: str = ""
    ) -> dict:
        return await self._submit(
            "replace_media", call_id=call_id, clip=clip,
            duration=duration, media_label=media_label,
        )

    async def set_paused(self, call_id: str, paused: bool) -> dict:
        return await self._submit("set_paused", call_id=call_id, paused=paused)

    async def hangup_target(self, target: str) -> list[str]:
        return await self._submit("hangup_target", target=target)

    def registration_state(self) -> dict[str, dict]:
        """Read-only snapshot; safe to read from asyncio without the queue."""
        return dict(self._registration)

    def active_calls(self) -> list[dict]:
        return [s.as_dict() for s in list(self._sessions.values())]

    def history(self, limit: int = HISTORY_DEPTH) -> list[dict]:
        """Most recent finished calls, newest last."""
        return list(self._history)[-limit:]

    async def _submit(self, name: str, **kwargs) -> Any:
        future: asyncio.Future = self.loop.create_future()
        self._commands.put((name, kwargs, future))
        return await future

    # -- SIP thread ------------------------------------------------------

    def _run(self) -> None:
        try:
            self._init_endpoint()
        except Exception as err:  # startup failure must reach start()
            self._startup_error = err
            self._ready.set()
            return
        self._ready.set()

        try:
            while not self._stopping.is_set():
                self._ep.libHandleEvents(_PUMP_MS)
                self._drain_commands()
                self._service_timers()
        except Exception:
            _LOGGER.exception("SIP worker loop died")
        finally:
            self._shutdown_endpoint()

    def _init_endpoint(self) -> None:
        ep = pj.Endpoint()
        ep.libCreate()

        ep_cfg = pj.EpConfig()
        # threadCnt = 0 is the whole concurrency design: pjsua2 creates no worker
        # threads, so this thread is the only one inside the library.
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.userAgent = "media2sip"
        ep_cfg.logConfig.level = self.config.sip_log_level
        ep_cfg.logConfig.consoleLevel = self.config.sip_log_level
        # Narrowband end to end; no echo canceller and no VAD, because there is
        # no microphone and a page must not be voice-gated into silence.
        ep_cfg.medConfig.clockRate = 8000
        ep_cfg.medConfig.sndClockRate = 8000
        ep_cfg.medConfig.channelCount = 1
        ep_cfg.medConfig.noVad = True
        ep_cfg.medConfig.ecTailLen = 0
        ep.libInit(ep_cfg)

        transport = pj.TransportConfig()
        transport.port = self.config.sip_port
        transport.portRange = 10
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport)

        ep.libStart()
        # A container has no sound card. Without this pjsua2 tries to open one
        # and the whole media subsystem fails at the first call.
        ep.audDevManager().setNullDev()

        self._ep = ep
        self._pin_codecs()
        self._create_accounts()
        _LOGGER.info("pjsua2 %s started, SIP on udp/%s", ep.libVersion().full, self.config.sip_port)

    def _pin_codecs(self) -> None:
        """Offer exactly what the page group negotiates, in our order.

        Phase 1 measured PCMU 8 kHz against the FreePBX page group. Leaving the
        default priority list in place means offering codecs the PBX will never
        pick, which only widens the surface for a one-way-audio bug.
        """
        wanted = {c.upper(): len(self.config.codecs) - i for i, c in enumerate(self.config.codecs)}
        for info in self._ep.codecEnum2():
            codec_id = info.codecId.upper()
            priority = 0
            for name, rank in wanted.items():
                if codec_id == name or codec_id.startswith(name.split("/")[0] + "/"):
                    if codec_id == name or name.count("/") < 2:
                        priority = 128 + rank
                    break
            self._ep.codecSetPriority(info.codecId, priority)
        enabled = [(i.codecId, i.priority) for i in self._ep.codecEnum2() if i.priority > 0]
        _LOGGER.info("codecs offered: %s", enabled)

    def _create_accounts(self) -> None:
        for account in self.config.accounts:
            cfg = pj.AccountConfig()
            cfg.idUri = account.uri
            cfg.regConfig.registrarUri = account.registrar
            cfg.regConfig.timeoutSec = account.register_expires
            cfg.regConfig.retryIntervalSec = 30
            cfg.regConfig.firstRetryIntervalSec = 8
            # Give up re-REGISTERing only when the PBX is truly gone; the entity
            # goes unavailable long before this matters.
            cfg.regConfig.randomRetryIntervalSec = 10

            cred = pj.AuthCredInfo("digest", "*", account.username, 0, account.password)
            cfg.sipConfig.authCreds.append(cred)

            # Let the PBX tell us how it sees us. FreePBX sets rport/received and
            # rtp_symmetric by default, which is what makes a bridged container
            # work without an explicit advertised address.
            cfg.natConfig.contactRewriteUse = True
            cfg.natConfig.viaRewriteUse = True
            cfg.natConfig.sdpNatRewriteUse = True
            cfg.natConfig.contactUseSrcPort = True

            media_transport = pj.TransportConfig()
            media_transport.port = self.config.rtp_port_start
            media_transport.portRange = self.config.rtp_port_count
            if account.public_address:
                # Escape hatch for a bridged container whose PBX does not do
                # symmetric RTP: advertise a reachable address in SDP and Contact.
                media_transport.publicAddress = account.public_address
            cfg.mediaConfig.transportConfig = media_transport
            cfg.mediaConfig.lockCodecEnabled = True
            cfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_DISABLED

            acc = _Account(self, account)
            acc.create(cfg)
            self._accounts[account.id] = acc
            self._registration[account.id] = {
                "account_id": account.id,
                "uri": account.uri,
                "registered": False,
                "code": 0,
                "reason": "registering",
                "since": time.time(),
            }
            _LOGGER.info("account %s registering as %s", account.id, account.uri)

    def _shutdown_endpoint(self) -> None:
        if self._ep is None:
            return
        for session in list(self._sessions.values()):
            self._teardown(session, "shutdown")
        try:
            self._accounts.clear()
            self._ep.libDestroy()
        except Exception:
            _LOGGER.exception("error shutting down pjsua2")
        self._ep = None

    # -- commands --------------------------------------------------------

    def _drain_commands(self) -> None:
        while True:
            try:
                name, kwargs, future = self._commands.get_nowait()
            except queue.Empty:
                return
            if name is None:
                return
            try:
                result = getattr(self, f"_cmd_{name}")(**kwargs)
                self._resolve(future, result)
            except Exception as err:
                self._reject(future, err)

    def _resolve(self, future: asyncio.Future, result: Any) -> None:
        self.loop.call_soon_threadsafe(lambda: None if future.done() else future.set_result(result))

    def _reject(self, future: asyncio.Future, err: Exception) -> None:
        self.loop.call_soon_threadsafe(lambda: None if future.done() else future.set_exception(err))

    def _cmd_place_call(
        self, call_id: str, target: str, clip: Path, duration: float,
        lead_in: float, account_id: str | None, media_label: str = "",
    ) -> dict:
        account_id = account_id or next(iter(self._accounts))
        acc = self._accounts.get(account_id)
        if acc is None:
            raise SipError(f"no such account: {account_id}")
        reg = self._registration.get(account_id, {})
        if not reg.get("registered"):
            raise SipError(f"account {account_id} is not registered ({reg.get('reason')})", 503)

        session = CallSession(
            call_id=call_id, target=target, account_id=account_id,
            clip=clip, duration=duration, lead_in=lead_in,
            media_label=media_label or clip.stem,
        )
        session.answer_deadline = time.monotonic() + self.config.answer_timeout

        host = next(a.host for a in self.config.accounts if a.id == account_id)
        uri = target if target.startswith("sip:") else f"sip:{target}@{host}"

        call = _Call(acc, self, session)
        session.call = call
        self._sessions[call_id] = session
        self._publish(ev.CALLING, session, uri=uri)

        try:
            call.makeCall(uri, pj.CallOpParam(True))
        except pj.Error as err:
            self._sessions.pop(call_id, None)
            self._publish(ev.DISCONNECTED, session, reason="make_call_failed", error=str(err))
            raise SipError(f"cannot place call to {uri}: {err.info()}") from err
        return session.as_dict()

    def _cmd_hangup(self, call_id: str) -> dict:
        session = self._sessions.get(call_id)
        if session is None:
            raise SipError(f"no such call: {call_id}", 404)
        self._teardown(session, "requested")
        return session.as_dict()

    def _cmd_replace_media(
        self, call_id: str, clip: Path, duration: float, media_label: str
    ) -> dict:
        """Swap the audio on a call that is already up.

        Not a new call. The handsets have already answered and the audio path is
        open, so there is nothing to re-dial and **no lead-in to wait out** - the
        new clip starts on the next frame. Re-originating instead would drop the
        page group and make it answer again, which is audible.
        """
        session = self._sessions.get(call_id)
        if session is None:
            raise SipError(f"no such call: {call_id}", 404)

        self._stop_player(session)
        session.clip = clip
        session.duration = duration
        session.media_label = media_label or clip.stem
        session.playback_started = False
        session.played = 0.0
        session.paused = False
        session.hangup_deadline = 0.0

        if session.media_ready:
            self._start_playback(session)
        else:
            # Media is not up yet; the existing lead-in timer will start it.
            session.playback_deadline = session.playback_deadline or (
                time.monotonic() + session.lead_in
            )
        return session.as_dict()

    def _cmd_set_paused(self, call_id: str, paused: bool) -> dict:
        """Pause by disconnecting the player from the call, not by stopping RTP.

        The conference bridge keeps sending silence, so the call stays up and the
        far end sees no media timeout. A port nobody pulls from does not advance,
        so the clip resumes where it stopped rather than skipping ahead.
        """
        session = self._sessions.get(call_id)
        if session is None:
            raise SipError(f"no such call: {call_id}", 404)
        if session.player is None or not session.playback_started:
            raise SipError(f"call {call_id} is not playing anything", 409)
        if session.paused == paused:
            return session.as_dict()

        now = time.monotonic()
        try:
            if paused:
                session.player.stopTransmit(session.audio_media)
                session.played = now - session.playback_origin
                session.paused = True
                # Suspend the playback timer; max_call_seconds still applies, so
                # a forgotten pause cannot hold the page group indefinitely.
                session.hangup_deadline = 0.0
                self._publish(ev.PLAYBACK_PAUSED, session)
            else:
                session.player.startTransmit(session.audio_media)
                session.paused = False
                session.playback_origin = now - session.played
                session.hangup_deadline = (
                    session.playback_origin + session.duration + _PLAYBACK_TAIL
                )
                self._publish(ev.PLAYBACK_RESUMED, session)
        except pj.Error as err:
            raise SipError(f"cannot {'pause' if paused else 'resume'}: {err.info()}") from err
        return session.as_dict()

    def _cmd_hangup_target(self, target: str) -> list[str]:
        hung = []
        for session in list(self._sessions.values()):
            if session.target == target:
                self._teardown(session, "preempted")
                hung.append(session.call_id)
        return hung

    # -- callbacks (SIP thread) ------------------------------------------

    def _on_reg_state(self, account: SipAccount, prm) -> None:
        registered = 200 <= prm.code < 300 and prm.expiration != 0
        previous = self._registration.get(account.id, {}).get("registered")
        self._registration[account.id] = {
            "account_id": account.id,
            "uri": account.uri,
            "registered": registered,
            "code": prm.code,
            "reason": prm.reason,
            "since": time.time(),
        }
        if registered != previous:
            self.bus.publish_threadsafe(
                ev.REGISTERED if registered else ev.UNREGISTERED,
                account_id=account.id, code=prm.code, reason=prm.reason,
            )
        if not registered and prm.code in (401, 403, 407):
            # Repeated auth failures are how a host gets IP-banned by FreePBX's
            # responsive firewall, which takes paging down silently. pjsua2 backs
            # off on its own; this is here so the cause is obvious in the log.
            _LOGGER.error(
                "account %s rejected with %s %s - check credentials before retrying, "
                "repeated auth failures can get this host firewalled by the PBX",
                account.id, prm.code, prm.reason,
            )

    def _on_call_state(self, session: CallSession, info) -> None:
        session.sip_code = info.lastStatusCode
        session.sip_reason = info.lastReason

        if info.state == pj.PJSIP_INV_STATE_EARLY:
            if session.state != ev.EARLY:
                session.state = ev.EARLY
                self._publish(ev.EARLY, session)
        elif info.state == pj.PJSIP_INV_STATE_CONFIRMED:
            if session.state != ev.CONFIRMED:
                session.state = ev.CONFIRMED
                session.confirmed_at = time.monotonic()
                session.answer_deadline = 0.0
                # The lead-in starts here, not at playback: auto-answering
                # handsets need a moment to open the audio path or the first
                # word is lost. Phase 1 confirmed this is not optional.
                session.playback_deadline = session.confirmed_at + session.lead_in
                self._publish(ev.CONFIRMED, session)
        elif info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._finish(session, "disconnected")

    def _on_call_media_state(self, session: CallSession, info) -> None:
        for media in info.media:
            if media.type == pj.PJMEDIA_TYPE_AUDIO and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                try:
                    session.audio_media = session.call.getAudioMedia(media.index)
                    session.media_ready = True
                except pj.Error:
                    _LOGGER.exception("cannot get audio media for call %s", session.call_id)
                return

    def _on_playback_eof(self, call_id: str) -> None:
        session = self._sessions.get(call_id)
        if session is None or not session.playback_started or session.paused:
            return
        # Do not tear down inside the callback; pjsua2 is still inside the player.
        session.hangup_deadline = min(
            session.hangup_deadline or float("inf"), time.monotonic() + _PLAYBACK_TAIL
        )

    # -- timers ----------------------------------------------------------

    def _service_timers(self) -> None:
        now = time.monotonic()
        for session in list(self._sessions.values()):
            if session.answer_deadline and now >= session.answer_deadline:
                _LOGGER.warning("call %s was never answered", session.call_id)
                self._teardown(session, "answer_timeout")
                continue
            if (
                not session.playback_started
                and session.media_ready
                and session.playback_deadline
                and now >= session.playback_deadline
            ):
                self._start_playback(session)
            if session.paused:
                # Only the hard cap applies while paused.
                if now - session.started_at > self.config.max_call_seconds:
                    _LOGGER.warning("call %s exceeded max_call_seconds while paused", session.call_id)
                    self._teardown(session, "max_duration")
                continue
            if session.hangup_deadline and now >= session.hangup_deadline:
                self._teardown(session, "playback_complete")
                continue
            if now - session.started_at > self.config.max_call_seconds:
                _LOGGER.warning("call %s exceeded max_call_seconds", session.call_id)
                self._teardown(session, "max_duration")

    def _start_playback(self, session: CallSession) -> None:
        try:
            player = _Player(lambda cid=session.call_id: self._on_playback_eof(cid))
            player.createPlayer(str(session.clip), pj.PJMEDIA_FILE_NO_LOOP)
            player.startTransmit(session.audio_media)
        except pj.Error as err:
            _LOGGER.error("playback failed for %s: %s", session.call_id, err.info())
            self._publish(ev.DISCONNECTED, session, reason="playback_failed", error=err.info())
            self._teardown(session, "playback_failed")
            return

        now = time.monotonic()
        session.player = player
        session.playback_started = True
        session.playback_origin = now
        session.played = 0.0
        session.paused = False
        if not session.playback_at:
            session.playback_at = now
        session.state = ev.PLAYBACK_STARTED
        # Backstop for onEof2. Normally the callback fires first and shortens this.
        session.hangup_deadline = time.monotonic() + session.duration + _PLAYBACK_TAIL
        self._publish(ev.PLAYBACK_STARTED, session)

    # -- teardown --------------------------------------------------------

    def _teardown(self, session: CallSession, reason: str) -> None:
        session.end_reason = session.end_reason or reason
        self._log_rtp(session)
        self._stop_player(session)
        call = session.call
        if call is not None:
            try:
                if call.isActive():
                    call.hangup(pj.CallOpParam(True))
                    return  # the DISCONNECTED callback finishes the job
            except pj.Error:
                pass
        self._finish(session, reason)

    def _stop_player(self, session: CallSession) -> None:
        if session.player is None:
            return
        try:
            if session.audio_media is not None:
                session.player.stopTransmit(session.audio_media)
        except pj.Error:
            pass
        session.player = None
        if session.playback_started:
            self._publish(ev.PLAYBACK_FINISHED, session)

    def _log_rtp(self, session: CallSession) -> None:
        """Record what actually went down the wire.

        A page that silently sends no RTP looks identical, from the SIP dialog
        alone, to one that works. The packet counters are the only honest signal.
        """
        call = session.call
        if call is None:
            return
        try:
            info = call.getInfo()
            for i, media in enumerate(info.media):
                if media.type != pj.PJMEDIA_TYPE_AUDIO:
                    continue
                stat = call.getStreamStat(i)
                tx, rx = stat.rtcp.txStat, stat.rtcp.rxStat
                session.rtp = {
                    "tx_pkt": tx.pkt, "tx_bytes": tx.bytes,
                    "rx_pkt": rx.pkt, "rx_bytes": rx.bytes,
                }
                _LOGGER.info(
                    "call %s rtp: tx_pkt=%s tx_bytes=%s rx_pkt=%s rx_bytes=%s",
                    session.call_id, tx.pkt, tx.bytes, rx.pkt, rx.bytes,
                )
                if tx.pkt == 0:
                    _LOGGER.error(
                        "call %s sent no RTP - the page was silent at the handsets",
                        session.call_id,
                    )
        except Exception:
            _LOGGER.debug("no RTP stats for %s", session.call_id, exc_info=True)

    def _finish(self, session: CallSession, reason: str) -> None:
        if self._sessions.pop(session.call_id, None) is None:
            return
        self._stop_player(session)
        session.state = ev.DISCONNECTED
        session.end_reason = session.end_reason or reason
        self._history.append(session.as_history())
        session.call = None
        session.audio_media = None
        self._publish(ev.DISCONNECTED, session, reason=session.end_reason)

    def _publish(self, event_type: str, session: CallSession, **extra) -> None:
        self.bus.publish_threadsafe(event_type, **session.as_dict(), **extra)
