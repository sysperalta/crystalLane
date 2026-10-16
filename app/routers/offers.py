from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.deps import call_context, require_otp_verified
from app.errors import error_responses, invalid_request, step_out_of_order
from app.logging_setup import log_event
from app.models import EvaluateOfferRequest, EvaluateOfferResponse
from app.money import to_cents, to_dollars, usd
from app.services.negotiation import evaluate_offer
from app.state import CallState, Negotiation

router = APIRouter(tags=["Negotiation"])
log = logging.getLogger("clf.offers")


@router.post(
    "/offers/evaluate",
    operation_id="evaluateOffer",
    summary="Decide on a carrier's counter offer",
    description="Maps to agent action `evaluate_offer`. The service counts rounds itself per "
    "X-Call-Id + load_id (the `round` field is informational). After 3 rounds the decision is "
    "always `final_decline`. Accepted and countered amounts become approved booking rates.",
    response_model=EvaluateOfferResponse,
    responses=error_responses(EvaluateOfferResponse),
)
async def evaluate(
    body: EvaluateOfferRequest,
    state: CallState = Depends(call_context),
) -> EvaluateOfferResponse:
    async with state.lock:
        require_otp_verified(state, body.mc_number)
        load = state.offered_loads.get(body.load_id)
        if load is None:
            raise step_out_of_order("load_id was not offered to this carrier on this call; search loads first")

        offer_cents = to_cents(body.carrier_offer)
        if offer_cents <= 0 or offer_cents * 2 < load.loadboard_cents:
            # Almost always a unit/transcription slip (e.g. 2.25 for $2,250). Doesn't use a round.
            raise invalid_request("carrier_offer looks wrong: send the carrier's total rate in US dollars, e.g. 2250")

        negotiation = state.negotiations.setdefault(body.load_id, Negotiation())
        negotiation.rounds += 1
        decision = evaluate_offer(
            loadboard_cents=load.loadboard_cents,
            max_cents=load.max_rate_cents,
            offer_cents=offer_cents,
            round_no=negotiation.rounds,
            last_counter_cents=negotiation.last_counter_cents,
        )
        if decision.decision == "accept":
            negotiation.approved_cents.add(offer_cents)
        elif decision.decision == "counter":
            negotiation.approved_cents.add(decision.counter_cents)
            negotiation.last_counter_cents = decision.counter_cents

        counter = to_dollars(decision.counter_cents) if decision.counter_cents is not None else None
        log_event(log, "offer_evaluated", call_id=state.call_id, load_id=body.load_id, mc_number=body.mc_number,
                  round=negotiation.rounds, agent_round=body.round, carrier_offer=body.carrier_offer,
                  decision=decision.decision, counter_rate=counter)
        detail = f"{body.load_id} round {negotiation.rounds}: carrier {usd(body.carrier_offer)}"
        if counter is not None:
            detail += f", counter {usd(counter)}"
        state.record("evaluate_offer", decision.decision, detail)
        return EvaluateOfferResponse(
            decision=decision.decision,
            counter_rate=counter,
            rounds_remaining=decision.rounds_remaining,
        )
