"""End-to-end call flow and enforced step order."""

from __future__ import annotations

import pytest

from tests.conftest import Call, failed, ok


def test_happy_path_end_to_end(call: Call) -> None:
    verified = call.verify_carrier().json()
    assert verified == ok(
        authorized=True,
        carrier_name="Blue Ridge Transport LLC",
        authority_status="active",
        reason=None,
    )

    sent = call.send_otp().json()
    assert sent == ok(status="sent", masked_phone="***-***-4821", expires_in_seconds=300, resends_remaining=1)
    assert call.verify_otp(call.latest_code()).json() == ok(status="verified", attempts_remaining=2)

    search = call.search().json()
    assert search["status"] == "found" and search["ok"] is True
    assert 1 <= len(search["loads"]) <= 3
    top = search["loads"][0]
    assert top["load_id"] == "LD-10482"
    assert top["loadboard_rate"] == 2100
    assert top["origin"] == "Joliet, IL" and top["destination"] == "Dallas, TX"

    first_response = call.offer("LD-10482", 2600, 1)
    first = first_response.json()
    assert first == ok(decision="counter", counter_rate=2200, rounds_remaining=2)
    # Whole dollars serialize as integers ("2200", not "2200.0"): the agent reads them aloud.
    assert '"counter_rate":2200,' in first_response.text
    assert isinstance(top["loadboard_rate"], int)
    second = call.offer("LD-10482", 2500, 2).json()
    assert second == ok(decision="counter", counter_rate=2300, rounds_remaining=1)

    booked = call.book("LD-10482", 2300).json()
    assert booked["status"] == "booked" and booked["ok"] is True
    assert booked["booking_reference"].startswith("BK-")

    handoff = call.handoff("LD-10482", 2300).json()
    assert handoff["status"] == "queued" and handoff["ok"] is True
    assert handoff["handoff_id"].startswith("HO-")
    assert handoff["queue_position"] >= 1


def test_load_board_rate_can_be_booked_without_negotiation(call: Call) -> None:
    call.authenticate()
    load = call.search().json()["loads"][0]
    assert call.book(load["load_id"], load["loadboard_rate"]).json()["status"] == "booked"


@pytest.mark.parametrize(
    ("mc", "status", "reason"),
    [
        ("111111", "inactive", "authority_inactive"),
        ("222222", "revoked", "authority_revoked"),
        ("00000001", "not_found", "carrier_not_found"),
    ],
)
def test_unauthorized_carriers(call: Call, mc: str, status: str, reason: str) -> None:
    body = call.verify_carrier(mc).json()
    assert body == ok(authorized=False, carrier_name=None, authority_status=status, reason=reason)


def test_fmcsa_outage_is_a_retryable_failure(call: Call) -> None:
    body = failed(call.verify_carrier("999999"), "fmcsa_unavailable")
    assert body["retryable"] is True and body["error_message"]
    # Unknown, not "false": the agent must not tell the carrier they're unauthorized.
    assert body["authorized"] is None and body["authority_status"] is None


def test_steps_out_of_order_are_409(call: Call) -> None:
    def assert_409(response) -> None:
        assert failed(response, "step_out_of_order")["retryable"] is False

    assert_409(call.send_otp())
    assert_409(call.verify_otp("123456"))
    assert_409(call.search())
    assert_409(call.offer("LD-10482", 2000))
    assert_409(call.book("LD-10482", 2100))
    assert_409(call.handoff("LD-10482", 2100))

    call.verify_carrier()
    assert_409(call.search())  # verified carrier, but OTP not verified yet
    call.send_otp()
    call.verify_otp(call.latest_code())

    assert_409(call.offer("LD-10482", 2000))  # not offered on this call yet
    assert_409(call.book("LD-10482", 2100))
    call.search()
    assert_409(call.handoff("LD-10482", 2100))  # not booked yet
    assert_409(call.offer("LD-99999", 2000))  # never offered


def test_mc_number_must_match_verified_carrier(call: Call) -> None:
    call.verify_carrier("123456")
    failed(call.post("/otp/send", {"mc_number": "654321"}), "step_out_of_order")


def test_cannot_switch_carrier_after_otp(call: Call) -> None:
    call.authenticate()
    failed(call.verify_carrier("654321"), "step_out_of_order")


def test_otp_state_is_per_call(client) -> None:
    first, second = Call(client), Call(client)
    first.authenticate()
    second.verify_carrier()
    failed(second.search(), "step_out_of_order")


def test_no_match(call: Call) -> None:
    call.authenticate()
    body = call.search(origin="Boise, ID", destination=None).json()
    assert body == ok(status="no_match", loads=[])


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_optional_search_fields_mean_no_preference(call: Call, blank) -> None:
    # HappyRobot always sends every field, empty when the carrier has no preference.
    call.authenticate()
    body = {"mc_number": call.mc, "origin": "Joliet, IL", "equipment_type": "dry_van",
            "destination": blank, "pickup_date": blank}
    response = call.post("/loads/search", body).json()
    assert response["ok"] is True and response["status"] == "found", response
    assert response["loads"] == call.search(destination=None).json()["loads"]


def test_blank_required_search_field_is_reported(call: Call) -> None:
    call.authenticate()
    assert failed(call.search(origin=""), "invalid_request")["error_message"] == "origin is required"


def test_search_understands_spoken_places(call: Call) -> None:
    call.authenticate()
    body = call.search(origin="near Chicago", destination="Texas", equipment_type="Dry Van").json()
    ids = [load["load_id"] for load in body["loads"]]
    assert body["status"] == "found"
    assert set(ids) <= {"LD-10482", "LD-10542", "LD-10546", "LD-10487"}
    assert "LD-10499" not in ids  # already covered in the TMS


def test_search_filters_equipment(call: Call) -> None:
    call.authenticate()
    loads = call.search(equipment_type="reefer").json()["loads"]
    assert loads and all(load["equipment_type"] == "reefer" for load in loads)
    assert loads[0]["load_id"] == "LD-10539"
