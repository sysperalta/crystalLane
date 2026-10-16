"""Shared services, request dependencies (auth, call context, rate limit) and step guards."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyHeader

from app.clock import Clock
from app.config import Settings
from app.errors import ApiError, invalid_request, step_out_of_order
from app.services.fmcsa import SimulatedFmcsaClient
from app.services.handoff import HandoffDesk
from app.services.otp import OtpService
from app.services.sms import SimulatedSmsProvider
from app.state import CallState, CallStore
from app.tms.client import TmsClient

if TYPE_CHECKING:
    from tms_sim.server import TmsSimulator

CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass
class Services:
    settings: Settings
    clock: Clock
    calls: CallStore
    otp: OtpService
    sms: SimulatedSmsProvider
    fmcsa: SimulatedFmcsaClient
    phones_on_file: Mapping[str, str]
    handoffs: HandoffDesk
    tms: TmsClient | None = None  # set at startup (needs the embedded simulator's port)
    tms_sim: TmsSimulator | None = None


api_key_scheme = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ApiKeyAuth",
    description="Shared secret from env var API_KEY. Compared in constant time. Rotatable.",
    auto_error=False,
)


def get_services(request: Request) -> Services:
    return request.app.state.services


async def require_api_key(
    svc: Services = Depends(get_services),
    api_key: str | None = Security(api_key_scheme),
) -> None:
    supplied = (api_key or "").encode()
    valid = False
    for key in svc.settings.api_keys:  # check every key: no early exit, constant time
        valid |= hmac.compare_digest(supplied, key.encode())
    if not valid:
        raise ApiError(401, "unauthorized", "Invalid API key")


async def call_context(
    _: None = Depends(require_api_key),
    svc: Services = Depends(get_services),
    x_call_id: str | None = Header(
        default=None,
        alias="X-Call-Id",
        description="HappyRobot run ID. Groups all requests of one call. Call state expires after 1 hour.",
        examples=["01a11d81-0000-7000-8000-000000000001"],
    ),
) -> CallState:
    if not x_call_id:
        raise invalid_request("X-Call-Id header is required")
    if not CALL_ID_RE.match(x_call_id):
        raise invalid_request("X-Call-Id must be 1-128 characters: letters, digits, '.', '_', ':', '-'")
    state = svc.calls.get_or_create(x_call_id)
    if not svc.calls.allow_request(state):
        raise ApiError(
            429,
            "rate_limited",
            "Too many requests on this call; wait and retry",
            retryable=True,
            headers={"Retry-After": str(svc.calls.retry_after_seconds(state))},
        )
    return state


# --- step guards: enforced call order -----------------------------------------------


def require_carrier_checked(state: CallState, mc_number: str) -> None:
    if state.mc_number is None:
        raise step_out_of_order("Carrier not verified for this call; call /carriers/verify first")
    if state.mc_number != mc_number:
        raise step_out_of_order("mc_number does not match the carrier verified on this call")


def require_otp_verified(state: CallState, mc_number: str) -> None:
    require_carrier_checked(state, mc_number)
    if not state.authorized:
        raise step_out_of_order("Carrier is not authorized to operate")
    if not state.otp.verified:
        raise step_out_of_order("OTP not verified for this call")
