"""The backup path against a real S3 implementation rather than a fake client.

The unit tests next door assert the logic; this asserts the wire format. A
signature, a key layout or a paginated listing can be wrong in a way no
hand-written stub notices, and the place that discovers it should not be the
first restore.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import object_storage  # noqa: E402

BUCKET = "imt-backups"


@pytest.fixture()
def s3(monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        for key, value in {
            "backup_storage_enabled": True,
            "backup_storage_bucket": BUCKET,
            "backup_storage_endpoint_url": "",
            "backup_storage_access_key_id": "test",
            "backup_storage_secret_access_key": "test",
            "backup_storage_region": "us-east-1",
            "backup_storage_prefix": "im-telligence",
        }.items():
            monkeypatch.setattr(settings, key, value)
        yield client


def test_a_snapshot_survives_the_round_trip_byte_for_byte(s3):
    payload = b'{"tables": {"users": [{"id": "u_1"}]}}'

    key = object_storage.upload_bytes(
        "database", "snap.json", payload, "application/json"
    )

    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == payload


def test_a_zip_on_disk_uploads_intact(s3, tmp_path):
    archive = tmp_path / "pdfs.zip"
    payload = b"PK\x03\x04" + bytes(range(256)) * 4
    archive.write_bytes(payload)

    key = object_storage.upload_file("files", "pdfs.zip", str(archive), "application/zip")

    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == payload


def test_pruning_removes_only_the_kind_it_was_asked_about(s3, tmp_path):
    for i in range(4):
        object_storage.upload_bytes("database", f"snap-{i}.json", b"{}", "application/json")
    archive = tmp_path / "pdfs.zip"
    archive.write_bytes(b"PK\x03\x04")
    object_storage.upload_file("files", "pdfs.zip", str(archive), "application/zip")

    removed = object_storage.prune("database", keep=2)

    assert len(removed) == 2
    assert len(object_storage.list_backups("database")) == 2
    assert len(object_storage.list_backups("files")) == 1, (
        "the PDF archive must survive a database prune"
    )


def test_listing_an_empty_kind_is_empty_not_an_error(s3):
    assert object_storage.list_backups("files") == []
