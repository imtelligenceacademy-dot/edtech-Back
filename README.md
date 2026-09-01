# IM-Telligence — Backend API

A FastAPI + SQLAlchemy (SQLite) backend for the IM-Telligence teacher platform.

## Stack

| Concern           | Choice                                             |
|-------------------|----------------------------------------------------|
| Web framework     | FastAPI                                             |
| ORM / DB          | SQLAlchemy 2.0 + SQLite (`im_telligence.db`)        |
| Validation        | Pydantic v2 (camelCase output to match the TS types)|
| Password hashing  | Argon2id (`argon2-cffi`)                            |
| Sessions          | JWT access + rotating refresh, both httpOnly cookies|
| Authorization     | Server-side RBAC mirroring `lib/permissions.ts`     |

## Security model

- **Passwords** are stored only as Argon2id hashes; hashes are transparently
  upgraded on login if the parameters change.
- **Access tokens** are short-lived (15 min) JWTs; **refresh tokens** are opaque,
  rotated on every refresh, and stored as SHA-256 hashes so a DB leak yields no
  usable tokens. Both are delivered as `httpOnly`, `SameSite`, `Secure`-capable
  cookies — JavaScript (and XSS) cannot read them.
- **Account lockout** after 5 failed logins for 15 minutes.
- **Uniform errors** on login/registration so the API never reveals whether an
  email exists.
- **RBAC + school scoping** is enforced on every route, not just the UI.
- **Security headers** + CORS locked to the configured frontend origin.
- The app **refuses to boot in production** with the default secret or non-secure
  cookies.

## Setup

```bash
py -3.11 -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then edit SECRET_KEY for anything real
uvicorn app.main:app --reload
```

The schema is created on first start — see **Migrations** below. There is no
seed script; the first account comes from the bootstrap settings instead.

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

### Your first login

A fresh database has no accounts, so set these in `.env` before the first
start and an active super-admin is created for you:

```
BOOTSTRAP_ADMIN_EMAIL=you@yourdomain.com
BOOTSTRAP_ADMIN_PASSWORD=<something long>
```

Use a real-looking domain. Reserved TLDs such as `.test` or `.local` are
accepted when the account is created but rejected by the login endpoint's
email validation, which leaves an account that cannot sign in.

This is a no-op once any super-admin exists, so it is safe to leave configured.

## Migrations

Alembic owns the schema. `app/migrate.py` runs on startup and handles three
cases: an empty database (build it from the migrations), a database from before
Alembic existed (level it, then stamp it), and the normal case (apply whatever
is new). Startup is idempotent, so a restart costs nothing.

After changing a model:

```bash
alembic revision --autogenerate -m "what changed"
```

Read the generated file before committing it — autogenerate does not detect
renames (it emits a drop plus an add, which loses the data) and never writes
the backfill a new NOT NULL column needs.

```bash
alembic upgrade head        # apply
alembic downgrade -1        # undo the last one
alembic current             # what this database is stamped at
alembic upgrade head --sql  # print the SQL instead of running it
```

`alembic.ini` carries no database URL on purpose: `migrations/env.py` takes it
from the application's own settings, so a migration cannot run against a
different database than the app uses.

A test fails if the models and migrations disagree, so a model change without a
migration is caught in CI rather than on deploy.

## Key endpoints

| Method | Path                          | Who                         |
|--------|-------------------------------|-----------------------------|
| POST   | `/api/auth/register`          | public (lands `pending`)    |
| POST   | `/api/auth/login`             | public                      |
| POST   | `/api/auth/refresh`           | cookie                      |
| POST   | `/api/auth/logout`            | cookie                      |
| GET    | `/api/auth/me`                | authenticated               |
| GET    | `/api/users`                  | super / school-admin        |
| PATCH  | `/api/users/{id}/status`      | super (approve/suspend)     |
| GET    | `/api/schools`                | scoped                      |
| GET    | `/api/lessons`                | scoped (teacher = assigned) |
| POST   | `/api/lessons/{id}/assign`    | super                       |
| GET    | `/api/progress`               | scoped                      |
| GET    | `/api/reports`                | super / school-admin        |
| GET    | `/api/security-logs`          | scoped                      |

## Tests

```bash
pytest -q
```

CI runs the suite on Python 3.11 and 3.13, and again against a real Postgres
service — development is SQLite and production is Postgres, and the differences
between them do not show up until deploy otherwise. It also applies and rolls
back every migration on Postgres for the same reason.

To run the suite against your own Postgres locally:

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/imt_test pytest -q
```

`TEST_DATABASE_URL` rather than `DATABASE_URL`, because the suite creates and
drops every table and must never be able to do that to a database you meant to
keep.
