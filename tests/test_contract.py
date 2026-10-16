"""One shape per endpoint: HappyRobot's agent reads responses with an LLM, so an endpoint must
answer with exactly the same keys whether it succeeds or fails."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.config import VERSION
from app.models import (
    BookLoadResponse,
    EvaluateOfferResponse,
    HandoffResponse,
    HealthResponse,
    SearchLoadsResponse,
    SendOtpResponse,
    VerifyCarrierResponse,
    VerifyOtpResponse,
)
from tests.conftest import API_KEY, ENVELOPE_KEYS, Call

ENDPOINTS: list[tuple[str, type[BaseModel]]] = [
    ("/carriers/verify", VerifyCarrierResponse),
    ("/otp/send", SendOtpResponse),
    ("/otp/verify", VerifyOtpResponse),
    ("/loads/search", SearchLoadsResponse),
    ("/offers/evaluate", EvaluateOfferResponse),
    ("/loads/LD-10482/book", BookLoadResponse),
    ("/handoff", HandoffResponse),
]


def _keys(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def _assert_success(body: dict[str, Any], model: type[BaseModel]) -> None:
    assert set(body) == _keys(model)
    assert list(body)[0] == "ok", "every reply starts with ok"
    assert body["ok"] is True and body["error_code"] is None and body["error_message"] is None
    assert body["retryable"] is False


def _assert_error(body: dict[str, Any], model: type[BaseModel], error: str) -> None:
    assert set(body) == _keys(model), f"{error}: keys differ from the success shape"
    assert list(body)[0] == "ok", "every reply starts with ok"
    assert body["ok"] is False and body["error_code"] == error
    assert isinstance(body["error_message"], str) and body["error_message"]
    business = {k: v for k, v in body.items() if k not in ENVELOPE_KEYS}
    assert all(v in (None, []) for v in business.values()), business


def test_every_endpoint_succeeds_in_its_envelope(client: TestClient) -> None:
    flow = Call(client)
    responses = [
        flow.verify_carrier(),
        flow.send_otp(),
        flow.verify_otp(flow.latest_code()),
        flow.search(),
        flow.offer("LD-10482", 2600),
        flow.book("LD-10482", 2200),
        flow.handoff("LD-10482", 2200),
    ]
    for (path, model), response in zip(ENDPOINTS, responses):
        assert response.status_code == 200, path
        _assert_success(response.json(), model)
    health = client.get("/v1/health", headers={"X-API-Key": API_KEY})
    _assert_success(health.json(), HealthResponse)


@pytest.mark.parametrize(("path", "model"), ENDPOINTS)
def test_errors_keep_the_same_shape(client: TestClient, path: str, model: type[BaseModel]) -> None:
    unauthenticated = client.post(f"/v1{path}", json={}, headers={"X-Call-Id": "shape-check"})
    assert unauthenticated.status_code == 401
    _assert_error(unauthenticated.json(), model, "unauthorized")

    caller = Call(client)
    invalid = caller.post(path, {"unexpected": True})
    assert invalid.status_code == 200  # call-time failures are 200 so HappyRobot can read the fields
    _assert_error(invalid.json(), model, "invalid_request")

    if path != "/carriers/verify":  # every later step needs an earlier one
        bodies = {
            "/otp/send": {"mc_number": "123456"},
            "/otp/verify": {"mc_number": "123456", "code": "123456"},
            "/loads/search": {"mc_number": "123456", "origin": "Joliet, IL", "equipment_type": "dry_van"},
            "/offers/evaluate": {"load_id": "LD-10482", "mc_number": "123456", "carrier_offer": 2600, "round": 1},
            "/loads/LD-10482/book": {"mc_number": "123456", "agreed_rate": 2100},
            "/handoff": {"load_id": "LD-10482", "mc_number": "123456", "agreed_rate": 2100},
        }
        out_of_order = caller.post(path, bodies[path])
        assert out_of_order.status_code == 200
        _assert_error(out_of_order.json(), model, "step_out_of_order")


@pytest.mark.parametrize("path", ["/v1/loads//book", "/v1/loads/book", "/v1/loads/%20/book"])
def test_blank_load_id_is_answered_in_book_loads_shape(client: TestClient, path: str) -> None:
    # HappyRobot fills /loads/{load_id}/book from a variable; a blank one must not become a bare 404.
    body = {"mc_number": "123456", "agreed_rate": 2100}
    response = client.post(path, json=body, headers={"X-API-Key": API_KEY, "X-Call-Id": "blank-load-id"})
    assert response.status_code == 200
    _assert_error(response.json(), BookLoadResponse, "invalid_request")
    assert response.json()["error_message"] == "load_id is required"
    assert client.post(path, json=body, headers={"X-Call-Id": "blank-load-id"}).status_code == 401


def test_health_error_keeps_the_same_shape(client: TestClient) -> None:
    _assert_error(client.get("/v1/health").json(), HealthResponse, "unauthorized")


def test_rate_limit_keeps_the_same_shape(call: Call) -> None:
    for _ in range(60):
        call.verify_carrier()
    response = call.verify_carrier()
    assert response.status_code == 200 and int(response.headers["retry-after"]) >= 1
    _assert_error(response.json(), VerifyCarrierResponse, "rate_limited")


def test_only_a_wrong_api_key_is_not_200(client: TestClient) -> None:
    upstream_down = Call(client).verify_carrier("999999")
    assert upstream_down.status_code == 200
    _assert_error(upstream_down.json(), VerifyCarrierResponse, "fmcsa_unavailable")
    assert client.get("/v1/health", headers={"X-API-Key": "wrong-key-wrong-key"}).status_code == 401


def test_contract_file_matches_the_code(client: TestClient) -> None:
    spec = yaml.safe_load(client.get("/openapi.yaml").text)
    schemas = spec["components"]["schemas"]
    for model in [m for _, m in ENDPOINTS] + [HealthResponse]:
        documented = schemas[model.__name__]
        assert set(documented["properties"]) == _keys(model), model.__name__
        assert set(documented["required"]) == _keys(model), f"{model.__name__}: every key is always present"
    assert spec["info"]["version"] == VERSION
    # Every documented error response of an endpoint uses that endpoint's own envelope.
    for path, operations in spec["paths"].items():
        for operation in operations.values():
            refs = {r["content"]["application/json"]["schema"]["$ref"] for r in operation["responses"].values()}
            assert len(refs) == 1, f"{path}: success and error responses must share one schema"
