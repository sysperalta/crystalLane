"""Auth, input validation, rate limiting and the never-return/never-log list."""

from __future__ import annotations

import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.config import VERSION
from app.logging_setup import JsonFormatter
from app.main import create_app
from tests.conftest import API_KEY, PHONE, Call, failed, make_settings, ok, wrong_code

AUTH = {"X-API-Key": API_KEY}


def test_health_requires_api_key(client: TestClient) -> None:
    empty = {"status": None, "tms": None, "fmcsa": None, "version": None}
    for headers in ({}, {"X-API-Key": "wrong-key-wrong-key"}):
        response = client.get("/v1/health", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"ok": False, "error_code": "unauthorized", "error_message": "Invalid API key", "retryable": False, **empty}
    response = client.get("/v1/health", headers=AUTH)
    assert response.json() == ok(status="ok", tms="reachable", fmcsa="reachable", version=VERSION)


def test_liveness_probe_is_public_and_says_nothing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_auth_is_checked_before_anything_else(client: TestClient) -> None:
    response = client.post("/v1/carriers/verify", json={"bogus": True})
    assert response.status_code == 401


def test_call_id_required_and_validated(client: TestClient) -> None:
    body = {"mc_number": "123456"}
    response = client.post("/v1/carriers/verify", json=body, headers=AUTH)
    assert failed(response, "invalid_request")["error_message"] == "X-Call-Id header is required"
    response = client.post("/v1/carriers/verify", json=body, headers={**AUTH, "X-Call-Id": "bad id\twith spaces"})
    failed(response, "invalid_request")


def test_api_key_rotation_accepts_both_keys() -> None:
    second = "next-api-key-9876543210"
    with TestClient(create_app(make_settings(api_keys=(API_KEY, second)))) as client:
        for key in (API_KEY, second):
            assert client.get("/v1/health", headers={"X-API-Key": key}).status_code == 200


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"mc_number": "MC123456"}, "mc_number must contain digits only (1-8 digits)"),
        ({"mc_number": "123456789"}, "mc_number must contain digits only (1-8 digits)"),
        ({}, "mc_number is required"),
        ({"mc_number": ""}, "mc_number is required"),
        ({"mc_number": "   "}, "mc_number is required"),
        ({"mc_number": None}, "mc_number is required"),
        ({"mc_number": "123456", "phone": "+15550000000"}, "phone is not an accepted field"),
    ],
)
def test_invalid_input_is_a_200_failure(call: Call, body: dict, message: str) -> None:
    # HappyRobot collapses non-2xx replies into one text blob, so input errors are HTTP 200, ok=false.
    response = call.post("/carriers/verify", body)
    assert response.status_code == 200
    assert response.text.startswith('{"ok":false,')
    assert response.json() == {"ok": False, "error_code": "invalid_request", "error_message": message, "retryable": False,
                               "authorized": None, "carrier_name": None, "authority_status": None, "reason": None}


def test_rejections_are_logged_with_their_reason(call: Call, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="clf")
    call.verify_carrier("MC123456")
    call.client.post("/v1/carriers/verify", json={"mc_number": "123456"}, headers={"X-Call-Id": call.call_id})
    lines = [r.fields for r in caplog.records if r.getMessage() == "request" and r.fields.get("call_id") == call.call_id]
    assert {"status": 200, "error_status": 400, "error_code": "invalid_request",
            "error_message": "mc_number must contain digits only (1-8 digits)"}.items() <= lines[0].items()
    assert {"status": 401, "error_status": 401, "error_code": "unauthorized",
            "error_message": "Invalid API key"}.items() <= lines[1].items()
    assert "MC123456" not in str(lines)


def test_validation_errors_never_echo_input(call: Call) -> None:
    call.verify_carrier()
    response = call.post("/otp/verify", {"mc_number": "123456", "code": "98765x"})
    failed(response, "invalid_request")
    assert "98765" not in response.text


@pytest.mark.parametrize("mc", ["123 456", "123-456", 123456])
def test_mc_number_tolerates_voice_formatting(call: Call, mc) -> None:
    assert call.post("/carriers/verify", {"mc_number": mc}).json()["authorized"] is True


def test_rate_limit_is_per_call(call: Call) -> None:
    for _ in range(60):
        assert call.verify_carrier().status_code == 200
    response = call.verify_carrier()
    assert failed(response, "rate_limited")["retryable"] is True
    assert int(response.headers["retry-after"]) >= 1
    assert Call(call.client).verify_carrier().status_code == 200


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/v1/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error_code": "invalid_request", "error_message": "Not found", "retryable": False}


def test_secrets_never_in_responses_or_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="clf")
    call = Call(client, call_id="leak-test-call")
    call.verify_carrier()
    call.send_otp()
    code = call.latest_code()
    call.verify_otp(wrong_code(code))
    call.verify_otp(code)
    search = call.search().json()
    call.offer("LD-10482", 2600)
    call.offer("LD-10482", 2500)
    call.book("LD-10482", 2300)
    call.handoff("LD-10482", 2300)

    allowed = {"load_id", "origin", "destination", "pickup_datetime", "delivery_datetime", "equipment_type",
               "loadboard_rate", "weight", "commodity_type", "num_of_pieces", "miles", "dimensions", "notes"}
    assert all(set(load) == allowed for load in search["loads"])

    formatter = JsonFormatter()
    logs = "\n".join(formatter.format(record) for record in caplog.records)
    responses = "\n".join(call.responses)
    assert "otp_sent" in logs and "booking_result" in logs  # the logs really were captured
    for haystack in (logs, responses):
        assert code not in haystack
        assert PHONE not in haystack and PHONE[2:] not in haystack
        assert API_KEY not in haystack
        assert "test-tms-token-000" not in haystack
        assert "max_rate" not in haystack
        # LD-10482's ceiling ($2,400) never surfaces unless a round-3 counter offers it.
        assert re.search(r"(?<!\d)2400(?!\d)", haystack) is None
        assert "240000" not in haystack
