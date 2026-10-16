"""Demo console access rules and startup configuration checks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, load_settings
from app.deps import Services
from app.main import create_app
from tests.conftest import API_KEY, DEMO_KEY, PHONE, Call, make_settings

DEMO = {"X-Demo-Key": DEMO_KEY}


def test_console_needs_its_own_key(client: TestClient) -> None:
    assert client.get("/demo/api/calls").status_code == 401
    assert client.get("/demo/api/calls", headers={"X-Demo-Key": API_KEY}).status_code == 401
    assert client.get("/demo/api/calls", headers=DEMO).status_code == 200


def test_console_page_is_served(client: TestClient) -> None:
    response = client.get("/demo")
    assert response.status_code == 200
    assert "Carrier phone" in response.text


def test_handset_shows_code_but_timeline_never_does(call: Call, client: TestClient) -> None:
    call.verify_carrier()
    call.send_otp()
    code = call.latest_code()
    message = client.get("/demo/api/phone?mc=123456", headers=DEMO).json()["messages"][0]
    assert code in message["body"]
    assert message["to"] == "***-***-4821"
    timeline = client.get("/demo/api/calls", headers=DEMO).text
    assert code not in timeline and PHONE not in timeline


def test_reset_restores_the_board(call: Call, client: TestClient, services: Services) -> None:
    call.authenticate()
    call.search()
    call.book("LD-10482", 2100)
    assert services.tms_sim.loads["LD-10482"].status == "B"
    assert client.post("/demo/api/reset", headers=DEMO).json() == {"status": "reset", "tms_reset": True}
    assert services.tms_sim.loads["LD-10482"].status == "A"
    assert services.calls.recent() == []


def test_openapi_document_renders(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "X-API-Key"
    load_schema = next(v for k, v in spec["components"]["schemas"].items() if k.startswith("Load"))
    assert "max_rate" not in load_schema["properties"]
    assert {op["post"]["operationId"] for path, op in spec["paths"].items() if "post" in op} == {
        "verifyCarrier", "sendOtp", "verifyOtp", "searchLoads", "evaluateOffer", "bookLoad", "handoff"}


def test_contract_docs_are_public(client: TestClient) -> None:
    spec = client.get("/openapi.yaml")
    assert spec.status_code == 200
    assert spec.headers["content-type"].startswith("application/yaml")
    assert spec.text.startswith("openapi: 3.1.0")
    # This deployment is listed first so Swagger UI's "Try it out" calls it, not the example host.
    assert "servers:\n  - url: /v1\n    description: This deployment\n  - url: https://{host}/v1" in spec.text
    for page in ("/docs", "/redoc"):
        html = client.get(page)
        assert html.status_code == 200 and "/openapi.yaml" in html.text
    root = client.get("/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/docs"


def test_console_absent_without_key() -> None:
    with TestClient(create_app(make_settings(demo_console_key=None))) as client:
        assert client.get("/demo").status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"env": "production"},
        {"api_keys": ()},
        {"api_keys": ("too-short",)},
        {"tms_token": ""},
        {"tms_fault_rate": 1.5},
        {"demo_console_key": API_KEY},
    ],
)
def test_unsafe_configuration_is_refused(overrides: dict) -> None:
    with pytest.raises(ConfigError):
        make_settings(**overrides).validate()


def test_load_settings_from_environment() -> None:
    settings = load_settings({
        "API_KEY": "key-one-0123456789, key-two-0123456789",
        "TMS_TOKEN": "tok",
        "DEMO_CARRIERS": "123456:+15551234567, 654321:+15559876543",
        "TMS_FAULT_RATE": "0.3",
    })
    assert settings.api_keys == ("key-one-0123456789", "key-two-0123456789")
    assert settings.demo_carriers == {"123456": "+15551234567", "654321": "+15559876543"}
    assert settings.tms_embedded_sim is True  # no TMS_HOST -> run the simulator in-process
    assert load_settings({"TMS_HOST": "tms-sim"}).tms_embedded_sim is False


def test_bad_demo_carriers_error_hides_the_phone() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({"DEMO_CARRIERS": "123456:+15551234567,654321-15559876543"})
    assert "5559876543" not in str(excinfo.value)
