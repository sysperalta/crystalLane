"""TCP client for the legacy TMS, implementing x-tms-integration from the spec:

- 1.5 s timeout per attempt, max 3 attempts, backoff 0.2 s then 0.5 s, 4 s total budget
- fresh connection per request, closed cleanly
- every fixed-width record is validated; bad records are skipped; a response with no
  valid record is retried, then surfaced as tms_malformed_response
- BOOK carries an idempotency key, so retrying a booking can never double-book
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.logging_setup import log_event
from app.tms.protocol import BOOKING_REF_RE, EQUIPMENT_TO_CODE, MalformedRecord, TmsLoad, parse_record

log = logging.getLogger("clf.tms")

T = TypeVar("T")
_MAX_RECORDS = 5000


class TmsUnavailable(Exception):
    def __init__(self, attempts: int) -> None:
        super().__init__(f"TMS failed after {attempts} attempts")
        self.attempts = attempts


class TmsMalformedResponse(TmsUnavailable):
    pass


class _Retryable(Exception):
    pass


class _Malformed(_Retryable):
    pass


class _AuthRejected(Exception):
    pass


@dataclass(frozen=True)
class BookResult:
    status: str  # "booked" | "unavailable"
    reference: str | None


class TmsClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        attempt_timeout: float = 1.5,
        max_attempts: int = 3,
        backoff: tuple[float, ...] = (0.2, 0.5),
        total_budget: float = 4.0,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._attempt_timeout = attempt_timeout
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._total_budget = total_budget

    # --- operations ---------------------------------------------------------------

    async def list_available(self, equipment_type: str) -> list[TmsLoad]:
        command = f"LIST {EQUIPMENT_TO_CODE[equipment_type]}"
        return await self._with_retries("list", command, self._read_list)

    async def book(self, load_id: str, mc_number: str, rate_cents: int, idempotency_key: str) -> BookResult:
        command = f"BOOK {load_id} {mc_number} {rate_cents} {idempotency_key}"
        return await self._with_retries("book", command, self._read_book)

    async def ping(self, timeout: float = 1.0) -> bool:
        async def read(reader: asyncio.StreamReader) -> bool:
            return await self._readline(reader) == "PONG"

        try:
            return await asyncio.wait_for(self._session("PING", read), timeout)
        except Exception:
            return False

    async def reset(self) -> bool:
        """Demo only: ask the simulator to restore its seed data."""

        async def read(reader: asyncio.StreamReader) -> bool:
            return await self._readline(reader) == "OK"

        try:
            return await asyncio.wait_for(self._session("RESET", read), 2.0)
        except Exception:
            return False

    # --- retry policy ---------------------------------------------------------------

    async def _with_retries(
        self, op: str, command: str, read_response: Callable[[asyncio.StreamReader], Awaitable[T]]
    ) -> T:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_budget
        attempts = 0
        malformed_last = False
        for attempt in range(1, self._max_attempts + 1):
            remaining = deadline - loop.time()
            if remaining <= 0.05:
                break
            attempts = attempt
            try:
                return await asyncio.wait_for(
                    self._session(command, read_response),
                    timeout=min(self._attempt_timeout, remaining),
                )
            except _AuthRejected:
                log_event(log, "tms_auth_rejected", logging.ERROR, op=op)
                raise TmsUnavailable(attempt) from None
            except _Malformed as exc:
                malformed_last, reason = True, str(exc)
            except (_Retryable, OSError, TimeoutError) as exc:
                malformed_last, reason = False, str(exc) or type(exc).__name__
            log_event(log, "tms_attempt_failed", logging.WARNING, op=op, attempt=attempt, reason=reason)
            if attempt < self._max_attempts:
                delay = self._backoff[min(attempt - 1, len(self._backoff) - 1)]
                if loop.time() + delay >= deadline:
                    break
                await asyncio.sleep(delay)
        log_event(log, "tms_gave_up", logging.ERROR, op=op, attempts=attempts, malformed=malformed_last)
        if malformed_last:
            raise TmsMalformedResponse(attempts)
        raise TmsUnavailable(attempts)

    # --- one TCP session ----------------------------------------------------------

    async def _session(self, command: str, read_response: Callable[[asyncio.StreamReader], Awaitable[T]]) -> T:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            await self._send(writer, f"AUTH {self._token}")
            if await self._readline(reader) != "OK":
                raise _AuthRejected()
            await self._send(writer, command)
            result = await read_response(reader)
            with contextlib.suppress(OSError):
                await self._send(writer, "QUIT")
            return result
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 0.25)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, line: str) -> None:
        writer.write(line.encode("ascii") + b"\r\n")
        await writer.drain()

    @staticmethod
    async def _readline(reader: asyncio.StreamReader) -> str:
        try:
            raw = await reader.readline()
        except ValueError:  # line longer than the stream limit
            raise _Malformed("oversized line") from None
        if not raw:
            raise _Retryable("connection closed by TMS")
        if not raw.endswith(b"\n"):
            raise _Retryable("truncated response")
        return raw.decode("ascii", errors="replace").rstrip("\r\n")

    async def _read_list(self, reader: asyncio.StreamReader) -> list[TmsLoad]:
        header = (await self._readline(reader)).split()
        if len(header) != 2 or header[0] != "OK" or not header[1].isdigit():
            if header[:1] == ["ERR"]:
                raise _Retryable("TMS returned an error")
            raise _Malformed("bad LIST header")
        count = int(header[1])
        if count > _MAX_RECORDS:
            raise _Malformed("implausible record count")
        loads: list[TmsLoad] = []
        skipped: list[str] = []
        for _ in range(count):
            line = await self._readline(reader)
            try:
                loads.append(parse_record(line))
            except MalformedRecord as exc:
                skipped.append(str(exc))
        if await self._readline(reader) != "END":
            raise _Malformed("missing END marker")
        if count and not loads:
            raise _Malformed("every record malformed")
        if skipped:
            log_event(log, "tms_records_skipped", logging.WARNING, skipped=len(skipped), reasons=sorted(set(skipped)))
        return loads

    async def _read_book(self, reader: asyncio.StreamReader) -> BookResult:
        parts = (await self._readline(reader)).split()
        if len(parts) == 3 and parts[:2] == ["OK", "BOOKED"] and BOOKING_REF_RE.match(parts[2]):
            return BookResult("booked", parts[2])
        if parts in (["OK", "UNAVAILABLE"], ["ERR", "NOT_FOUND"]):
            return BookResult("unavailable", None)
        raise _Malformed("bad BOOK response")
