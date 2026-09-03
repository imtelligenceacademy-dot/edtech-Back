"""The two functions the scheduler and the admin button actually call.

These exist because the object-storage tests did not cover them, and the gap
shipped: `build_files_archive` returns the missing files as a count, the caller
treated it as a list, and `len()` on an int failed in production on the first
real run. Both functions are now driven end to end, so a signature that moves
underneath them fails here instead.
"""

from __future__ import annotations

import os

import pytest

from app.config import settings
from app.services import backup, object_storage


@pytest.fixture()
def captured(monkeypatch):
    """Records uploads instead of performing them."""
    calls: dict = {"uploads": [], "pruned": [], "removed_temp": None}

    def fake_upload_bytes(kind, filename, data, content_type):
        calls["uploads"].append((kind, filename, len(data), content_type))
        return f"prefix/{kind}/{filename}"

    def fake_upload_file(kind, filename, path, content_type):
        # Assert the temp file still exists at upload time — deleting it in the
        # wrong order would upload nothing and report success.
        assert os.path.exists(path), "the archive must still be on disk when uploaded"
        calls["uploads"].append((kind, filename, os.path.getsize(path), content_type))
        calls["removed_temp"] = path
        return f"prefix/{kind}/{filename}"

    monkeypatch.setattr(object_storage, "upload_bytes", fake_upload_bytes)
    monkeypatch.setattr(object_storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(object_storage, "prune", lambda kind, keep: calls["pruned"].append(kind))
    monkeypatch.setattr(settings, "backup_storage_keep", 3)
    return calls


def test_the_database_snapshot_uploads_and_prunes(captured, db):
    key = backup.backup_database_to_storage()

    assert key.startswith("prefix/database/")
    kind, filename, size, _ = captured["uploads"][0]
    assert kind == "database" and size > 0
    assert captured["pruned"] == ["database"], "old snapshots are pruned after a new one"


def test_the_file_archive_uploads_and_reports_counts_as_numbers(captured, db):
    """The bug this file exists for: the caller must not call len() on a count."""
    key, included, missing = backup.backup_files_to_storage()

    assert key.startswith("prefix/files/")
    assert isinstance(included, int)
    assert isinstance(missing, int)
    assert captured["pruned"] == ["files"]


def test_the_temp_archive_is_deleted_after_upload(captured, db):
    """A failed or finished backup must not leave the zip behind; filling the
    disk with backups is how one problem becomes two."""
    backup.backup_files_to_storage()

    leftover = captured["removed_temp"]
    assert leftover is not None
    assert not os.path.exists(leftover), "the temp archive should be cleaned up"


def test_the_temp_archive_is_deleted_even_when_the_upload_fails(monkeypatch, db):
    seen: dict = {}

    def exploding_upload(kind, filename, path, content_type):
        seen["path"] = path
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(object_storage, "upload_file", exploding_upload)
    monkeypatch.setattr(object_storage, "prune", lambda kind, keep: None)

    with pytest.raises(RuntimeError):
        backup.backup_files_to_storage()

    assert not os.path.exists(seen["path"]), "a failed upload must still clean up"
