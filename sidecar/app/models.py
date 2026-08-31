"""Request and response shapes for the control API.

Deliberately stack-agnostic: nothing here mentions pjsua2. If the SIP layer is
ever swapped, this file and the integration that talks to it do not change.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CallRequest(BaseModel):
    target: str = Field(..., description="Paging extension, or a full sip: URI")
    media: str | None = Field(
        None, description="http(s) URL, 'sound:<name>', or a path inside the sounds volume"
    )
    chime: str | None = Field(None, description="'sound:<name>' played before the media")
    lead_in: float | None = Field(
        None, ge=0, le=10,
        description="Seconds to wait after the call is answered before playing. "
                    "Auto-answering handsets need this or the first word is clipped.",
    )
    account_id: str | None = None
    policy: Literal["reject", "preempt"] = Field(
        "reject",
        description="What to do when this target already has a call in flight. "
                    "Queueing is the integration's job, not the sidecar's.",
    )
    headers: dict[str, str] | None = Field(
        None, description="Extra headers for fetching the media URL, e.g. Authorization"
    )


class CallResponse(BaseModel):
    call_id: str
    target: str
    account_id: str
    state: str
    duration: float
    lead_in: float
    media: str
    cached: bool
    preempted: list[str] = []


class AccountHealth(BaseModel):
    account_id: str
    uri: str
    registered: bool
    code: int
    reason: str
    since: float


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    accounts: list[AccountHealth]
    active_calls: list[dict]
    sounds: list[str]
    lead_in: float
    codecs: list[str]
