from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.deps import Services, call_context, get_services, require_otp_verified
from app.errors import error_responses, invalid_request, step_out_of_order
from app.logging_setup import log_event
from app.models import HandoffRequest, HandoffResponse
from app.money import to_cents
from app.state import CallState

router = APIRouter(tags=["Handoff"])
log = logging.getLogger("clf.handoff")


@router.post(
    "/handoff",
    operation_id="handoff",
    summary="Hand the carrier off to the Senior Rep Queue (mocked for POC)",
    description="Maps to agent action `transfer_to_senior_rep`. Allowed only after "
    "/loads/{load_id}/book returned `booked` on this X-Call-Id. Idempotent per load.",
    response_model=HandoffResponse,
    responses=error_responses(HandoffResponse),
)
async def handoff(
    body: HandoffRequest,
    state: CallState = Depends(call_context),
    svc: Services = Depends(get_services),
) -> HandoffResponse:
    async with state.lock:
        require_otp_verified(state, body.mc_number)
        booking = state.bookings.get(body.load_id)
        if booking is None:
            raise step_out_of_order("Load is not booked on this call; book it before the handoff")
        if to_cents(body.agreed_rate) != booking.rate_cents:
            raise invalid_request("agreed_rate does not match the rate this load was booked at")

        existing = state.handoffs.get(body.load_id)
        if existing is None:
            existing = svc.handoffs.enqueue(
                call_id=state.call_id,
                load_id=body.load_id,
                mc_number=body.mc_number,
                rate=body.agreed_rate,
                booking_reference=booking.reference,
            )
            state.handoffs[body.load_id] = existing
            log_event(log, "handoff_queued", call_id=state.call_id, load_id=body.load_id, mc_number=body.mc_number,
                      handoff_id=existing.handoff_id, queue_position=existing.queue_position)
            state.record("transfer_to_senior_rep", "queued", f"{existing.handoff_id}, position {existing.queue_position}")
        return HandoffResponse(status="queued", queue_position=existing.queue_position, handoff_id=existing.handoff_id)
