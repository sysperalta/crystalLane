"""Simulated SMS provider.

Stands in for Twilio or similar. "Delivered" messages land in an in-memory handset that only
the demo console can read (separate DEMO_CONSOLE_KEY). Nothing here is ever returned to
HappyRobot or written to the logs.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from app.state import utc_now_iso


class SmsUnavailable(Exception):
    pass


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"***-***-{digits[-4:]}"


@dataclass(frozen=True)
class SmsMessage:
    sent_at: str
    mc_number: str
    to_masked: str
    body: str = field(repr=False)
    call_id: str | None = None  # lets QA automation fetch the code sent on its own call


class SimulatedSmsProvider:
    def __init__(self, max_messages: int = 200) -> None:
        self._outbox: deque[SmsMessage] = deque(maxlen=max_messages)

    async def send(self, *, to_phone: str, mc_number: str, body: str, call_id: str | None = None) -> None:
        self._outbox.append(SmsMessage(utc_now_iso(), mc_number, mask_phone(to_phone), body, call_id))

    def inbox(self, mc_number: str | None = None, limit: int = 20, call_id: str | None = None) -> list[SmsMessage]:
        messages = [
            m for m in reversed(self._outbox)
            if (mc_number is None or m.mc_number == mc_number) and (call_id is None or m.call_id == call_id)
        ]
        return messages[:limit]

    def clear(self) -> None:
        self._outbox.clear()
