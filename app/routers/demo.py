"""Demo console (only when ENV != production and DEMO_CONSOLE_KEY is set).

Shows the simulated carrier handset (where the mocked SMS provider "delivers" OTP codes)
and a live timeline per call. Protected by its own key, so the agent's API key can never
read an OTP. Not part of the HappyRobot contract.
"""

from __future__ import annotations

import hmac
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import HTMLResponse

from app.deps import Services, get_services
from app.errors import ApiError
from app.logging_setup import log_event
from app.money import to_dollars
from app.state import CallState

router = APIRouter(prefix="/demo", include_in_schema=False)
log = logging.getLogger("clf.demo")

_CONSOLE_HTML = Path(__file__).resolve().parent.parent / "static" / "console.html"


async def require_demo_key(
    svc: Services = Depends(get_services),
    x_demo_key: str | None = Header(default=None, alias="X-Demo-Key"),
) -> None:
    expected = (svc.settings.demo_console_key or "").encode()
    if not expected or not hmac.compare_digest((x_demo_key or "").encode(), expected):
        raise ApiError(401, "unauthorized", "Invalid demo console key")


@router.get("", response_class=HTMLResponse)
async def console_page() -> HTMLResponse:
    return HTMLResponse(_CONSOLE_HTML.read_text(encoding="utf-8"))


_CODE_RE = re.compile(r"\b(\d{6})\b")


@router.get("/api/phone", dependencies=[Depends(require_demo_key)])
async def phone_inbox(
    mc: str | None = Query(default=None, pattern=r"^[0-9]{1,8}$"),
    call_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$"),
    svc: Services = Depends(get_services),
) -> dict[str, Any]:
    """The simulated handset. QA automation: GET /demo/api/phone?call_id=<X-Call-Id> -> messages[0].code."""
    messages = []
    for m in svc.sms.inbox(mc, call_id=call_id):
        match = _CODE_RE.search(m.body)
        messages.append({"sent_at": m.sent_at, "mc_number": m.mc_number, "to": m.to_masked,
                         "call_id": m.call_id, "code": match.group(1) if match else None, "body": m.body})
    return {"messages": messages}


def _call_view(state: CallState) -> dict[str, Any]:
    return {
        "call_id": state.call_id,
        "started_at": state.started_at,
        "mc_number": state.mc_number,
        "carrier_name": state.carrier_name,
        "authorized": state.authorized,
        "otp_verified": state.otp.verified,
        "bookings": [
            {"load_id": load_id, "reference": b.reference, "rate": to_dollars(b.rate_cents)}
            for load_id, b in state.bookings.items()
        ],
        "events": list(state.events),
    }


@router.get("/api/calls", dependencies=[Depends(require_demo_key)])
async def recent_calls(svc: Services = Depends(get_services)) -> dict[str, Any]:
    return {
        "calls": [_call_view(s) for s in svc.calls.recent(15)],
        "handoffs": list(svc.handoffs.history)[:20],
    }


@router.post("/api/reset", dependencies=[Depends(require_demo_key)])
async def reset_demo(svc: Services = Depends(get_services)) -> dict[str, Any]:
    svc.calls.clear()
    svc.sms.clear()
    svc.handoffs.clear()
    tms_reset = await svc.tms.reset()
    log_event(log, "demo_reset", tms_reset=tms_reset)
    return {"status": "reset", "tms_reset": tms_reset}
