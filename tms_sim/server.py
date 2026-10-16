"""asyncio TCP server that behaves like Crystal Lane's legacy TMS.

Protocol: see app/tms/protocol.py. Fault injection (TMS_FAULT_RATE) exercises the adapter's
timeouts, retries and record validation:

    slow         answer after 2 s (past the adapter's 1.5 s attempt timeout)
    drop         close the connection without answering (a BOOK is applied first: lost reply)
    garbage      answer with unparseable records / lines
    corrupt_one  truncate one LIST record (the adapter must skip it, not fail)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import random
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.logging_setup import log_event
from app.tms.protocol import CODE_TO_EQUIPMENT, EQUIPMENT_TO_CODE, TmsLoad, encode_record
from tms_sim.data import build_loads

log = logging.getLogger("clf.tms_sim")

FAULTS = ("slow", "drop", "garbage", "corrupt_one")
_IDLE_TIMEOUT_SECONDS = 30
_BUSINESS_TZ = ZoneInfo("America/Chicago")


class TmsSimulator:
    def __init__(self, token: str, *, fault_rate: float = 0.0, slow_seconds: float = 2.0,
                 rng: random.Random | None = None) -> None:
        if not token:
            raise ValueError("a TMS token is required")
        self._token = token.encode()
        self.fault_rate = fault_rate
        self.slow_seconds = slow_seconds
        self.forced_faults: deque[str | None] = deque()  # deterministic faults for tests/rehearsals
        self._rng = rng or random.Random()
        self.reset()

    def reset(self) -> None:
        now = datetime.now(timezone.utc)
        self.loads: dict[str, TmsLoad] = {load.load_id: load for load in build_loads(now)}
        self.bookings: dict[str, tuple[str, str]] = {}  # idempotency key -> (load_id, reference)
        self._seeded_on = now.astimezone(_BUSINESS_TZ).date()
        self._next_reference = self._rng.randint(70000, 79999)

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._handle, host, port, limit=8192)

    # --- connection handling ------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not await self._authenticate(reader, writer):
                return
            while True:
                line = await self._read_command(reader)
                if line is None or line == "QUIT":
                    return
                verb, _, args = line.partition(" ")
                if verb == "PING":
                    await self._send(writer, ["PONG"])
                    continue
                if verb == "RESET":
                    self.reset()
                    await self._send(writer, ["OK"])
                    continue
                if verb not in ("LIST", "BOOK"):
                    await self._send(writer, ["ERR BAD_COMMAND"])
                    continue
                fault = self._next_fault()
                response = self._list(args, fault) if verb == "LIST" else self._book(args, fault)
                if fault:
                    log_event(log, "tms_sim_fault", fault=fault, verb=verb)
                if fault == "drop":
                    return
                if fault == "slow":
                    await asyncio.sleep(self.slow_seconds)
                await self._send(writer, response)
        except (OSError, TimeoutError, ValueError):
            pass  # client went away, idled out, or sent an oversized line
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        line = await self._read_command(reader)
        ok = line is not None and line.startswith("AUTH ") and hmac.compare_digest(line[5:].encode(), self._token)
        await self._send(writer, ["OK" if ok else "ERR AUTH"])
        return ok

    @staticmethod
    async def _read_command(reader: asyncio.StreamReader) -> str | None:
        raw = await asyncio.wait_for(reader.readline(), _IDLE_TIMEOUT_SECONDS)
        return raw.decode("ascii", errors="replace").strip() if raw else None

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, lines: list[str]) -> None:
        writer.write("".join(f"{line}\r\n" for line in lines).encode("ascii"))
        await writer.drain()

    def _next_fault(self) -> str | None:
        if self.forced_faults:
            return self.forced_faults.popleft()
        if self.fault_rate and self._rng.random() < self.fault_rate:
            return self._rng.choice(FAULTS)
        return None

    # --- commands -----------------------------------------------------------------

    def _list(self, args: str, fault: str | None) -> list[str]:
        if datetime.now(timezone.utc).astimezone(_BUSINESS_TZ).date() != self._seeded_on:
            self.reset()  # new business day: fresh board
        code = args.strip()
        if code != "*" and code not in CODE_TO_EQUIPMENT:
            return ["ERR BAD_EQUIPMENT"]
        records = [
            encode_record(load)
            for load in self.loads.values()
            if load.status == "A" and (code == "*" or EQUIPMENT_TO_CODE[load.equipment_type] == code)
        ]
        if fault == "garbage":
            if not records:
                return ["OK ##"]
            records = [self._garbage() for _ in records]
        elif fault == "corrupt_one" and records:
            index = self._rng.randrange(len(records))
            records[index] = records[index][:-7]
        return [f"OK {len(records)}", *records, "END"]

    def _book(self, args: str, fault: str | None) -> list[str]:
        parts = args.split()
        if len(parts) != 4 or not parts[2].isdigit():
            return ["ERR BAD_REQUEST"]
        load_id, _mc_number, _rate_cents, key = parts
        if key in self.bookings:
            line = f"OK BOOKED {self.bookings[key][1]}"
        else:
            load = self.loads.get(load_id)
            if load is None:
                line = "ERR NOT_FOUND"
            elif load.status != "A":
                line = "OK UNAVAILABLE"
            else:
                reference = f"BK-{self._next_reference}"
                self._next_reference += 1
                self.loads[load_id] = replace(load, status="B")
                self.bookings[key] = (load_id, reference)
                line = f"OK BOOKED {reference}"
        if fault == "garbage":
            return ["OK B%%KED ??"]
        return [line]

    def _garbage(self) -> str:
        return "".join(self._rng.choice("#@%&*?!~") for _ in range(self._rng.randint(20, 60)))
