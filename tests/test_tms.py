"""Legacy TMS integration: fixed-width parsing, retries, timeouts, idempotent booking."""

from __future__ import annotations

import socket
import time

import pytest
from fastapi.testclient import TestClient

from app.deps import Services
from app.services.matching import parse_place
from app.tms.client import TmsClient
from app.tms.protocol import FIELDS, RECORD_LENGTH, MalformedRecord, encode_record, parse_record
from tests.conftest import API_KEY, Call, failed, ok
from tms_sim.data import build_loads


def _set_field(line: str, name: str, value: str) -> str:
    start = 0
    for field, width in FIELDS:
        if field == name:
            return line[:start] + value.ljust(width)[:width] + line[start + width:]
        start += width
    raise KeyError(name)


def test_records_round_trip() -> None:
    for load in build_loads():
        line = encode_record(load)
        assert len(line) == RECORD_LENGTH
        assert parse_record(line) == load


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda s: s[:-1],
        lambda s: s + " ",
        lambda s: _set_field(s, "load_id", "XX-10482"),
        lambda s: _set_field(s, "status", "?"),
        lambda s: _set_field(s, "pickup", "2026-13-45 99:99"),
        lambda s: _set_field(s, "equipment", "ZZ"),
        lambda s: _set_field(s, "loadboard_cents", "0021A0000"),
        lambda s: _set_field(s, "max_rate_cents", "000000100"),  # below loadboard
        lambda s: _set_field(s, "weight_lbs", "-000001"),
        lambda s: _set_field(s, "origin", "Joliet"),
        lambda s: s[:50] + "\x00" + s[51:],
    ],
)
def test_malformed_records_are_rejected(corrupt) -> None:
    line = encode_record(build_loads()[0])
    with pytest.raises(MalformedRecord):
        parse_record(corrupt(line))


def test_every_seed_city_is_geocoded() -> None:
    for load in build_loads():
        assert parse_place(load.origin).coords, load.origin
        assert parse_place(load.destination).coords, load.destination


@pytest.fixture
def ready(call: Call) -> Call:
    call.authenticate()
    return call


def test_search_survives_dropped_and_garbled_responses(ready: Call, services: Services) -> None:
    services.tms_sim.forced_faults.extend(["drop", "garbage"])
    response = ready.search()
    assert response.status_code == 200
    assert response.json()["status"] == "found"
    assert not services.tms_sim.forced_faults


def test_one_corrupt_record_is_skipped(ready: Call, services: Services) -> None:
    services.tms_sim.forced_faults.append("corrupt_one")
    response = ready.search(origin="Chicago, IL", destination=None)
    assert response.status_code == 200
    assert response.json()["status"] == "found"


def test_slow_tms_returns_503_within_budget(ready: Call, services: Services) -> None:
    services.tms_sim.forced_faults.extend(["slow"] * 3)
    started = time.monotonic()
    response = ready.search()
    elapsed = time.monotonic() - started
    assert failed(response, "tms_unavailable") == {
        "ok": False, "error_code": "tms_unavailable", "error_message": "TMS did not respond after 3 attempts",
        "retryable": True, "status": None, "loads": []}
    assert 3.5 < elapsed < 4.8


def test_garbage_every_time_is_malformed_503(ready: Call, services: Services) -> None:
    services.tms_sim.forced_faults.extend(["garbage"] * 3)
    assert failed(ready.search(), "tms_malformed_response")["retryable"] is True


def test_lost_booking_reply_is_retried_without_double_booking(ready: Call, services: Services) -> None:
    ready.search()
    services.tms_sim.forced_faults.append("drop")  # booking applied, reply lost
    first = ready.book("LD-10482", 2100).json()
    assert first["status"] == "booked"
    assert [ref for load_id, ref in services.tms_sim.bookings.values() if load_id == "LD-10482"] == [
        first["booking_reference"]
    ]
    assert ready.book("LD-10482", 2100).json() == first  # same call + load -> same booking


def test_two_carriers_cannot_book_the_same_load(client: TestClient) -> None:
    first, second = Call(client, mc="123456"), Call(client, mc="654321")
    for c in (first, second):
        c.authenticate()
        c.search()
    assert first.book("LD-10482", 2100).json()["status"] == "booked"
    assert second.book("LD-10482", 2100).json() == ok(status="unavailable", booking_reference=None)


def test_booked_load_disappears_from_search(client: TestClient) -> None:
    first, second = Call(client), Call(client, mc="654321")
    first.authenticate()
    first.search()
    first.book("LD-10482", 2100)
    second.authenticate()
    ids = [load["load_id"] for load in second.search().json()["loads"]]
    assert "LD-10482" not in ids


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_degraded_when_tms_unreachable(client: TestClient, services: Services) -> None:
    services.tms = TmsClient("127.0.0.1", _free_port(), "irrelevant-token")
    body = client.get("/v1/health", headers={"X-API-Key": API_KEY}).json()
    assert body["status"] == "degraded" and body["tms"] == "unreachable"


def test_wrong_tms_token_fails_fast(ready: Call, services: Services) -> None:
    services.tms = TmsClient("127.0.0.1", services.tms._port, "wrong-token")
    started = time.monotonic()
    failed(ready.search(), "tms_unavailable")
    assert time.monotonic() - started < 1.0  # auth rejection isn't retried
