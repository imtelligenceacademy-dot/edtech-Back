from __future__ import annotations

from pathlib import Path

from app.config import settings


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
