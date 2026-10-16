from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.deps import Services, call_context, get_services, require_carrier_checked
from app.errors import ApiError, error_responses, step_out_of_order
from app.logging_setup import log_event
from app.models import McNumberRequest, SendOtpResponse, VerifyOtpRequest, VerifyOtpResponse
from app.services.otp import MAX_ATTEMPTS_PER_CALL, MAX_SENDS_PER_CALL, OTP_TTL_SECONDS, sms_body
from app.services.sms import SmsUnavailable, mask_phone
from app.state import CallState

router = APIRouter(tags=["OTP"])
log = logging.getLogger("clf.otp")


def _resends_remaining(sends: int) -> int:
    return max(0, MAX_SENDS_PER_CALL - max(sends, 1))


@router.post(
    "/otp/send",
    operation_id="sendOtp",
    summary="Send a one-time code to the phone number on file",
    description="Maps to agent action `send_otp`. Sends only to the number on file for the MC "
    "(there is no phone field). The code is never returned. Max 2 sends per call; a resend "
    "invalidates the previous code.",
    response_model=SendOtpResponse,
    responses=error_responses(SendOtpResponse),
)
async def send_otp(
    body: McNumberRequest,
    state: CallState = Depends(call_context),
    svc: Services = Depends(get_services),
) -> SendOtpResponse:
    async with state.lock:
        require_carrier_checked(state, body.mc_number)
        otp = state.otp
        if not state.authorized:
            state.record("send_otp", "not_authorized")
            return SendOtpResponse(status="not_authorized", masked_phone=None, expires_in_seconds=None,
                                   resends_remaining=_resends_remaining(otp.sends))
        if otp.verified:
            raise step_out_of_order("OTP already verified for this call; continue with load search")
        if otp.attempts >= MAX_ATTEMPTS_PER_CALL:
            raise step_out_of_order("OTP is locked for this call after 3 failed attempts")
        if not state.phone_on_file:
            state.record("send_otp", "no_phone_on_file")
            return SendOtpResponse(status="no_phone_on_file", masked_phone=None, expires_in_seconds=None,
                                   resends_remaining=_resends_remaining(otp.sends))
        if otp.sends >= MAX_SENDS_PER_CALL:
            state.record("send_otp", "resend_limit_reached")
            return SendOtpResponse(status="resend_limit_reached", masked_phone=None, expires_in_seconds=None,
                                   resends_remaining=0)

        # QA test carriers (demo only) get their fixed code; everyone else a fresh random one.
        fixed_code = svc.settings.qa_test_carriers.get(state.mc_number)
        code = fixed_code or svc.otp.generate()
        try:
            await svc.sms.send(to_phone=state.phone_on_file, mc_number=state.mc_number, body=sms_body(code),
                               call_id=state.call_id)
        except SmsUnavailable:
            raise ApiError(503, "sms_unavailable", "SMS provider unavailable; try again shortly", retryable=True) from None
        otp.code_hash = svc.otp.digest(code)  # only the keyed hash is kept; a resend replaces it
        otp.expires_at = svc.clock.monotonic() + OTP_TTL_SECONDS
        otp.sends += 1
        del code

        masked = mask_phone(state.phone_on_file)
        log_event(log, "otp_sent", call_id=state.call_id, mc_number=state.mc_number, masked_phone=masked,
                  send_number=otp.sends, qa_fixed_code=fixed_code is not None)
        state.record("send_otp", "sent", f"to {masked} (send {otp.sends}/{MAX_SENDS_PER_CALL})")
        return SendOtpResponse(status="sent", masked_phone=masked, expires_in_seconds=OTP_TTL_SECONDS,
                               resends_remaining=MAX_SENDS_PER_CALL - otp.sends)


@router.post(
    "/otp/verify",
    operation_id="verifyOtp",
    summary="Verify the code the carrier read back",
    description="Maps to agent action `verify_otp`. Max 3 attempts per call, then locked. "
    "Only `verified` unlocks load search, booking and handoff for this X-Call-Id.",
    response_model=VerifyOtpResponse,
    responses=error_responses(VerifyOtpResponse),
)
async def verify_otp(
    body: VerifyOtpRequest,
    state: CallState = Depends(call_context),
    svc: Services = Depends(get_services),
) -> VerifyOtpResponse:
    async with state.lock:
        require_carrier_checked(state, body.mc_number)
        otp = state.otp
        remaining = MAX_ATTEMPTS_PER_CALL - otp.attempts

        if otp.verified:
            return VerifyOtpResponse(status="verified", attempts_remaining=remaining)
        if remaining <= 0:
            return _result(state, "attempts_exhausted", 0)
        if otp.code_hash is None:
            return _result(state, "not_sent", remaining)
        if svc.clock.monotonic() >= otp.expires_at:
            return _result(state, "expired", remaining)  # an expired code doesn't burn an attempt

        otp.attempts += 1
        remaining = MAX_ATTEMPTS_PER_CALL - otp.attempts
        if svc.otp.matches(body.code, otp.code_hash):
            otp.verified = True
            otp.code_hash = None
            return _result(state, "verified", remaining)
        if remaining <= 0:
            otp.code_hash = None
            return _result(state, "attempts_exhausted", 0)
        return _result(state, "incorrect", remaining)


def _result(state: CallState, status: str, remaining: int) -> VerifyOtpResponse:
    log_event(log, "otp_checked", call_id=state.call_id, mc_number=state.mc_number, status=status, attempts_remaining=remaining)
    state.record("verify_otp", status, f"{remaining} attempts left")
    return VerifyOtpResponse(status=status, attempts_remaining=remaining)
