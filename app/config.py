"""Application configuration, loaded from environment variables / .env.

Uses pydantic-settings so every value is validated and typed. Security-sensitive
defaults (the JWT secret) are rejected when running in production.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "production"] = "development"

    secret_key: str = INSECURE_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"

    database_url: str = "sqlite:///./im_telligence.db"

    # Directory where uploaded lesson files (PDFs) are stored on disk.
    upload_dir: str = "./storage/files"
    max_upload_mb: int = 20

    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    cors_origins: str = "http://localhost:3000"

    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    max_failed_logins: int = 5
    lockout_minutes: int = 15
    max_lockout_minutes: int = 1440
    login_ip_max_failures: int = 5
    login_ip_window_minutes: int = 15
    login_ip_ban_cycles: int = 2

    # --- Teacher chat history ---------------------------------------------- #
    # How long a teacher's conversations are kept. Raising it is free; lowering
    # it deletes everything newly outside the window on the next daily purge,
    # and that is not recoverable. 0 disables the purge entirely.
    chat_retention_days: int = 365

    # --- Lesson sequencing ------------------------------------------------- #
    # After a teacher completes a lesson, the next lesson in the same
    # grade+language track unlocks this many days later. A super-admin can
    # override per teacher+lesson at any time.
    lesson_unlock_wait_days: int = 7

    # --- Teacher AI assistant ---------------------------------------------- #
    # Active provider. Falls back to "mock" automatically if its key is unset.
    ai_provider: Literal["mock", "groq", "grok", "openai", "anthropic"] = "groq"
    groq_api_key: str = ""
    xai_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    grok_model: str = "grok-2-latest"
    openai_model: str = "gpt-5.4-mini"
    anthropic_model: str = "claude-sonnet-4-6"
    ai_max_context_chars: int = 24000
    ai_timeout_seconds: int = 30

    # --- Teacher assistant: visual slide inspection ------------------------ #
    # When enabled AND the active provider supports images, each teacher
    # question also sends a rendered image of the slide they are looking at, so
    # the assistant can read diagrams, wiring and block code. Disable to fall
    # back to text-only answers without any other behaviour change (rollback
    # switch). Only the single visible slide is ever sent, never the whole PDF.
    ai_teacher_vision_enabled: bool = True
    # Hard ceiling on the encoded slide image. The renderer steps the quality
    # and scale down until it fits, and gives up (text-only) if it cannot.
    ai_max_image_bytes: int = 1_500_000
    # OpenAI image fidelity: "high" keeps small pin labels and block text legible.
    ai_image_detail: Literal["low", "high", "auto"] = "high"
    ai_teacher_daily_limit: int = 40
    ai_teacher_hourly_limit: int = 15
    ai_admin_daily_limit: int = 15
    ai_admin_hourly_limit: int = 5

    # --- Email delivery for DB backups ------------------------------------ #
    # Resend (https://resend.com) is preferred for production. If a Resend API
    # key is set, backups are emailed via Resend; otherwise the app falls back
    # to the SMTP settings below (e.g. Gmail), so SMTP stays a working backup.
    resend_api_key: str = ""
    # Sender address. For production set a verified-domain address; Resend's
    # shared "onboarding@resend.dev" works for testing to the account owner.
    resend_from: str = "onboarding@resend.dev"

    # --- SMTP (fallback for emailing the DB backup) ----------------------- #
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True

    # --- Automated daily database backup ---------------------------------- #
    # When enabled, the server emails a full DB backup to backup_email_to every
    # backup_interval_hours, using the email provider resolved above.
    backup_email_enabled: bool = False
    backup_email_to: str = ""
    backup_interval_hours: int = 24

    # --- Admin notifications ---------------------------------------------- #
    # Where teacher lesson-access requests are emailed. If empty, the app falls
    # back to the email addresses of all super-admin accounts.
    admin_email: str = ""

    # --- First-run super-admin bootstrap ---------------------------------- #
    # On startup, if there are NO super-admin accounts yet and these are set, an
    # active super-admin is created so a fresh production DB is usable. Ignored
    # once any super-admin exists.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_name: str = "Super Admin"

    # --- Sign-in location lookup ------------------------------------------ #
    # Off by default. "maxmind" reads a local GeoLite2 .mmdb (set geoip_db_path,
    # pip install geoip2) and sends nothing anywhere. "http" calls a third-party
    # service, which hands that service your users' IP addresses — an explicit
    # choice, never a default. An IP gives a city at best, never a person.
    geoip_provider: Literal["none", "maxmind", "http"] = "none"
    geoip_db_path: str = ""
    # {ip} is substituted, e.g. "https://ipapi.co/{ip}/json/".
    geoip_api_url: str = ""
    # How much of a resolved place to believe. Databases map an address to where
    # the ISP routes it, so in countries with one incumbent and a central
    # gateway every subscriber resolves to the capital — measured on Lebanese
    # ranges, where correct addresses all reported "Beirut". Country is right
    # even when the city is not, so country is the default. The full place is
    # still stored; this only decides how much of it is shown.
    geoip_precision: Literal["country", "city"] = "country"

    # --- Sign-in that doesn't add up -------------------------------------- #
    # One account signing in from two different networks inside this window.
    # Nobody is on two networks at once, so it is either a shared password or a
    # stolen one. Warning-only: geolocation and carrier NAT are wrong often
    # enough that locking teachers out on this would be worse than the problem.
    signin_window_minutes: int = 10

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("secret_key")
    @classmethod
    def _secret_not_default_in_prod(cls, v: str, info) -> str:
        # `info.data` may not yet contain `environment` depending on field order,
        # so we re-check in `validate()` below as well. This guards the common case.
        return v

    def validate_runtime(self) -> None:
        """Fail fast if the deployment is insecure. Call once at startup."""
        if self.is_production and self.secret_key == INSECURE_DEFAULT_SECRET:
            raise RuntimeError(
                "SECRET_KEY is still the insecure default while ENVIRONMENT=production. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        if self.is_production and not self.cookie_secure:
            raise RuntimeError("COOKIE_SECURE must be true in production (HTTPS).")
        # Browsers only accept SameSite=None cookies when they are also Secure —
        # this is the cross-site setup (frontend and API on different domains).
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise RuntimeError("COOKIE_SAMESITE=none requires COOKIE_SECURE=true.")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
