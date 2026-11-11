"""FastAPI application entry point: `uvicorn app.main:app`."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import reports, research, stream
from .config import ROOT_DIR, configure_logging, get_settings
from .database import init_db
from .services.research_service import ResearchService

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    app.state.service = ResearchService(settings)
    app.state.service.mark_interrupted()
    missing = settings.missing_keys(settings.search_provider)
    if missing:
        log.warning("Missing API keys: %s - research cannot start until they are set in .env", ", ".join(missing))
    yield


app = FastAPI(title="ResearchFlow AI", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Full detail goes to the log; the client only learns that something failed.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(research.router)
app.include_router(reports.router)
app.include_router(stream.router)

# Serve the built frontend (single-container deployment). In development Vite serves it instead.
_static = Path(os.environ.get("STATIC_DIR", ROOT_DIR / "frontend" / "dist"))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="frontend")
