"""QA test carriers (demo mode only): a known MC + fixed OTP for automated test calls."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, load_settings
from app.logging_setup import JsonFormatter
from app.main import create_app
from tests.conftest import DEMO_KEY, Call, make_settings, ok, wrong_code

QA_MC, QA_CODE = "900001", "112233"


@pytest.fixture
def qa_client() -> TestClient:
    settings = make_settings(qa_test_carriers={QA_MC: QA_CODE})
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


def test_qa_carrier_completes_the_flow_with_its_fixed_code(qa_client: TestClient) -> None:
    call = Call(qa_client, mc=QA_MC)
    assert call.verify_carrier().json() == ok(
        authorized=True, carrier_name="QA Test Carrier LLC", authority_status="active", reason=None)
    sent = call.send_otp().json()
    assert sent["status"] == "sent" and sent["masked_phone"] == "***-***-0001"
    assert call.verify_otp(QA_CODE).json() == ok(status="verified", attempts_remaining=2)
    assert call.search().json()["status"] == "found"


def test_qa_carrier_still_obeys_every_otp_rule(qa_client: TestClient) -> None:
    call = Call(qa_client, mc=QA_MC)
    call.verify_carrier()
    assert call.verify_otp(QA_CODE).json()["status"] == "not_sent"  # must still be sent first
    call.send_otp()
    assert call.verify_otp(wrong_code(QA_CODE)).json()["status"] == "incorrect"
    call.services.clock.advance(301)
    assert call.verify_otp(QA_CODE).json()["status"] == "expired"
    call.send_otp()  # the one allowed resend
    assert call.verify_otp(QA_CODE).json() == ok(status="verified", attempts_remaining=1)


def test_other_carriers_keep_random_codes(qa_client: TestClient) -> None:
    codes = set()
    for _ in range(4):
        call = Call(qa_client)
        call.verify_carrier()
        call.send_otp()
        codes.add(call.latest_code())
    assert QA_CODE not in codes and len(codes) > 1


def test_code_can_be_fetched_by_call_id(qa_client: TestClient) -> None:
    regular, qa = Call(qa_client), Call(qa_client, mc=QA_MC)
    for call in (regular, qa):
        call.verify_carrier()
        call.send_otp()
    demo = {"X-Demo-Key": DEMO_KEY}
    mine = qa_client.get(f"/demo/api/phone?call_id={qa.call_id}", headers=demo).json()["messages"]
    assert [(m["call_id"], m["code"]) for m in mine] == [(qa.call_id, QA_CODE)]
    theirs = qa_client.get(f"/demo/api/phone?call_id={regular.call_id}", headers=demo).json()["messages"]
    assert theirs[0]["code"] == regular.latest_code()


def test_fixed_code_never_in_api_replies_or_logs(qa_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="clf")
    call = Call(qa_client, mc=QA_MC)
    call.verify_carrier()
    call.send_otp()
    call.verify_otp(QA_CODE)
    logs = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert QA_CODE not in "\n".join(call.responses)
    assert QA_CODE not in logs
    assert '"qa_fixed_code": true' in logs


def test_qa_carriers_parse_and_errors_hide_the_code() -> None:
    settings = load_settings({"QA_TEST_CARRIERS": "900001:112233, 900002:445566"})
    assert settings.qa_test_carriers == {"900001": "112233", "900002": "445566"}
    with pytest.raises(ConfigError) as excinfo:
        load_settings({"QA_TEST_CARRIERS": "900001:98765x"})
    assert "98765" not in str(excinfo.value)
    with pytest.raises(ConfigError):
        make_settings(env="production", qa_test_carriers={QA_MC: QA_CODE}).validate()
