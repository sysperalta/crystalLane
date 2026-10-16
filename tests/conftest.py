from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.deps import Services
from app.main import create_app

API_KEY = "test-api-key-0123456789"
DEMO_KEY = "test-demo-console-key-0123"
PHONE = "+15551234821"
OTHER_PHONE = "+15559876543"

ENVELOPE_KEYS = {"ok", "error_code", "error_message", "retryable"}
NO_ERROR = {"ok": True, "error_code": None, "error_message": None, "retryable": False}


def ok(**fields: Any) -> dict[str, Any]:
    """Expected success body: the endpoint's own fields plus an empty error envelope."""
    return {**NO_ERROR, **fields}


def failed(response: Any, error_code: str) -> dict[str, Any]:
    """Assert a call-time failure (HTTP 200, ok=false, this error_code) and return the body."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["error_code"] == error_code, body
    return body


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        env="test",
        api_keys=(API_KEY,),
        tms_token="test-tms-token-000",
        tms_embedded_sim=True,
        tms_port=0,
        tms_fault_rate=0.0,
        demo_carriers={"123456": PHONE, "654321": OTHER_PHONE},
        demo_console_key=DEMO_KEY,
        fmcsa_latency=(0.0, 0.0),
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(make_settings()), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def services(client: TestClient) -> Services:
    return client.app.state.services


class Call:
    """Drives one HappyRobot call (one X-Call-Id) against the API, like the workflow would."""

    def __init__(self, client: TestClient, mc: str = "123456", call_id: str | None = None) -> None:
        self.client = client
        self.mc = mc
        self.call_id = call_id or f"test-{uuid.uuid4()}"
        self.responses: list[str] = []

    @property
    def services(self) -> Services:
        return self.client.app.state.services

    def post(self, path: str, body: dict[str, Any]):
        response = self.client.post(
            f"/v1{path}", json=body, headers={"X-API-Key": API_KEY, "X-Call-Id": self.call_id}
        )
        self.responses.append(response.text)
        return response

    def verify_carrier(self, mc: str | None = None):
        return self.post("/carriers/verify", {"mc_number": mc or self.mc})

    def send_otp(self):
        return self.post("/otp/send", {"mc_number": self.mc})

    def latest_code(self) -> str:
        message = self.services.sms.inbox(self.mc)[0]
        return re.search(r"\b(\d{6})\b", message.body).group(1)

    def verify_otp(self, code: str):
        return self.post("/otp/verify", {"mc_number": self.mc, "code": code})

    def authenticate(self) -> None:
        assert self.verify_carrier().json()["authorized"] is True
        assert self.send_otp().json()["status"] == "sent"
        assert self.verify_otp(self.latest_code()).json()["status"] == "verified"

    def search(self, origin: str = "Joliet, IL", destination: str | None = "Dallas, TX",
               equipment_type: str = "dry_van", **extra: Any):
        body = {"mc_number": self.mc, "origin": origin, "equipment_type": equipment_type, **extra}
        if destination is not None:
            body["destination"] = destination
        return self.post("/loads/search", body)

    def offer(self, load_id: str, amount: float, round_no: int = 1):
        return self.post("/offers/evaluate",
                         {"load_id": load_id, "mc_number": self.mc, "carrier_offer": amount, "round": round_no})

    def book(self, load_id: str, rate: float):
        return self.post(f"/loads/{load_id}/book", {"mc_number": self.mc, "agreed_rate": rate})

    def handoff(self, load_id: str, rate: float):
        return self.post("/handoff", {"load_id": load_id, "mc_number": self.mc, "agreed_rate": rate})


@pytest.fixture
def call(client: TestClient) -> Call:
    return Call(client)


def wrong_code(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"
