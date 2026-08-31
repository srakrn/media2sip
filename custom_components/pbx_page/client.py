"""Client for the sidecar's control API.

The integration is a state machine driven by sidecar events, so this client's
real job is not issuing commands — it is keeping the websocket up and telling
everyone when it is not. A page group that silently stops working is worse than
one that visibly breaks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff. Starts fast because a sidecar restart is the common case,
# and caps low enough that recovery never takes minutes.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SidecarError(Exception):
    """The sidecar refused or could not service a request."""

    def __init__(self, message: str, status: int | None = None, sip_code: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.sip_code = sip_code


class SidecarBusy(SidecarError):
    """The target already has a call in flight and the policy is to reject."""


class PbxPageClient:
    """HTTP commands plus a websocket event stream."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str | None = None,
    ) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._token = token or None

        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._task: asyncio.Task | None = None
        self._closing = False

        self.connected = False
        self.accounts: dict[str, dict] = {}
        self.sounds: list[str] = []
        self.lead_in: float | None = None
        self.sidecar_version: str | None = None

    # -- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="pbx_page websocket")

    async def async_stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.connected = False

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    # -- registration awareness -----------------------------------------

    @property
    def registered(self) -> bool:
        """True when every configured account holds a registration.

        Availability hangs off this. Losing registration has to surface, because
        it is the failure mode you most want to be able to alert on.
        """
        return bool(self.accounts) and all(a.get("registered") for a in self.accounts.values())

    @property
    def available(self) -> bool:
        return self.connected and self.registered

    # -- commands --------------------------------------------------------

    async def async_health(self) -> dict:
        return await self._request("GET", "/health")

    async def async_place_call(self, **payload: Any) -> dict:
        return await self._request("POST", "/call", json=payload)

    async def async_hangup(self, call_id: str) -> dict:
        return await self._request("DELETE", f"/call/{call_id}")

    async def async_active_calls(self) -> list[dict]:
        return await self._request("GET", "/calls")

    async def async_history(self, limit: int = 20) -> list[dict]:
        return await self._request("GET", f"/calls/history?limit={limit}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with self._session.request(
                method, f"{self._url}{path}", headers=headers, timeout=_REQUEST_TIMEOUT, **kwargs
            ) as response:
                body: Any = None
                try:
                    # content_type=None: parse the body on its merits rather than
                    # trusting a header. A sidecar behind a proxy that rewrites
                    # Content-Type should still yield its error detail.
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = None
                if response.status >= 400:
                    detail = (body or {}).get("detail") if isinstance(body, dict) else None
                    message = detail or f"{method} {path} returned {response.status}"
                    sip_code = (body or {}).get("sip_code", 0) if isinstance(body, dict) else 0
                    if response.status == 409:
                        raise SidecarBusy(message, response.status, sip_code)
                    raise SidecarError(message, response.status, sip_code)
                return body
        except TimeoutError as err:
            raise SidecarError(f"{method} {path} timed out") from err
        except (aiohttp.ClientError, OSError) as err:
            raise SidecarError(f"cannot reach the sidecar: {err}") from err

    # -- event stream ----------------------------------------------------

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        while not self._closing:
            try:
                await self._connect()
                backoff = _BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must survive anything
                if self.connected:
                    _LOGGER.warning("sidecar event stream dropped: %s", err)
                else:
                    _LOGGER.debug("sidecar still unreachable: %s", err)
                self._set_connected(False)

            if self._closing:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _connect(self) -> None:
        # Seed from /health first, so entities know their availability the moment
        # the websocket opens rather than after the first state change.
        health = await self.async_health()
        self._apply_health(health)

        url = f"{self._url}/ws"
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with self._session.ws_connect(url, headers=headers, heartbeat=30) as socket:
            _LOGGER.info("connected to the sidecar event stream at %s", url)
            self._set_connected(True)
            async for message in socket:
                if message.type is aiohttp.WSMsgType.TEXT:
                    self._handle(message.json())
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        raise ConnectionError("event stream closed")

    def _apply_health(self, health: dict) -> None:
        self.accounts = {a["account_id"]: a for a in health.get("accounts", [])}
        self.sounds = health.get("sounds", [])
        self.lead_in = health.get("lead_in")
        self.sidecar_version = health.get("version")

    def _handle(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "hello":
            self.accounts = {a["account_id"]: a for a in event.get("accounts", [])}
        elif event_type in ("registered", "unregistered"):
            account_id = event.get("account_id")
            if account_id in self.accounts:
                self.accounts[account_id] = {
                    **self.accounts[account_id],
                    "registered": event_type == "registered",
                    "code": event.get("code", 0),
                    "reason": event.get("reason", ""),
                }
            if event_type == "unregistered":
                _LOGGER.warning(
                    "account %s lost its registration (%s %s) - paging is down",
                    account_id, event.get("code"), event.get("reason"),
                )
        self._dispatch(event)

    def _set_connected(self, connected: bool) -> None:
        if self.connected == connected:
            return
        self.connected = connected
        if not connected:
            # Do not keep claiming registrations we can no longer observe.
            self.accounts = {k: {**v, "registered": False} for k, v in self.accounts.items()}
        self._dispatch({"type": "connection", "connected": connected})

    def _dispatch(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("error in a pbx_page event listener")
