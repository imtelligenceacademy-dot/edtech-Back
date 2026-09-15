"""The health check, and whether it can fail.

It returned a constant, which is a check that the process is running — something
the fact of answering already proves. Every way this app really fails leaves it
answering perfectly: the database unreachable, the pool exhausted, a deploy
whose migrations did not run. All of those returned "ok".

That is worse than having no health check, because a deploy gated on it goes
green over an application that cannot serve a page, and the green is taken as
evidence. So what is asserted here is mostly the failing direction: these tests
exist to prove this endpoint is capable of saying no.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import main as app_main
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_a_healthy_instance_says_so(client):
    res = client.get("/health")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_it_reports_the_schema_it_is_running(client):
    """The other thing worth knowing after a deploy, and previously invisible
    from outside — inferring it took a login attempt against production."""
    body = client.get("/health").json()

    assert body["revision"], "a migrated database has a recorded revision"


def test_an_unreachable_database_fails_the_check(client, monkeypatch):
    """The whole point. A 200 here while the database is gone is what let a
    broken deploy pass."""

    def refuse():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(app_main.engine, "connect", refuse)

    res = client.get("/health")

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unreachable"


def test_a_failure_does_not_tell_the_internet_why(client, monkeypatch):
    """This endpoint is public and a driver error names the host it failed to
    reach and the user it tried. That belongs in the log, not the body."""

    def refuse():
        raise OperationalError(
            "SELECT 1",
            {},
            Exception("could not connect to host=db.internal user=imt_prod password=hunter2"),
        )

    monkeypatch.setattr(app_main.engine, "connect", refuse)

    raw = client.get("/health").text

    for leaked in ("db.internal", "imt_prod", "hunter2", "could not connect"):
        assert leaked not in raw, f"the response leaks {leaked!r}"


def test_a_database_that_cannot_name_its_revision_is_still_healthy(client, monkeypatch):
    """A diagnostic gap is not a reason to fail a deploy. The status stays on the
    question that matters — can this instance reach its database — and the
    revision is reported as null."""
    real_connect = app_main.engine.connect

    class _NoRevision:
        def __init__(self, conn):
            self._conn = conn
            self._calls = 0

        def execute(self, *args, **kwargs):
            self._calls += 1
            if self._calls == 1:
                return self._conn.execute(*args, **kwargs)
            raise OperationalError("version_num", {}, Exception("no such table"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

    def connect():
        return _NoRevision(real_connect().__enter__())

    monkeypatch.setattr(app_main.engine, "connect", connect)

    res = client.get("/health")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["revision"] is None
