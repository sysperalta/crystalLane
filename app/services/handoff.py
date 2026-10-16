"""Mock of the Senior Rep Queue. Live transfers don't work on web calls, so the POC records
the handoff and reports a queue position."""

from __future__ import annotations

import itertools
import secrets
from collections import deque
from typing import Any

from app.clock import Clock
from app.state import Handoff, utc_now_iso

_QUEUE_WINDOW_SECONDS = 600


class HandoffDesk:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._recent: deque[float] = deque()
        self._ids = itertools.count(3001 + secrets.randbelow(5000))
        self.history: deque[dict[str, Any]] = deque(maxlen=100)

    def enqueue(self, *, call_id: str, load_id: str, mc_number: str, rate: float, booking_reference: str) -> Handoff:
        now = self._clock.monotonic()
        while self._recent and now - self._recent[0] > _QUEUE_WINDOW_SECONDS:
            self._recent.popleft()
        self._recent.append(now)
        handoff = Handoff(handoff_id=f"HO-{next(self._ids)}", queue_position=len(self._recent))
        self.history.appendleft(
            {
                "handoff_id": handoff.handoff_id,
                "queued_at": utc_now_iso(),
                "call_id": call_id,
                "load_id": load_id,
                "mc_number": mc_number,
                "agreed_rate": rate,
                "booking_reference": booking_reference,
            }
        )
        return handoff

    def clear(self) -> None:
        self._recent.clear()
        self.history.clear()
