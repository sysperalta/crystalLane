"""Simulated FMCSA operating-authority lookup.

Mimics the QCMobile docket lookup: MC number -> legal name + authority status. Swap this
class for a real HTTP client (FMCSA_KEY) without touching the routes.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

AuthorityStatus = Literal["active", "inactive", "revoked"]

# Scripted carriers so every branch can be shown live.
FIXTURES: dict[str, tuple[str, AuthorityStatus]] = {
    "123456": ("Blue Ridge Transport LLC", "active"),
    "654321": ("Lone Star Haulers Inc", "active"),
    "246810": ("Great Lakes Carriers Co", "active"),  # no phone on file unless DEMO_CARRIERS adds one
    "111111": ("Midwest Freightways LLC", "inactive"),
    "222222": ("Rapid Route Logistics Inc", "revoked"),
}
OUTAGE_MC = "999999"  # simulates FMCSA being down -> 503 fmcsa_unavailable


class FmcsaUnavailable(Exception):
    pass


@dataclass(frozen=True)
class CarrierRecord:
    mc_number: str
    legal_name: str
    authority_status: AuthorityStatus


class SimulatedFmcsaClient:
    def __init__(
        self,
        extra_active_mcs: Iterable[str] = (),
        latency: tuple[float, float] = (0.05, 0.2),
        qa_mcs: Iterable[str] = (),
    ) -> None:
        # Any MC configured in DEMO_CARRIERS is treated as an active carrier.
        self._extra_active = set(extra_active_mcs) - FIXTURES.keys()
        # QA_TEST_CARRIERS are active too, under a name that makes test calls obvious.
        self._qa = set(qa_mcs) - FIXTURES.keys()
        self._latency = latency

    async def lookup(self, mc_number: str) -> CarrierRecord | None:
        if self._latency[1] > 0:
            await asyncio.sleep(random.uniform(*self._latency))
        if mc_number == OUTAGE_MC:
            raise FmcsaUnavailable()
        if mc_number in FIXTURES:
            name, status = FIXTURES[mc_number]
            return CarrierRecord(mc_number, name, status)
        if mc_number in self._qa:
            return CarrierRecord(mc_number, "QA Test Carrier LLC", "active")
        if mc_number in self._extra_active:
            return CarrierRecord(mc_number, f"Demo Carrier {mc_number} LLC", "active")
        return None

    async def ping(self) -> bool:
        return True
