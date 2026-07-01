"""Database backup, restore, wipe, and optional email delivery.

SQLite development uses single-file .db snapshots. Postgres production uses a
portable JSON export of all SQLAlchemy-managed application tables.
"""

from __future__ import annotations

import base64
import contextlib
import enum
import json
import logging
import os
import smtplib
import sqlite3
import tempfile
import uuid
from datetime import date, datetime, time, timezone
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy import Date, DateTime, Enum as SAEnum, Time, select

from app.config import settings
from app.database import Base, IS_SQLITE, engine
from app.models import User
from app.models.base import utcnow

logger = logging.getLogger("app.backup")

BACKUP_FORMAT = "im-telligence-backup"
BACKUP_VERSION = 1


class EmailNotConfigured(RuntimeError):
    pass


class EmailDeliveryFailed(RuntimeError):
    pass


class InvalidBackup(ValueError):
    pass


def _db_path() -> str:
    url = settings.database_url
    if not url.startswith("sqlite"):
        raise RuntimeError("DB backup only supports SQLite file snapshots")
    return url.split("///", 1)[1]


def _sqlite_snapshot_bytes() -> bytes:
    """Return a consistent single-file SQLite copy, including WAL changes."""
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


def _serialize_value(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    return value


def _deserialize_value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and "__bytes_b64__" in value:
        return base64.b64decode(value["__bytes_b64__"])
    if isinstance(column.type, SAEnum) and column.type.enum_class is not None:
        return column.type.enum_class(value)
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    if isinstance(column.type, Time):
        return time.fromisoformat(value)
    return value


def _json_snapshot_bytes() -> bytes:
    payload: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": "sqlalchemy-json",
        "tables": {},
    }
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            stmt = select(table)
            order_by = list(table.primary_key.columns)
            if order_by:
                stmt = stmt.order_by(*order_by)

            rows = []
            for row in conn.execute(stmt):
                mapping = row._mapping
                rows.append(
                    {
                        column.name: _serialize_value(mapping[column.name])
                        for column in table.columns
                    }
                )
            payload["tables"][table.name] = rows

    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def snapshot_bytes() -> bytes:
    if IS_SQLITE:
        return _sqlite_snapshot_bytes()
    return _json_snapshot_bytes()


def backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    ext = "db" if IS_SQLITE else "json"
    return f"im-telligence-backup-{stamp}.{ext}"


def backup_upload_hint() -> str:
    return ".db" if IS_SQLITE else ".json"


def wipe_database(keep: dict) -> None:
    """Delete every row from every table, then re-insert the acting super-admin."""
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


def _restore_sqlite_database(content: bytes) -> list[str]:
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
            raise InvalidBackup("This .db does not look like an IM-Telligence backup.")

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


def _restore_json_database(content: bytes) -> list[str]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise InvalidBackup("That file is not a valid IM-Telligence JSON backup.") from exc

    if payload.get("format") != BACKUP_FORMAT:
        raise InvalidBackup("That file is not an IM-Telligence backup.")
    if payload.get("version") != BACKUP_VERSION:
        raise InvalidBackup("That backup version is not supported by this server.")

    tables = payload.get("tables")
    if not isinstance(tables, dict) or "users" not in tables:
        raise InvalidBackup("That backup is missing required IM-Telligence tables.")

    restored: list[str] = []
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

        for table in Base.metadata.sorted_tables:
            raw_rows = tables.get(table.name)
            if raw_rows is None:
                continue
            if not isinstance(raw_rows, list):
                raise InvalidBackup(f"Table {table.name} is not valid in this backup.")

            columns_by_name = {column.name: column for column in table.columns}
            rows = []
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    raise InvalidBackup(f"Table {table.name} contains an invalid row.")
                rows.append(
                    {
                        name: _deserialize_value(column, raw_row[name])
                        for name, column in columns_by_name.items()
                        if name in raw_row
                    }
                )
            if rows:
                conn.execute(table.insert(), rows)
            restored.append(table.name)
    return restored


def restore_database(content: bytes) -> list[str]:
    if IS_SQLITE:
        return _restore_sqlite_database(content)
    return _restore_json_database(content)


def _email_body(note: str | None) -> str:
    return (
        (note.strip() + "\n\n" if note else "")
        + "Attached is a full IM-Telligence database backup.\n"
        + "This file contains all platform data. Store it securely."
    )


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
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = resp.text[:500] if resp.text else str(exc)
        raise EmailDeliveryFailed(f"Resend rejected the email: {detail}") from exc


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

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_tls:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except Exception as exc:
        raise EmailDeliveryFailed(f"SMTP failed: {exc}") from exc


def send_email(
    recipients: list[str],
    subject: str,
    text: str,
    attachment: Attachment | None = None,
) -> None:
    """Send an email using Resend when configured, otherwise SMTP."""
    if settings.resend_api_key:
        try:
            _send_via_resend(recipients, subject, text, attachment)
            return
        except Exception as exc:
            raise EmailDeliveryFailed(f"Resend failed: {exc}") from exc

    if not settings.smtp_host:
        raise EmailNotConfigured(
            "Email is not configured. Set RESEND_API_KEY or "
            "SMTP_HOST / SMTP_USER / SMTP_PASSWORD."
        )
    _send_via_smtp(recipients, subject, text, attachment)


def send_backup_email(recipients: list[str], data: bytes, filename: str, note: str | None) -> None:
    send_email(
        recipients,
        f"IM-Telligence database backup - {filename}",
        _email_body(note),
        attachment=(filename, data),
    )


def email_backup_now(recipients: list[str], note: str | None = None) -> str:
    data = snapshot_bytes()
    filename = backup_filename()
    send_backup_email(recipients, data, filename, note)
    return filename
