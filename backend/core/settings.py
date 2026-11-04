"""Application settings. All secrets come from the environment; none has a real default."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PLACEHOLDER_MARKERS = ("changeme", "change-me", "placeholder", "example", "xxxx", "set-me", "your-")
_ANGLE_PLACEHOLDER = re.compile(r"<[^<>]*>")
# Database users that are never acceptable for the API (superuser / schema owner).
FORBIDDEN_DB_USERS = frozenset({"postgres", "metis_owner", "root", "admin", "superuser"})

# Fields that must be set (and not be placeholders) when METIS_ENV=production.
REQUIRED_SECRETS = (
    "database_url",
    "secret_key",
    "redis_url",
    "object_store_endpoint",
    "object_store_access_key",
    "object_store_secret_key",
)


class SettingsError(ValueError):
    """Raised when production configuration is incomplete or unsafe."""


def _db_user_forbidden(url: str) -> bool:
    try:
        u = make_url(url)
    except Exception:  # noqa: BLE001 - unparsable URL: report generically, never echo it
        return True
    user = (u.username or str(u.query.get("user", ""))).lower()
    return user in FORBIDDEN_DB_USERS or "user" in u.query


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="METIS_", extra="ignore", hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Secrets / connection strings: never defaulted to a usable value.
    database_url: SecretStr | None = None  # must connect as the least-privilege metis_app login role
    secret_key: SecretStr | None = None
    redis_url: SecretStr | None = None
    object_store_endpoint: str | None = None
    object_store_access_key: SecretStr | None = None
    object_store_secret_key: SecretStr | None = None
    object_store_bucket: str = "metis"

    db_pool_size: int = 5
    db_statement_timeout_ms: int = 30000

    @model_validator(mode="after")
    def _production_requires_secrets(self) -> Settings:
        if self.env != "production":
            return self
        problems: list[str] = []
        for name in REQUIRED_SECRETS:
            val = getattr(self, name)
            raw = val.get_secret_value() if isinstance(val, SecretStr) else val
            if not raw:
                problems.append(f"METIS_{name.upper()} is missing")
            elif any(m in raw.lower() for m in PLACEHOLDER_MARKERS) or _ANGLE_PLACEHOLDER.search(raw):
                problems.append(f"METIS_{name.upper()} looks like a placeholder")
        if self.secret_key:
            key = self.secret_key.get_secret_value()
            if len(key) < 32:
                problems.append("METIS_SECRET_KEY must be at least 32 characters")
            elif len(set(key)) < 4:
                problems.append("METIS_SECRET_KEY is trivially guessable")
        if self.database_url and _db_user_forbidden(self.database_url.get_secret_value()):
            problems.append("METIS_DATABASE_URL must not use the postgres superuser or schema owner")
        if problems:
            # Names only; never include secret values in the message.
            raise SettingsError("refusing to start in production: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Names/messages only; pydantic's default rendering can echo raw METIS_* input values.
        msgs = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        raise SettingsError(msgs) from None
