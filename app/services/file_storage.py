from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from app.config import settings

# How much is pulled off the upload at a time while measuring it.
_READ_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    """An upload passed the size limit. Raised as soon as it does."""


def read_upload_capped(stream: BinaryIO, max_bytes: int) -> bytes:
    """The uploaded bytes, refusing anything over the limit before holding it.

    Read whole and measured afterwards, the refusal only arrived once the
    thing being refused was already in memory: a client posting two
    gigabytes got a 413 that cost this process two gigabytes to produce,
    which is a denial of service with a polite error code on the end of it.
    Starlette spools past a megabyte to a temporary file, so the body on
    disk was bounded and the read of it was not.

    Measured as it goes instead, so the limit costs a chunk rather than a
    file.
    """
    stream.seek(0)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def upload_root() -> Path:
    return Path(settings.upload_dir)


def resolve_stored_file(storage_path: str | None) -> Path | None:
    if not storage_path:
        return None

    stored_name = Path(storage_path).name
    roots = [
        upload_root(),
        Path("/data/files"),
        Path("./storage/files"),
    ]

    seen: set[Path] = set()
    for root in roots:
        candidate = root / stored_name
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate
    return None
