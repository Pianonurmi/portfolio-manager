"""FastAPI application factory."""

import importlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.modules import MODULES
from app.platform import api as platform_api
from app.platform import (
    auth,
    connections,
    system_jobs,  # noqa: F401  registers platform jobs
)

logging.basicConfig(
    level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}'
)


def load_modules() -> list[object]:
    routers = []
    for name in MODULES:
        for part in ("jobs", "explain"):
            try:
                importlib.import_module(f"app.modules.{name}.{part}")
            except ModuleNotFoundError as exc:
                if exc.name != f"app.modules.{name}.{part}":
                    raise
        api = importlib.import_module(f"app.modules.{name}.api")
        routers.append(api.router)
        routers.extend(getattr(api, "extra_routers", []))
    return routers


def create_app() -> FastAPI:
    app = FastAPI(title="Portfolio Manager", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.include_router(platform_api.health)
    app.include_router(auth.router)
    app.include_router(platform_api.router)
    app.include_router(connections.router)
    for r in load_modules():
        app.include_router(r)  # type: ignore[arg-type]

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app


app = create_app()
