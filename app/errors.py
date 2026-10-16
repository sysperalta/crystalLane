"""Failures are answered inside the called endpoint's own envelope, with ok=false.

HappyRobot turns any non-2xx reply into one opaque text blob, so on real endpoints every
call-time failure (bad input, call order, rate limit, upstream outage) is HTTP 200 with
`ok: false`, `error_code`, `error_message` and `retryable`. Only a wrong API key keeps 401: it
is a configuration problem, not something the agent can handle mid-call. Internally ApiError
still carries the HTTP-equivalent status, which is logged as `error_status`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import log_event

log = logging.getLogger("clf.errors")


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        *,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.retryable = retryable
        self.headers = headers


def invalid_request(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


def step_out_of_order(message: str) -> ApiError:
    return ApiError(409, "step_out_of_order", message)


def _body(error: str, message: str, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "error_code": error, "error_message": message, "retryable": retryable}


def _reject(
    request: Request, status_code: int, error: str, message: str, retryable: bool,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    # The request log line (middleware) picks this up, so every rejection is logged with its reason.
    # Messages never contain submitted values, so this is safe to log.
    request.state.rejection = {"error_code": error, "error_message": message, "error_status": status_code}
    # Answer in the called endpoint's own envelope (same keys as its success response, business
    # fields null), as HTTP 200 unless the API key is wrong. Unknown paths keep their real status
    # and the bare {ok, error_code, error_message, retryable}.
    envelope = getattr(request.app.state, "envelopes", {}).get(request.scope.get("endpoint"))
    if envelope is None:
        return JSONResponse(status_code=status_code, content=_body(error, message, retryable), headers=headers)
    content = envelope(error_code=error, error_message=message, retryable=retryable).model_dump(mode="json")
    return JSONResponse(status_code=401 if status_code == 401 else 200, content=content, headers=headers)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _reject(request, exc.status_code, exc.error, exc.message, exc.retryable, exc.headers)


# Field-specific hints read better for a voice agent than raw pydantic messages.
_FIELD_HINTS = {
    "mc_number": "mc_number must contain digits only (1-8 digits)",
    "code": "code must be exactly 6 digits",
    "equipment_type": "equipment_type must be one of: dry_van, reefer, flatbed",
    "pickup_date": "pickup_date must be a date in YYYY-MM-DD format",
    "load_id": "load_id must look like LD-10482",
}
_LOCATIONS = {"body", "path", "query", "header", "cookie"}


def _describe(err: dict[str, Any]) -> str:
    """Human message for one validation error. Never includes the rejected input value."""
    kind = err.get("type", "")
    loc = [str(part) for part in err.get("loc", ()) if part not in _LOCATIONS]
    if kind == "json_invalid":
        return "Request body must be valid JSON"
    if not loc:
        if kind == "missing":
            return "Request body is required"
        return "Request body must be a JSON object"
    field = ".".join(loc)
    if kind == "extra_forbidden":
        return f"{field} is not an accepted field"
    value = err.get("input")
    if kind == "missing" or value is None or (isinstance(value, str) and not value.strip()):  # absent, null, blank
        return f"{field} is required"
    if loc[-1] in _FIELD_HINTS:
        return _FIELD_HINTS[loc[-1]]
    return f"{field}: {err.get('msg', 'is invalid')}"


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    messages = list(dict.fromkeys(_describe(e) for e in exc.errors()))
    return _reject(request, 400, "invalid_request", "; ".join(messages[:3]), False)


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    messages = {404: "Not found", 405: "Method not allowed"}
    message = messages.get(exc.status_code) or str(exc.detail)
    return _reject(request, exc.status_code, "invalid_request", message, False, getattr(exc, "headers", None))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event(log, "unhandled_error", logging.ERROR, path=request.url.path, error_type=type(exc).__name__)
    return _reject(request, 500, "internal_error", "Unexpected error", True)


def error_responses(envelope: type) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entry for the one non-200 reply an endpoint gives: 401, in its own envelope."""
    return {401: {"model": envelope, "description": "Missing or wrong API key (the only non-200 reply)"}}
