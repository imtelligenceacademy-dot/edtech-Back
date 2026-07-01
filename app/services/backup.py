"""Database backup: produce a consistent SQLite snapshot and (optionally) email it."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import smtplib
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx

from app.config import settings
from app.database import Base, engine
from app.models import User
from app.models.base import utcnow

logger = logging.getLogger("app.backup")


class EmailNotConfigured(RuntimeError):
    pass


class InvalidBackup(ValueError):
    pass


def _db_path() -> str:
    url = settings.database_url
    if not url.startswith("sqlite"):
        raise RuntimeError("DB backup only supports SQLite")
    # sqlite:///./im_telligence.db  ->  ./im_telligence.db
    return url.split("///", 1)[1]


def snapshot_bytes() -> bytes:
    """Return a consistent single-file copy of the DB (WAL changes included).

    Uses `VACUUM INTO`, which writes a clean, defragmented copy — safer than
    streaming the live file while the server is running.
    """
    target = os.path.join(tempfile.gettempdir(), f"imt_backup_{uuid.uuid4().hex}.db")
    src = sqlite3.connect(_db_path())
    try:
        src.execute("VACUUM INTO ?", (target,))
    finally:
        src.close()
    try:
        with open(target, "rb") as f:
            return f.read()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(target)


def backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    return f"im-telligence-backup-{stamp}.db"


def wipe_database(keep: dict) -> None:
    """Delete every row from every table, then re-insert the acting super-admin
    so they are never locked out."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(
            User.__table__.insert().values(
                id=keep["id"],
                name=keep["name"],
                email=keep["email"],
                password_hash=keep["password_hash"],
                role=keep["role"],
                status=keep["status"],
                grades=[],
                failed_login_count=0,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )


def restore_database(content: bytes) -> list[str]:
    """Replace all data with the contents of an uploaded .db backup (same schema).
    Returns the list of tables restored."""
    if content[:16] != b"SQLite format 3\x00":
        raise InvalidBackup("That file is not a valid SQLite database (.db).")

    tmp = os.path.join(tempfile.gettempdir(), f"imt_restore_{uuid.uuid4().hex}.db")
    with open(tmp, "wb") as f:
        f.write(content)

    order = [t.name for t in Base.metadata.sorted_tables]
    con = sqlite3.connect(_db_path())
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("ATTACH DATABASE ? AS src", (tmp,))
        src_tables = {
            r[0] for r in con.execute("SELECT name FROM src.sqlite_master WHERE type='table'")
        }
        if "users" not in src_tables:
            raise InvalidBackup("This .db doesn't look like an IM-Telligence backup.")

        con.execute("BEGIN")
        for t in reversed(order):
            con.execute(f'DELETE FROM main."{t}"')
        restored: list[str] = []
        for t in order:
            if t in src_tables:
                con.execute(f'INSERT INTO main."{t}" SELECT * FROM src."{t}"')
                restored.append(t)
        con.execute("COMMIT")
        con.execute("DETACH DATABASE src")
        return restored
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _email_body(note: str | None) -> str:
    return (
        (note.strip() + "\n\n" if note else "")
        + "Attached is a full IM-Telligence database backup (.db).\n"
        + "This file contains all platform data — store it securely."
    )


# An optional email attachment as (filename, raw bytes).
Attachment = tuple[str, bytes]


def _send_via_resend(
    recipients: list[str], subject: str, text: str, attachment: Attachment | None
) -> None:
    payload: dict = {
        "from": settings.resend_from,
        "to": recipients,
        "subject": subject,
        "text": text,
    }
    if attachment is not None:
        filename, data = attachment
        payload["attachments"] = [
            {"filename": filename, "content": base64.b64encode(data).decode()}
        ]
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()


def _send_via_smtp(
    recipients: list[str], subject: str, text: str, attachment: Attachment | None
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    if attachment is not None:
        filename, data = attachment
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=filename
        )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def send_email(
    recipients: list[str],
    subject: str,
    text: str,
    attachment: Attachment | None = None,
) -> None:
    """Send an email. Prefers Resend (production); if Resend is unconfigured or
    its send fails, falls back to SMTP (e.g. Gmail) when that is configured."""
    if settings.resend_api_key:
        try:
            _send_via_resend(recipients, subject, text, attachment)
            return
        except Exception:
            if not settings.smtp_host:
                raise
            logger.warning("Resend send failed; falling back to SMTP.", exc_info=True)

    if not settings.smtp_host:
        raise EmailNotConfigured(
            "Email is not configured. Set RESEND_API_KEY (recommended) or "
            "SMTP_HOST / SMTP_USER / SMTP_PASSWORD in backend/.env."
        )
    _send_via_smtp(recipients, subject, text, attachment)


def send_backup_email(recipients: list[str], data: bytes, filename: str, note: str | None) -> None:
    """Email a full DB backup as a .db attachment (Resend → SMTP fallback)."""
    send_email(
        recipients,
        f"IM-Telligence database backup — {filename}",
        _email_body(note),
        attachment=(filename, data),
    )


def email_backup_now(recipients: list[str], note: str | None = None) -> str:
    """Snapshot the DB and email it in one step. Returns the backup filename.
    Used by both the manual endpoint and the scheduled daily job."""
    data = snapshot_bytes()
    filename = backup_filename()
    send_backup_email(recipients, data, filename, note)
    return filename
