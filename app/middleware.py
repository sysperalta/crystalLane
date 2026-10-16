"""One log line per request plus defensive response headers.

Only method, path, status, latency and X-Call-Id are logged: never bodies, never other headers.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.deps import CALL_ID_RE
from app.logging_setup import log_event

log = logging.getLogger("clf.http")

_QUIET_PATHS = {"/healthz"}
_EXTRA_HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]


def _call_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"x-call-id":
            text = value.decode("latin-1")
            return text if CALL_ID_RE.match(text) else "invalid"
    return None


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []), *_EXTRA_HEADERS]}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in _QUIET_PATHS:
                # Set by the error handlers: error code + message, which never include submitted values.
                rejection = scope.get("state", {}).get("rejection") or {}
                log_event(
                    log,
                    "request",
                    call_id=_call_id(scope),
                    method=scope.get("method"),
                    path=path,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    **rejection,
                )
