"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.clock import Clock
from app.config import VERSION, Settings, load_settings
from app.deps import Services
from app.errors import ApiError, api_error_handler, http_error_handler, unhandled_error_handler, validation_error_handler
from app.logging_setup import configure_logging, log_event
from app.middleware import RequestLogMiddleware
from app.routers import carriers, demo, docs, handoff, loads, offers, otp, system
from app.services.fmcsa import SimulatedFmcsaClient
from app.services.handoff import HandoffDesk
from app.services.otp import OtpService
from app.services.sms import SimulatedSmsProvider
from app.state import CallStore
from app.tms.client import TmsClient
from tms_sim.server import TmsSimulator

log = logging.getLogger("clf.app")

DESCRIPTION = """
Interface between the HappyRobot voice agent and Crystal Lane Freight's systems
(legacy TMS over TCP, FMCSA, SMS provider). **All three are simulated in this POC.**

The service enforces every business and compliance rule; the AI prompt is only a second
layer of defence.

**Enforced call order (per X-Call-Id):** verify carrier -> send OTP -> verify OTP ->
search loads -> evaluate offers -> book -> handoff

**Never returned or logged:** max_rate, OTP codes, full carrier phone numbers, API keys,
TMS credentials.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = (settings or load_settings()).validate()
    configure_logging()
    clock = Clock()
    services = Services(
        settings=settings,
        clock=clock,
        calls=CallStore(clock),
        otp=OtpService(),
        sms=SimulatedSmsProvider(),
        fmcsa=SimulatedFmcsaClient(settings.demo_carriers.keys(), qa_mcs=settings.qa_test_carriers.keys(),
                                   latency=settings.fmcsa_latency),
        phones_on_file=_phones_on_file(settings),
        handoffs=HandoffDesk(clock),
    )
    console_enabled = settings.demo_mode and settings.demo_console_key is not None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        server = None
        host, port = settings.tms_host, settings.tms_port
        if settings.tms_embedded_sim:
            services.tms_sim = TmsSimulator(settings.tms_token, fault_rate=settings.tms_fault_rate)
            server = await services.tms_sim.start("127.0.0.1", settings.tms_port)
            host, port = "127.0.0.1", server.sockets[0].getsockname()[1]
        services.tms = TmsClient(host, port, settings.tms_token)
        sweeper = asyncio.create_task(_sweep_expired_calls(services.calls))
        log_event(log, "service_started", version=VERSION, env=settings.env, tms=f"{host}:{port}",
                  tms_embedded_sim=settings.tms_embedded_sim, tms_fault_rate=settings.tms_fault_rate,
                  demo_console=console_enabled, demo_carriers=sorted(settings.demo_carriers),
                  qa_test_carriers=sorted(settings.qa_test_carriers))  # MC numbers only, never the codes
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server.wait_closed(), 3)

    app = FastAPI(
        title="Crystal Lane Freight - Carrier Sales Adapter API",
        version=VERSION,
        description=DESCRIPTION,
        docs_url=None,  # /docs and /redoc render the hand-written contract (app/routers/docs.py)
        redoc_url=None,
        openapi_url="/openapi.json",  # generated from the code; handy to diff against the contract
        lifespan=lifespan,
    )
    app.state.services = services

    app.include_router(docs.router)
    for module in (system, carriers, otp, loads, offers, handoff):
        app.include_router(module.router, prefix="/v1")
    if console_enabled:
        app.include_router(demo.router)

    @app.get("/healthz", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        """Unauthenticated liveness probe for Docker / Azure. Reveals nothing."""
        return {"status": "ok"}

    # Endpoint -> its response envelope, so error handlers answer in the same shape as success.
    app.state.envelopes = {
        route.endpoint: route.response_model
        for route in app.routes
        if isinstance(route, APIRoute)
        and isinstance(route.response_model, type)
        and issubclass(route.response_model, BaseModel)
    }

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(RequestLogMiddleware)
    return app


def _phones_on_file(settings: Settings) -> dict[str, str]:
    """DEMO_CARRIERS phones, plus a fictitious 555 number for each QA test carrier without one."""
    phones = dict(settings.demo_carriers)
    for mc in settings.qa_test_carriers:
        phones.setdefault(mc, f"+1555{mc[-7:].zfill(7)}")
    return phones


async def _sweep_expired_calls(calls: CallStore, every_seconds: float = 60) -> None:
    while True:
        await asyncio.sleep(every_seconds)
        purged = calls.purge_expired()
        if purged:
            log_event(log, "calls_expired", count=purged)
