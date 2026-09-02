"""Off-box backup storage, over the S3 API.

The emailed snapshot is the database only. The lesson and ICT Fair PDFs live on
the Railway volume, and nothing copied them anywhere until a super-admin
remembered to click "Download files archive". Losing that volume would leave a
restored database full of rows pointing at files that no longer exist — every
lesson intact as a record, opening to nothing.

Written against the S3 API rather than any one vendor, so the same settings
drive Cloudflare R2, Backblaze B2, AWS S3 or MinIO. Only the endpoint changes.

Everything here fails soft and loud: a backup that cannot be uploaded is logged
as an error and the scheduler carries on, because a storage outage must not take
the API down with it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger("app.object_storage")


class ObjectStorageNotConfigured(RuntimeError):
    """Raised only on an explicit request. The scheduler checks `enabled()`."""


class ObjectStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    modified: datetime


def enabled() -> bool:
    """Whether uploads are configured. A bucket with no credentials is not."""
    return bool(
        settings.backup_storage_enabled
        and settings.backup_storage_bucket
        and settings.backup_storage_access_key_id
        and settings.backup_storage_secret_access_key
    )


def _client():
    """An S3 client for the configured endpoint.

    boto3 is imported here rather than at module scope so the app still starts
    when the dependency is absent — object storage is optional, and a missing
    import should disable a feature rather than refuse to boot.
    """
    if not enabled():
        raise ObjectStorageNotConfigured(
            "Backup storage needs BACKUP_STORAGE_ENABLED, _BUCKET, "
            "_ACCESS_KEY_ID and _SECRET_ACCESS_KEY."
        )
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise ObjectStorageNotConfigured(
            "boto3 is not installed; backup storage is unavailable."
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=settings.backup_storage_endpoint_url or None,
        aws_access_key_id=settings.backup_storage_access_key_id,
        aws_secret_access_key=settings.backup_storage_secret_access_key,
        region_name=settings.backup_storage_region or "auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=15,
            read_timeout=120,
        ),
    )


def _key(kind: str, filename: str) -> str:
    prefix = settings.backup_storage_prefix.strip("/")
    parts = [p for p in (prefix, kind, filename) if p]
    return "/".join(parts)


def upload_bytes(kind: str, filename: str, data: bytes, content_type: str) -> str:
    """Store a backup already held in memory. Returns the key it was written to."""
    key = _key(kind, filename)
    try:
        _client().put_object(
            Bucket=settings.backup_storage_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
    except ObjectStorageNotConfigured:
        raise
    except Exception as exc:
        raise ObjectStorageError(f"upload of {key} failed: {exc}") from exc
    logger.info("Backup uploaded: %s (%s bytes)", key, len(data))
    return key


def upload_file(kind: str, filename: str, path: str, content_type: str) -> str:
    """Store a backup that is already on disk.

    Streamed rather than read into memory: the PDF archive is the whole content
    library zipped, and holding that in a web process is how a backup takes the
    API down with it.
    """
    key = _key(kind, filename)
    try:
        with open(path, "rb") as handle:
            _client().put_object(
                Bucket=settings.backup_storage_bucket,
                Key=key,
                Body=handle,
                ContentType=content_type,
            )
    except ObjectStorageNotConfigured:
        raise
    except Exception as exc:
        raise ObjectStorageError(f"upload of {key} failed: {exc}") from exc
    logger.info("Backup uploaded: %s (%s bytes)", key, os.path.getsize(path))
    return key


def list_backups(kind: str) -> list[StoredObject]:
    """Everything stored under one kind, newest first."""
    prefix = _key(kind, "")
    try:
        client = _client()
        found: list[StoredObject] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": settings.backup_storage_bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                found.append(
                    StoredObject(
                        key=item["Key"],
                        size=int(item.get("Size", 0)),
                        modified=item.get("LastModified") or datetime.now(timezone.utc),
                    )
                )
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except ObjectStorageNotConfigured:
        raise
    except Exception as exc:
        raise ObjectStorageError(f"listing {prefix} failed: {exc}") from exc

    return sorted(found, key=lambda o: o.modified, reverse=True)


def prune(kind: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` backups of one kind.

    Without this a nightly upload grows without limit, and the bill is the first
    thing that notices. `keep <= 0` disables pruning, so a misconfigured zero
    keeps everything rather than deleting everything — the safe direction to
    fail in when the operation is irreversible.
    """
    if keep <= 0:
        return []

    stored = list_backups(kind)
    stale = stored[keep:]
    if not stale:
        return []

    try:
        _client().delete_objects(
            Bucket=settings.backup_storage_bucket,
            Delete={"Objects": [{"Key": o.key} for o in stale]},
        )
    except Exception as exc:
        raise ObjectStorageError(f"pruning {kind} failed: {exc}") from exc

    keys = [o.key for o in stale]
    logger.info("Pruned %s old %s backup(s): %s", len(keys), kind, ", ".join(keys))
    return keys
