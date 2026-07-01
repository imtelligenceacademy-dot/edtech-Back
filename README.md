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
cd backend
py -3.11 -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then edit SECRET_KEY for anything real
python -m app.seed --reset      # creates im_telligence.db with demo data
uvicorn app.main:app --reload
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

### Demo credentials (dev seed only)

Every seeded account uses the password `Password123!`.

| Role         | Email                       |
|--------------|-----------------------------|
| Super admin  | georgio@im-telligence.com   |
| School admin | rania@antonine.edu.lb       |
| Teacher      | nour@antonine.edu.lb        |

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

## Notes

- `Base.metadata.create_all` runs on startup for dev convenience. For real
  deployments, switch to Alembic migrations (the dependency is already included).
