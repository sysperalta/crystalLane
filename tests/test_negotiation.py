"""Margin rules: pure policy plus the API around it (rounds, approvals, booking)."""

from __future__ import annotations

import pytest

from app.services.negotiation import evaluate_offer
from tests.conftest import Call, failed, ok

L, M = 210_000, 240_000  # LD-10482: loadboard $2,100, max $2,400 (cents)


@pytest.mark.parametrize(
    ("offer", "round_no", "last", "expected"),
    [
        (200_000, 1, None, ("accept", None, 2)),
        (210_000, 1, None, ("accept", None, 2)),
        (215_000, 1, None, ("accept", None, 2)),  # small gap: not worth a counter
        (230_000, 1, None, ("counter", 220_000, 2)),  # in budget: meet in the middle once
        (230_000, 2, 220_000, ("accept", None, 1)),
        (260_000, 1, None, ("counter", 220_000, 2)),  # 1/3 of the room
        (250_000, 2, 220_000, ("counter", 230_000, 1)),  # 2/3 of the room
        (245_000, 3, 230_000, ("counter", 240_000, 0)),  # best and final
        (235_000, 2, 220_000, ("accept", None, 1)),
        (245_000, 4, 240_000, ("final_decline", None, 0)),
    ],
)
def test_policy(offer: int, round_no: int, last: int | None, expected: tuple) -> None:
    d = evaluate_offer(loadboard_cents=L, max_cents=M, offer_cents=offer, round_no=round_no, last_counter_cents=last)
    assert (d.decision, d.counter_cents, d.rounds_remaining) == expected


@pytest.mark.parametrize("loadboard, ceiling", [(100_000, 101_000), (210_000, 240_000), (99_900, 99_900), (51_300, 77_700)])
def test_policy_invariants(loadboard: int, ceiling: int) -> None:
    last = None
    for round_no, offer in enumerate([ceiling * 2, ceiling * 2, ceiling * 2, ceiling * 2], start=1):
        d = evaluate_offer(loadboard_cents=loadboard, max_cents=ceiling, offer_cents=offer,
                           round_no=round_no, last_counter_cents=last)
        if d.counter_cents is not None:
            assert d.counter_cents <= ceiling
            assert d.counter_cents <= offer
            assert last is None or d.counter_cents > last
            last = d.counter_cents
    assert d.decision == "final_decline"


def _ready(call: Call) -> None:
    call.authenticate()
    call.search()


def test_rounds_counted_by_service_not_agent(call: Call) -> None:
    _ready(call)
    decisions = [call.offer("LD-10482", 2700, round_no=1).json() for _ in range(4)]  # agent always says round 1
    assert [d["decision"] for d in decisions] == ["counter", "counter", "counter", "final_decline"]
    assert [d["counter_rate"] for d in decisions] == [2200, 2300, 2400, None]
    assert [d["rounds_remaining"] for d in decisions] == [2, 1, 0, 0]


def test_round_above_three_is_accepted(call: Call) -> None:
    _ready(call)
    assert call.offer("LD-10482", 2000, round_no=4).json()["ok"] is True


def test_only_approved_rates_can_be_booked(call: Call) -> None:
    _ready(call)
    assert call.book("LD-10482", 2250).json() == ok(status="rate_not_approved", booking_reference=None)
    call.offer("LD-10482", 2600)  # counter 2200 -> now approved
    assert call.book("LD-10482", 2250).json()["status"] == "rate_not_approved"
    assert call.book("LD-10482", 2200).json()["status"] == "booked"


def test_accepted_offer_is_bookable(call: Call) -> None:
    _ready(call)
    assert call.offer("LD-10482", 2050).json()["decision"] == "accept"
    assert call.book("LD-10482", 2050).json()["status"] == "booked"


def test_implausible_offer_is_rejected_without_using_a_round(call: Call) -> None:
    _ready(call)
    failed(call.offer("LD-10482", 2.25), "invalid_request")
    assert call.offer("LD-10482", 2600).json()["rounds_remaining"] == 2


def test_offer_accepts_dollar_strings(call: Call) -> None:
    _ready(call)
    assert call.offer("ld-10482", "$2,050").json()["decision"] == "accept"
