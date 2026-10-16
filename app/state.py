"""Per-call state keyed by X-Call-Id (the HappyRobot run id).

In memory on purpose for the POC, so run a single worker. A multi-instance deployment
would move this to Redis with the same TTL semantics.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.clock import Clock
from app.tms.protocol import TmsLoad

CALL_TTL_SECONDS = 3600
RATE_LIMIT_PER_MINUTE = 60
MAX_TRACKED_CALLS = 10_000
MAX_EVENTS_PER_CALL = 100


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class OtpState:
    code_hash: bytes | None = field(default=None, repr=False)
    expires_at: float = 0.0
    sends: int = 0
    attempts: int = 0
    verified: bool = False


@dataclass
class Negotiation:
    rounds: int = 0
    last_counter_cents: int | None = None
    approved_cents: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class Booking:
    reference: str
    rate_cents: int


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    queue_position: int


@dataclass
class CallState:
    call_id: str
    created_at: float
    started_at: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    mc_number: str | None = None
    carrier_name: str | None = None
    authority_status: str | None = None
    authorized: bool = False
    phone_on_file: str | None = field(default=None, repr=False)
    otp: OtpState = field(default_factory=OtpState, repr=False)
    # Loads shown to the carrier on this call, with their internal max_rate.
    offered_loads: dict[str, TmsLoad] = field(default_factory=dict, repr=False)
    negotiations: dict[str, Negotiation] = field(default_factory=dict, repr=False)
    bookings: dict[str, Booking] = field(default_factory=dict)
    handoffs: dict[str, Handoff] = field(default_factory=dict)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_CALL), repr=False)
    hits: deque[float] = field(default_factory=deque, repr=False)

    def record(self, step: str, outcome: str, detail: str | None = None) -> None:
        """Timeline entry for the demo console. Never pass secrets (OTP, max_rate, phone)."""
        self.events.append({"at": utc_now_iso(), "step": step, "outcome": outcome, "detail": detail})


class CallStore:
    def __init__(
        self,
        clock: Clock,
        *,
        ttl_seconds: float = CALL_TTL_SECONDS,
        max_calls: int = MAX_TRACKED_CALLS,
        rate_limit_per_minute: int = RATE_LIMIT_PER_MINUTE,
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._max_calls = max_calls
        self._rate_limit = rate_limit_per_minute
        self._calls: OrderedDict[str, CallState] = OrderedDict()

    def get_or_create(self, call_id: str) -> CallState:
        now = self._clock.monotonic()
        state = self._calls.get(call_id)
        if state is not None and now - state.created_at >= self._ttl:
            del self._calls[call_id]
            state = None
        if state is None:
            if len(self._calls) >= self._max_calls:
                self.purge_expired()
                while len(self._calls) >= self._max_calls:
                    self._calls.popitem(last=False)
            state = CallState(call_id=call_id, created_at=now, started_at=utc_now_iso())
            self._calls[call_id] = state
        return state

    def allow_request(self, state: CallState) -> bool:
        """Sliding one-minute window per X-Call-Id."""
        now = self._clock.monotonic()
        while state.hits and now - state.hits[0] >= 60:
            state.hits.popleft()
        if len(state.hits) >= self._rate_limit:
            return False
        state.hits.append(now)
        return True

    def retry_after_seconds(self, state: CallState) -> int:
        if not state.hits:
            return 1
        return max(1, math.ceil(60 - (self._clock.monotonic() - state.hits[0])))

    def purge_expired(self) -> int:
        now = self._clock.monotonic()
        expired = [cid for cid, s in self._calls.items() if now - s.created_at >= self._ttl]
        for cid in expired:
            del self._calls[cid]
        return len(expired)

    def recent(self, limit: int = 20) -> list[CallState]:
        return list(reversed(self._calls.values()))[:limit]

    def clear(self) -> None:
        self._calls.clear()
