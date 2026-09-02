"""Off-box backup storage.

The pruning tests carry the weight. Everything else here fails by not making a
backup, which is recoverable by fixing it; pruning fails by deleting backups
that already exist, which is not. So the assertions are about what it refuses to
delete as much as what it removes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import object_storage
from app.services.object_storage import (
    ObjectStorageError,
    ObjectStorageNotConfigured,
    StoredObject,
    enabled,
    prune,
)


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(settings, "backup_storage_enabled", True)
    monkeypatch.setattr(settings, "backup_storage_bucket", "imt-backups")
    monkeypatch.setattr(settings, "backup_storage_access_key_id", "key")
    monkeypatch.setattr(settings, "backup_storage_secret_access_key", "secret")
    monkeypatch.setattr(settings, "backup_storage_prefix", "im-telligence")


class FakeS3:
    def __init__(self, keys: list[str] | None = None):
        self.put: list[dict] = []
        self.deleted: list[str] = []
        now = datetime.now(timezone.utc)
        # Newest first in the list we hand back, oldest last.
        self._objects = [
            {"Key": k, "Size": 100, "LastModified": now - timedelta(days=i)}
            for i, k in enumerate(keys or [])
        ]

    def put_object(self, **kw):
        self.put.append(kw)

    def list_objects_v2(self, **kw):
        prefix = kw.get("Prefix", "")
        return {
            "Contents": [o for o in self._objects if o["Key"].startswith(prefix)],
            "IsTruncated": False,
        }

    def delete_objects(self, **kw):
        self.deleted += [o["Key"] for o in kw["Delete"]["Objects"]]


# --- Configuration ---------------------------------------------------------- #


def test_a_bucket_without_credentials_is_not_enabled(monkeypatch):
    monkeypatch.setattr(settings, "backup_storage_enabled", True)
    monkeypatch.setattr(settings, "backup_storage_bucket", "imt-backups")
    monkeypatch.setattr(settings, "backup_storage_access_key_id", "")
    monkeypatch.setattr(settings, "backup_storage_secret_access_key", "")

    assert enabled() is False, "half-configured storage must not look ready"


def test_uploading_while_unconfigured_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "backup_storage_enabled", False)

    with pytest.raises(ObjectStorageNotConfigured) as exc:
        object_storage.upload_bytes("database", "x.json", b"{}", "application/json")
    assert "BACKUP_STORAGE_ENABLED" in str(exc.value)


# --- Keys and uploads ------------------------------------------------------- #


def test_backups_are_filed_under_prefix_and_kind(configured, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(object_storage, "_client", lambda: fake)

    key = object_storage.upload_bytes(
        "database", "snap-2026-09-02.json", b"{}", "application/json"
    )

    assert key == "im-telligence/database/snap-2026-09-02.json"
    assert fake.put[0]["Bucket"] == "imt-backups"
    assert fake.put[0]["ContentType"] == "application/json"


def test_a_file_is_streamed_rather_than_read_into_memory(configured, monkeypatch, tmp_path):
    """The PDF archive is the whole content library; holding it in the web
    process is how a backup takes the API down with it."""
    fake = FakeS3()
    monkeypatch.setattr(object_storage, "_client", lambda: fake)
    archive = tmp_path / "pdfs.zip"
    archive.write_bytes(b"PK\x03\x04 not really a zip")

    object_storage.upload_file("files", "pdfs.zip", str(archive), "application/zip")

    body = fake.put[0]["Body"]
    assert hasattr(body, "read"), "the body should be a file handle, not bytes"


def test_an_upload_failure_is_reported_not_swallowed(configured, monkeypatch):
    class Broken(FakeS3):
        def put_object(self, **kw):
            raise RuntimeError("bucket on fire")

    monkeypatch.setattr(object_storage, "_client", lambda: Broken())

    with pytest.raises(ObjectStorageError) as exc:
        object_storage.upload_bytes("database", "x.json", b"{}", "application/json")
    assert "bucket on fire" in str(exc.value)


# --- Pruning: the irreversible half ----------------------------------------- #


def test_pruning_keeps_the_newest_and_deletes_the_rest(configured, monkeypatch):
    keys = [f"im-telligence/database/day{i}.json" for i in range(5)]
    fake = FakeS3(keys)  # index 0 is newest
    monkeypatch.setattr(object_storage, "_client", lambda: fake)

    removed = prune("database", keep=2)

    assert set(removed) == set(keys[2:])
    assert keys[0] not in fake.deleted and keys[1] not in fake.deleted


def test_keep_zero_deletes_nothing(configured, monkeypatch):
    """A misconfigured 0 must keep everything, not wipe the bucket. When the
    operation cannot be undone, the fallback has to be the harmless direction."""
    fake = FakeS3([f"im-telligence/database/day{i}.json" for i in range(5)])
    monkeypatch.setattr(object_storage, "_client", lambda: fake)

    assert prune("database", keep=0) == []
    assert fake.deleted == []


def test_pruning_does_nothing_when_there_is_nothing_stale(configured, monkeypatch):
    fake = FakeS3(["im-telligence/database/only.json"])
    monkeypatch.setattr(object_storage, "_client", lambda: fake)

    assert prune("database", keep=14) == []
    assert fake.deleted == []


def test_one_kind_never_prunes_another(configured, monkeypatch):
    """Database snapshots are frequent and small, PDF archives rare and large.
    Counting them together would delete the wrong ones."""
    fake = FakeS3(
        [
            "im-telligence/database/a.json",
            "im-telligence/database/b.json",
            "im-telligence/files/pdfs.zip",
        ]
    )
    monkeypatch.setattr(object_storage, "_client", lambda: fake)

    prune("database", keep=1)

    assert fake.deleted == ["im-telligence/database/b.json"]
    assert "im-telligence/files/pdfs.zip" not in fake.deleted


def test_listing_returns_newest_first(configured, monkeypatch):
    fake = FakeS3([f"im-telligence/database/day{i}.json" for i in range(3)])
    monkeypatch.setattr(object_storage, "_client", lambda: fake)

    listed = object_storage.list_backups("database")

    assert [o.key for o in listed] == [
        "im-telligence/database/day0.json",
        "im-telligence/database/day1.json",
        "im-telligence/database/day2.json",
    ]
    assert all(isinstance(o, StoredObject) for o in listed)
