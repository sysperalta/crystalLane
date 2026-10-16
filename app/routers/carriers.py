from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.deps import Services, call_context, get_services
from app.errors import ApiError, error_responses, step_out_of_order
from app.logging_setup import log_event
from app.models import VerifyCarrierRequest, VerifyCarrierResponse
from app.services.fmcsa import FmcsaUnavailable
from app.state import CallState

router = APIRouter(tags=["Carriers"])
log = logging.getLogger("clf.carriers")

_REASONS = {"inactive": "authority_inactive", "revoked": "authority_revoked", "not_found": "carrier_not_found"}


@router.post(
    "/carriers/verify",
    operation_id="verifyCarrier",
    summary="Check carrier operating authority with FMCSA",
    description="Maps to agent action `verify_carrier`. `authorized` is true only for ACTIVE authority. "
    "The carrier's phone number is never returned.",
    response_model=VerifyCarrierResponse,
    responses=error_responses(VerifyCarrierResponse),
)
async def verify_carrier(
    body: VerifyCarrierRequest,
    state: CallState = Depends(call_context),
    svc: Services = Depends(get_services),
) -> VerifyCarrierResponse:
    mc = body.mc_number
    async with state.lock:
        if state.otp.verified and state.mc_number != mc:
            raise step_out_of_order("This call is already verified for a different MC number")
        try:
            record = await svc.fmcsa.lookup(mc)
        except FmcsaUnavailable:
            log_event(log, "fmcsa_unavailable", logging.WARNING, call_id=state.call_id, mc_number=mc)
            state.record("verify_carrier", "fmcsa_unavailable", f"MC {mc}")
            raise ApiError(503, "fmcsa_unavailable", "FMCSA did not respond; try again shortly", retryable=True) from None

        status = record.authority_status if record else "not_found"
        authorized = status == "active"
        if state.mc_number != mc:
            state.otp.code_hash = None  # a code sent for another MC must never verify this one
        state.mc_number = mc
        state.authorized = authorized
        state.authority_status = status
        state.carrier_name = record.legal_name if record else None
        state.phone_on_file = svc.phones_on_file.get(mc) if authorized else None

        log_event(log, "carrier_verified", call_id=state.call_id, mc_number=mc, authorized=authorized, authority_status=status)
        state.record("verify_carrier", "authorized" if authorized else status, f"MC {mc} {state.carrier_name or ''}".strip())
        return VerifyCarrierResponse(
            authorized=authorized,
            carrier_name=state.carrier_name if authorized else None,
            authority_status=status,
            reason=None if authorized else _REASONS[status],
        )
