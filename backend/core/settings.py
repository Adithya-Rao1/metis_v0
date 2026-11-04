"""Application settings. All secrets come from the environment; none has a real default."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_MARKERS = ("changeme", "change-me", "placeholder", "example", "xxxx")

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="METIS_", extra="ignore")

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
            elif any(m in raw.lower() for m in PLACEHOLDER_MARKERS):
                problems.append(f"METIS_{name.upper()} looks like a placeholder")
        if self.secret_key and len(self.secret_key.get_secret_value()) < 32:
            problems.append("METIS_SECRET_KEY must be at least 32 characters")
        if self.database_url and self.database_url.get_secret_value().split("://", 1)[-1].startswith(
            ("postgres:", "postgres@")
        ):
            problems.append("METIS_DATABASE_URL must not use the postgres superuser")
        if problems:
            # Names only; never include secret values in the message.
            raise SettingsError("refusing to start in production: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
