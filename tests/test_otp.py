"""OTP compliance rules."""

from __future__ import annotations

from tests.conftest import Call, failed, ok, wrong_code


def test_send_resend_then_limit(call: Call) -> None:
    call.verify_carrier()
    assert call.send_otp().json()["resends_remaining"] == 1
    assert call.send_otp().json() == ok(
        status="sent", masked_phone="***-***-4821", expires_in_seconds=300, resends_remaining=0,
    )
    assert call.send_otp().json() == ok(
        status="resend_limit_reached", masked_phone=None, expires_in_seconds=None, resends_remaining=0,
    )
    assert len(call.services.sms.inbox("123456")) == 2


def test_resend_invalidates_previous_code(call: Call) -> None:
    call.verify_carrier()
    call.send_otp()
    first = call.latest_code()
    call.send_otp()
    second = call.latest_code()
    if first == second:  # 1 in a million
        return
    assert call.verify_otp(first).json()["status"] == "incorrect"
    assert call.verify_otp(second).json() == ok(status="verified", attempts_remaining=1)


def test_three_wrong_codes_lock_the_call(call: Call) -> None:
    call.verify_carrier()
    call.send_otp()
    code = call.latest_code()
    bad = wrong_code(code)
    assert call.verify_otp(bad).json() == ok(status="incorrect", attempts_remaining=2)
    assert call.verify_otp(bad).json() == ok(status="incorrect", attempts_remaining=1)
    assert call.verify_otp(bad).json() == ok(status="attempts_exhausted", attempts_remaining=0)
    assert call.verify_otp(code).json() == ok(status="attempts_exhausted", attempts_remaining=0)
    failed(call.search(), "step_out_of_order")
    failed(call.send_otp(), "step_out_of_order")  # a resend can't unlock it


def test_code_expires_after_300_seconds(call: Call) -> None:
    call.verify_carrier()
    call.send_otp()
    code = call.latest_code()
    call.services.clock.advance(301)
    assert call.verify_otp(code).json() == ok(status="expired", attempts_remaining=3)
    call.send_otp()  # the one allowed resend
    assert call.verify_otp(call.latest_code()).json()["status"] == "verified"


def test_verify_before_send(call: Call) -> None:
    call.verify_carrier()
    assert call.verify_otp("123456").json() == ok(status="not_sent", attempts_remaining=3)


def test_not_authorized_carrier_gets_no_code(call: Call) -> None:
    call.mc = "222222"
    call.verify_carrier()
    body = call.send_otp().json()
    assert body["status"] == "not_authorized"
    assert call.services.sms.inbox("222222") == []


def test_no_phone_on_file(call: Call) -> None:
    call.mc = "246810"
    assert call.verify_carrier().json()["authorized"] is True
    assert call.send_otp().json()["status"] == "no_phone_on_file"


def test_bad_code_format_does_not_use_an_attempt(call: Call) -> None:
    call.verify_carrier()
    call.send_otp()
    assert failed(call.verify_otp("12345"), "invalid_request")["error_message"] == "code must be exactly 6 digits"
    assert call.verify_otp(call.latest_code()).json() == ok(status="verified", attempts_remaining=2)


def test_spoken_code_with_spaces_is_accepted(call: Call) -> None:
    call.verify_carrier()
    call.send_otp()
    code = call.latest_code()
    assert call.verify_otp(f"{code[:3]} {code[3:]}").json()["status"] == "verified"


def test_code_is_never_returned(call: Call) -> None:
    call.verify_carrier()
    call.send_otp()
    call.send_otp()
    code = call.latest_code()
    call.verify_otp(wrong_code(code))
    call.verify_otp(code)
    assert all(code not in text for text in call.responses)
