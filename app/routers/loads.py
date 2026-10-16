from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import TypeVar

from fastapi import APIRouter, Depends, Path

from app.deps import Services, call_context, get_services, require_otp_verified
from app.errors import ApiError, error_responses, invalid_request, step_out_of_order
from app.logging_setup import log_event
from app.models import BookLoadRequest, BookLoadResponse, Load, SearchLoadsRequest, SearchLoadsResponse
from app.money import to_cents, to_dollars, usd
from app.services.matching import rank_loads
from app.state import Booking, CallState
from app.tms.client import TmsMalformedResponse, TmsUnavailable
from app.tms.protocol import TmsLoad

router = APIRouter(tags=["Loads"])
log = logging.getLogger("clf.loads")

T = TypeVar("T")


async def _call_tms(operation: Awaitable[T], state: CallState, step: str) -> T:
    try:
        return await operation
    except TmsMalformedResponse as exc:
        state.record(step, "tms_malformed_response")
        raise ApiError(503, "tms_malformed_response",
                       f"TMS returned malformed data after {exc.attempts} attempts", retryable=True) from None
    except TmsUnavailable as exc:
        state.record(step, "tms_unavailable")
        raise ApiError(503, "tms_unavailable", f"TMS did not respond after {exc.attempts} attempts", retryable=True) from None


def to_public_load(load: TmsLoad) -> Load:
    """The only place a TMS load becomes an API object. max_rate is deliberately not copied."""
    return Load(
        load_id=load.load_id,
        origin=load.origin,
        destination=load.destination,
        pickup_datetime=load.pickup.isoformat(),
        delivery_datetime=load.delivery.isoformat(),
        equipment_type=load.equipment_type,
        loadboard_rate=to_dollars(load.loadboard_cents),
        weight=load.weight_lbs,
        commodity_type=load.commodity or None,
        num_of_pieces=load.pieces,
        miles=load.miles,
        dimensions=load.dimensions or None,
        notes=load.notes or None,
    )


@router.post(
    "/loads/search",
    operation_id="searchLoads",
    summary="Search available loads in the legacy TMS",
    description="Maps to agent action `find_loads`. Requires OTP verified on this X-Call-Id. "
    "Returns at most 3 available loads, best match first. max_rate never appears.",
    response_model=SearchLoadsResponse,
    responses=error_responses(SearchLoadsResponse),
)
async def search_loads(
    body: SearchLoadsRequest,
    state: CallState = Depends(call_context),
    svc: Services = Depends(get_services),
) -> SearchLoadsResponse:
    async with state.lock:
        require_otp_verified(state, body.mc_number)
        available = await _call_tms(svc.tms.list_available(body.equipment_type), state, "find_loads")
        matches = rank_loads(
            available,
            origin=body.origin,
            destination=body.destination,
            pickup_date=body.pickup_date,
            now=datetime.now(timezone.utc),
        )
        for load in matches:
            state.offered_loads[load.load_id] = load
        status = "found" if matches else "no_match"
        load_ids = [load.load_id for load in matches]
        log_event(log, "loads_searched", call_id=state.call_id, mc_number=body.mc_number, origin=body.origin,
                  destination=body.destination, equipment_type=body.equipment_type, status=status, load_ids=load_ids)
        state.record("find_loads", status,
                     ", ".join(load_ids) or f"{body.origin} -> {body.destination or 'anywhere'} ({body.equipment_type})")
        return SearchLoadsResponse(status=status, loads=[to_public_load(load) for load in matches])


# A blank load_id turns the URL into /loads//book, which no route would match (a bare 404 that
# HappyRobot can't read). Answer it in book_load's own envelope instead. /loads/book covers a
# proxy that collapses the double slash.
@router.post("/loads//book", response_model=BookLoadResponse, include_in_schema=False)
@router.post("/loads/book", response_model=BookLoadResponse, include_in_schema=False)
async def book_load_without_id(_: CallState = Depends(call_context)) -> BookLoadResponse:
    raise invalid_request("load_id is required")


def _idempotency_key(call_id: str, load_id: str) -> str:
    return hashlib.sha256(f"{call_id}:{load_id}".encode()).hexdigest()[:32]


@router.post(
    "/loads/{load_id}/book",
    operation_id="bookLoad",
    summary="Tentatively book a load in the TMS",
    description="Maps to agent action `book_load`. agreed_rate must equal the loadboard_rate or an "
    "amount approved by /offers/evaluate on this call. Idempotent per X-Call-Id + load_id.",
    response_model=BookLoadResponse,
    responses=error_responses(BookLoadResponse),
)
async def book_load(
    body: BookLoadRequest,
    load_id: str = Path(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9-]+$", examples=["LD-10482"]),
    state: CallState = Depends(call_context),
    svc: Services = Depends(get_services),
) -> BookLoadResponse:
    load_id = load_id.upper()
    async with state.lock:
        require_otp_verified(state, body.mc_number)
        load = state.offered_loads.get(load_id)
        if load is None:
            raise step_out_of_order("load_id was not offered to this carrier on this call; search loads first")

        existing = state.bookings.get(load_id)
        if existing is not None:
            return BookLoadResponse(status="booked", booking_reference=existing.reference)

        rate_cents = to_cents(body.agreed_rate)
        negotiation = state.negotiations.get(load_id)
        approved = {load.loadboard_cents} | (negotiation.approved_cents if negotiation else set())
        if rate_cents not in approved:
            log_event(log, "booking_rejected", logging.WARNING, call_id=state.call_id, load_id=load_id,
                      mc_number=body.mc_number, agreed_rate=body.agreed_rate, status="rate_not_approved")
            state.record("book_load", "rate_not_approved", f"{load_id} at {usd(body.agreed_rate)}")
            return BookLoadResponse(status="rate_not_approved", booking_reference=None)

        result = await _call_tms(
            svc.tms.book(load_id, body.mc_number, rate_cents, _idempotency_key(state.call_id, load_id)),
            state,
            "book_load",
        )
        if result.status == "booked":
            state.bookings[load_id] = Booking(reference=result.reference, rate_cents=rate_cents)
        log_event(log, "booking_result", call_id=state.call_id, load_id=load_id, mc_number=body.mc_number,
                  agreed_rate=body.agreed_rate, status=result.status, booking_reference=result.reference)
        state.record("book_load", result.status,
                     f"{load_id} at {usd(body.agreed_rate)}" + (f" -> {result.reference}" if result.reference else ""))
        return BookLoadResponse(status=result.status, booking_reference=result.reference)
