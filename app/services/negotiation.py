"""Negotiation policy (the spec's example policy, pending confirmation by Crystal Lane).

Pure function over integer cents. Guarantees, whatever the inputs:
- a counter is never above max_rate and never above the carrier's offer
- each counter is strictly higher than the previous counter on this call
- after MAX_ROUNDS the decision is always final_decline
Nothing returned reveals max_rate (no gaps, percentages or limits).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_ROUNDS = 3
_GRID_CENTS = 500  # counters land on whole $5 amounts so they sound natural on a call
_MIN_IN_BAND_GAP_CENTS = 2500  # below this (or 3% of loadboard) an in-budget offer is just accepted


@dataclass(frozen=True)
class Decision:
    decision: Literal["accept", "counter", "final_decline"]
    counter_cents: int | None
    rounds_remaining: int


def evaluate_offer(
    *,
    loadboard_cents: int,
    max_cents: int,
    offer_cents: int,
    round_no: int,
    last_counter_cents: int | None,
) -> Decision:
    if round_no > MAX_ROUNDS:
        return Decision("final_decline", None, 0)
    remaining = MAX_ROUNDS - round_no
    floor = max(loadboard_cents, last_counter_cents or 0)  # a counter must beat both

    if offer_cents <= loadboard_cents:
        return Decision("accept", None, remaining)

    if offer_cents <= max_cents:
        # Within budget. In round 1, try once to meet in the middle if the gap is worth it.
        gap = offer_cents - loadboard_cents
        if round_no == 1 and last_counter_cents is None and gap >= max(_MIN_IN_BAND_GAP_CENTS, loadboard_cents * 3 // 100):
            counter = _counter_between(
                target=loadboard_cents + gap // 2,
                above=floor,
                ceiling=_floor_grid(offer_cents - 1),
            )
            if counter is not None:
                return Decision("counter", counter, remaining)
        return Decision("accept", None, remaining)

    # Above budget: step up by thirds of the room between loadboard and max.
    if round_no < MAX_ROUNDS:
        counter = _counter_between(
            target=loadboard_cents + (max_cents - loadboard_cents) * round_no // MAX_ROUNDS,
            above=floor,
            ceiling=max_cents,
        )
        if counter is None:
            return Decision("final_decline", None, 0)
        return Decision("counter", counter, remaining)

    # Last round: best and final at the ceiling, if that still improves on our last counter.
    if last_counter_cents is None or max_cents > last_counter_cents:
        return Decision("counter", max_cents, 0)
    return Decision("final_decline", None, 0)


def _floor_grid(cents: int) -> int:
    return cents // _GRID_CENTS * _GRID_CENTS


def _ceil_grid(cents: int) -> int:
    return -(-cents // _GRID_CENTS) * _GRID_CENTS


def _counter_between(*, target: int, above: int, ceiling: int) -> int | None:
    """Grid-aligned amount strictly above `above` and at most `ceiling`, as close to target as possible."""
    counter = _floor_grid(target)
    if counter <= above:
        counter = _ceil_grid(above + 1)
    counter = min(counter, ceiling)
    return counter if above < counter <= ceiling else None
