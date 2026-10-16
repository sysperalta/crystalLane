"""Public API documentation, served from the hand-written contract (openapi.yaml).

The contract has no secrets, so these routes need no API key:
    /docs          Swagger UI with "Try it out" against this deployment
    /redoc         reading view
    /openapi.yaml  the contract itself
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter(include_in_schema=False)

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "openapi.yaml"
_TITLE = "Carrier Sales Adapter API"
_SWAGGER_JS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"
_SWAGGER_CSS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css"
_REDOC_JS = "https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js"


@lru_cache(maxsize=1)
def contract_for_this_deployment() -> str:
    """The contract with this deployment listed first in `servers`, so Try it out calls it."""
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    return text.replace(
        "\nservers:\n",
        "\nservers:\n  - url: /v1\n    description: This deployment\n",
        1,
    )


@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@router.get("/openapi.yaml")
async def openapi_yaml() -> Response:
    return Response(contract_for_this_deployment(), media_type="application/yaml")


@router.get("/docs", response_class=HTMLResponse)
async def swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.yaml",
        title=_TITLE,
        swagger_js_url=_SWAGGER_JS,
        swagger_css_url=_SWAGGER_CSS,
        swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "list", "defaultModelsExpandDepth": 0},
    )


@router.get("/redoc", response_class=HTMLResponse)
async def redoc() -> HTMLResponse:
    page = get_redoc_html(openapi_url="/openapi.yaml", title=_TITLE, redoc_js_url=_REDOC_JS)
    # ReDoc has no dark theme and leaves the page transparent, so a dark browser shows dark-on-dark.
    html = page.body.decode().replace("<style>", "<style>\n      html, body { background: #ffffff; color-scheme: light; }", 1)
    return HTMLResponse(html)
