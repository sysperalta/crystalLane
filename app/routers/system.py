from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.config import VERSION
from app.deps import Services, get_services, require_api_key
from app.errors import error_responses
from app.models import HealthResponse

router = APIRouter(tags=["System"])


@router.get(
    "/health",
    operation_id="getHealth",
    summary="Service health check",
    response_model=HealthResponse,
    responses=error_responses(HealthResponse),
    dependencies=[Depends(require_api_key)],
)
async def get_health(svc: Services = Depends(get_services)) -> HealthResponse:
    tms_ok, fmcsa_ok = await asyncio.gather(svc.tms.ping(), svc.fmcsa.ping())
    return HealthResponse(
        status="ok" if tms_ok and fmcsa_ok else "degraded",
        tms="reachable" if tms_ok else "unreachable",
        fmcsa="reachable" if fmcsa_ok else "unreachable",
        version=VERSION,
    )
