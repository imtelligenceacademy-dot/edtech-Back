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
from app.database import Base, SessionLocal, engine, ensure_added_columns
from app.services.backup import email_backup_now
from app.services.bootstrap import ensure_bootstrap_admin
from app.routers import (
    access_requests,
    ai,
    auth,
    backup,
    dashboard,
    files,
    lessons,
    progress,
    reports,
    schools,
    security,
    users,
)

# Import models so they register on Base.metadata before create_all.
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    # Dev convenience: ensure tables exist. Production should use Alembic migrations.
    Base.metadata.create_all(bind=engine)
    ensure_added_columns()

    # Seed the first super-admin on a fresh DB (no-op once one exists).
    with SessionLocal() as db:
        ensure_bootstrap_admin(db)

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
        if backup_task is not None:
            backup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await backup_task


app = FastAPI(
    title="IM-Telligence API",
    version="0.1.0",
    description="Backend for the IM-Telligence teacher platform.",
    lifespan=lifespan,
    # Hide schema docs in production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
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
)

for r in (auth, users, schools, lessons, progress, reports, security, files, dashboard, ai, backup, access_requests):
    app.include_router(r.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}
