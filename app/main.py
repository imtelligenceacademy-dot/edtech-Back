"""FastAPI application entrypoint.

Run (from the backend/ directory):
    uvicorn app.main:app --reload
Interactive docs: http://localhost:8000/docs
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import SessionLocal
from app.migrate import run_migrations
from app.services.backup import email_backup_now
from app.services.chat_history import purge_expired
from app.services.bootstrap import ensure_bootstrap_admin
from app.routers import (
    access_requests,
    ai,
    auth,
    backup,
    dashboard,
    fair,
    chat,
    files,
    lessons,
    progress,
    reports,
    schools,
    security,
    users,
)

# Import models so they register on Base.metadata (migrations autogenerate
# against it, and the ORM needs every mapper configured before first use).
import app.models  # noqa: F401

logger = logging.getLogger("app")


async def _daily_backup_loop() -> None:
    """Email a full DB backup to the configured recipient every N hours.
    The blocking snapshot + send runs in a worker thread so the event loop
    (i.e. the API) is never blocked."""
    interval = max(1, settings.backup_interval_hours) * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            filename = await asyncio.to_thread(
                email_backup_now, [settings.backup_email_to], "Automated daily backup."
            )
            logger.info("Daily backup emailed to %s (%s)", settings.backup_email_to, filename)
        except Exception:
            logger.exception("Daily backup email failed")


async def _chat_retention_loop() -> None:
    """Drop teacher conversations past the retention window, once a day.

    The delete runs in a worker thread for the same reason the backup does: it
    is a blocking DB call and the API should not wait on it.
    """
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            deleted = await asyncio.to_thread(_purge_chat_history)
            if deleted:
                logger.info(
                    "Chat retention: deleted %s messages older than %s days",
                    deleted,
                    settings.chat_retention_days,
                )
        except Exception:
            logger.exception("Chat retention purge failed")


def _purge_chat_history() -> int:
    with SessionLocal() as db:
        return purge_expired(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    # Alembic owns the schema. Handles a fresh database, one built by the old
    # create_all path, and the normal incremental case — see app/migrate.py.
    run_migrations()

    # Seed the first super-admin on a fresh DB (no-op once one exists).
    with SessionLocal() as db:
        ensure_bootstrap_admin(db)

    retention_task = asyncio.create_task(_chat_retention_loop())

    backup_task: asyncio.Task | None = None
    if settings.backup_email_enabled and settings.backup_email_to:
        backup_task = asyncio.create_task(_daily_backup_loop())
        logger.info(
            "Daily backup scheduler started: every %sh to %s",
            settings.backup_interval_hours,
            settings.backup_email_to,
        )

    try:
        yield
    finally:
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        if backup_task is not None:
            backup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await backup_task


def doc_urls(is_production: bool) -> dict[str, str | None]:
    """Where the interactive docs and the schema live, or nothing in production.

    All three have to be turned off together. Disabling `docs_url` alone hides
    the Swagger page while `openapi.json` keeps serving the schema behind it —
    every route, parameter and field, readable by anyone. That is what was
    happening: the docs looked hidden and the map was public.
    """
    if is_production:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": None, "openapi_url": "/openapi.json"}


app = FastAPI(
    title="IM-Telligence API",
    version="0.1.0",
    description="Backend for the IM-Telligence teacher platform.",
    lifespan=lifespan,
    **doc_urls(settings.is_production),
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )

        # Lesson PDFs are meant to be embedded in the frontend's lesson viewer,
        # so allow framing from the configured frontend origins for that one
        # endpoint. Everything else stays DENY (clickjacking protection).
        path = request.url.path
        is_pdf_download = path.startswith("/api/files/") and path.endswith("/download")
        if is_pdf_download:
            allowed = " ".join(["'self'", *settings.cors_origin_list])
            response.headers["Content-Security-Policy"] = f"frame-ancestors {allowed}"
        else:
            response.headers.setdefault("X-Frame-Options", "DENY")

        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,  # required for cookie-based auth
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Content-Disposition"],
)

for r in (auth, users, schools, lessons, progress, reports, security, files, dashboard, ai, backup, access_requests, fair, chat):
    app.include_router(r.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
