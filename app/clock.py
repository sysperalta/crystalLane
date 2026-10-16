"""Monotonic clock with an offset, so tests can fast-forward OTP expiry and call TTLs."""

import time


class Clock:
    def __init__(self) -> None:
        self._offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds
