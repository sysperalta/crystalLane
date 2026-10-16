"""Request/response schemas mirroring openapi.yaml.

Every response goes through one of these models with extra="forbid", so a field that is
not declared here (max_rate above all) cannot reach HappyRobot even by accident.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, StringConstraints, model_validator

_EQUIPMENT_ALIASES = {
    "dry_van": "dry_van",
    "dryvan": "dry_van",
    "van": "dry_van",
    "reefer": "reefer",
    "refrigerated": "reefer",
    "flatbed": "flatbed",
    "flat_bed": "flatbed",
}


def _strip_separators(value: Any) -> Any:
    """Voice transcripts often produce '123 456' or '482-913'. Letters are still rejected."""
    if isinstance(value, str):
        return re.sub(r"[\s-]+", "", value)
    return value


def _normalize_equipment(value: Any) -> Any:
    if isinstance(value, str):
        key = re.sub(r"[\s-]+", "_", value.strip().lower())
        return _EQUIPMENT_ALIASES.get(key, key)
    return value


def _normalize_load_id(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


def _blank_to_none(value: Any) -> Any:
    """HappyRobot sends every field, so an empty optional field means "not given"."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _parse_money(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("$", "").replace(",", "").strip()
    return value


def _money_out(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 2)


McNumber = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{1,8}$"),
    BeforeValidator(_strip_separators),
    Field(description="MC number, digits only", examples=["123456"]),
]
OtpCode = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{6}$"),
    BeforeValidator(_strip_separators),
    Field(examples=["482913"]),
]
Money = Annotated[
    float,
    Field(ge=0, le=1_000_000, allow_inf_nan=False, description="Amount in US dollars, no symbols", examples=[2250]),
    BeforeValidator(_parse_money),
]
MoneyOut = Annotated[
    float,
    PlainSerializer(_money_out, return_type=int | float),  # 2250, not 2250.0: read aloud by the agent
    Field(description="Amount in US dollars", examples=[2250]),
]
EquipmentType = Annotated[Literal["dry_van", "reefer", "flatbed"], BeforeValidator(_normalize_equipment)]
LoadId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=20, pattern=r"^[A-Z0-9-]+$"),
    BeforeValidator(_normalize_load_id),
    Field(examples=["LD-10482"]),
]
Place = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- requests -----------------------------------------------------------------------


class McNumberRequest(_Request):
    mc_number: McNumber


class VerifyCarrierRequest(McNumberRequest):
    pass


class VerifyOtpRequest(_Request):
    mc_number: McNumber
    code: OtpCode


class SearchLoadsRequest(_Request):
    mc_number: McNumber
    origin: Place = Field(examples=["Joliet, IL"])
    destination: Annotated[Place | None, BeforeValidator(_blank_to_none)] = Field(
        default=None, examples=["Dallas, TX"], description='Optional. "" or null means no preference'
    )
    equipment_type: EquipmentType
    pickup_date: Annotated[date | None, BeforeValidator(_blank_to_none)] = Field(
        default=None, examples=["2026-10-08"], description='Optional YYYY-MM-DD. "" or null means any date'
    )


class EvaluateOfferRequest(_Request):
    load_id: LoadId
    mc_number: McNumber
    carrier_offer: Money
    round: int = Field(
        ge=1,
        description="Agent's view of the round. Informational only: the service counts rounds itself.",
    )


class BookLoadRequest(_Request):
    mc_number: McNumber
    agreed_rate: Money


class HandoffRequest(_Request):
    load_id: LoadId
    mc_number: McNumber
    agreed_rate: Money


# --- responses ----------------------------------------------------------------------

ErrorCode = Literal[
    "invalid_request",
    "unauthorized",
    "step_out_of_order",
    "rate_limited",
    "tms_unavailable",
    "tms_malformed_response",
    "fmcsa_unavailable",
    "sms_unavailable",
    "internal_error",
]


class ErrorResponse(_Response):
    """Only for paths that don't exist. Every real endpoint answers with its own envelope below."""

    ok: bool = False
    error_code: ErrorCode
    error_message: str
    retryable: bool


class _Envelope(_Response):
    """One shape per endpoint: success and failure carry exactly the same keys.

    HappyRobot's agent reads these responses with an LLM and learns each tool's shape, so the
    shape must never change. `ok` is true when the request was processed (a "carrier not
    authorized" answer is still ok). When ok is false, `error_code` and `error_message` say why
    and every business field is null (lists are empty). Field names avoid `error`, which
    HappyRobot uses itself for failed requests.
    """

    ok: bool = Field(default=True, description="true when the request was processed; false when error_code is set")
    error_code: ErrorCode | None = Field(default=None, description="null when ok, otherwise the error code")
    error_message: str | None = Field(default=None, description="null when ok, otherwise what to fix or say")
    retryable: bool = Field(default=False, description="true when retrying the same request may succeed")

    @model_validator(mode="after")
    def _ok_follows_error_code(self) -> _Envelope:
        self.ok = self.error_code is None
        return self


class HealthResponse(_Envelope):
    status: Literal["ok", "degraded"] | None = None
    tms: Literal["reachable", "unreachable"] | None = None
    fmcsa: Literal["reachable", "unreachable"] | None = None
    version: str | None = None


class VerifyCarrierResponse(_Envelope):
    authorized: bool | None = None
    carrier_name: str | None = None
    authority_status: Literal["active", "inactive", "revoked", "not_found"] | None = None
    reason: str | None = None


class SendOtpResponse(_Envelope):
    status: Literal["sent", "resend_limit_reached", "no_phone_on_file", "not_authorized"] | None = None
    masked_phone: str | None = None
    expires_in_seconds: int | None = None
    resends_remaining: int | None = Field(default=None, ge=0, le=1)


class VerifyOtpResponse(_Envelope):
    status: Literal["verified", "incorrect", "expired", "attempts_exhausted", "not_sent"] | None = None
    attempts_remaining: int | None = Field(default=None, ge=0, le=3)


class Load(_Response):
    """A load as shown to the carrier. max_rate is intentionally NOT part of this schema."""

    load_id: str
    origin: str
    destination: str
    pickup_datetime: str
    delivery_datetime: str
    equipment_type: Literal["dry_van", "reefer", "flatbed"]
    loadboard_rate: MoneyOut
    weight: int | None = None
    commodity_type: str | None = None
    num_of_pieces: int | None = None
    miles: int | None = None
    dimensions: str | None = None
    notes: str | None = None


class SearchLoadsResponse(_Envelope):
    status: Literal["found", "no_match"] | None = None
    loads: list[Load] = Field(default_factory=list, max_length=3)


class EvaluateOfferResponse(_Envelope):
    decision: Literal["accept", "counter", "final_decline"] | None = None
    counter_rate: MoneyOut | None = None
    rounds_remaining: int | None = Field(default=None, ge=0, le=2)


class BookLoadResponse(_Envelope):
    status: Literal["booked", "unavailable", "rate_not_approved"] | None = None
    booking_reference: str | None = None


class HandoffResponse(_Envelope):
    status: Literal["queued"] | None = None
    queue_position: int | None = None
    handoff_id: str | None = None
