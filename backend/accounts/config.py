"""Accounts configuration. Secrets derive from METIS_SECRET_KEY; nothing has an unsafe default."""

from __future__ import annotations

import hashlib
import hmac

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.settings import get_settings

COOKIE_SESSION = "metis_session"
COOKIE_CSRF = "metis_csrf"
CSRF_HEADER = "X-CSRF-Token"
JWT_ALG = "HS256"
JWT_ISSUER = "metis"


class AccountsConfig(BaseSettings):
    """Tunables (env prefix METIS_ACCOUNTS_). The required terms version is METIS_TERMS_VERSION."""

    model_config = SettingsConfigDict(env_prefix="METIS_ACCOUNTS_", extra="ignore", populate_by_name=True)

    terms_version: str = Field(default="2026-01", validation_alias="METIS_TERMS_VERSION")
    bcrypt_cost: int = Field(default=12, ge=4, le=16)
    session_ttl_s: int = Field(default=3600, ge=60)
    verify_ttl_hours: int = Field(default=24, ge=1, le=24)
    cookie_secure: bool = True
    trust_proxy: bool = False  # honour the last X-Forwarded-For hop only behind our own proxy
    # (limit, window_seconds)
    rl_register_ip: tuple[int, int] = (10, 3600)
    rl_register_email: tuple[int, int] = (3, 3600)
    rl_login_ip: tuple[int, int] = (30, 300)
    rl_login_email: tuple[int, int] = (10, 900)
    rl_verify_ip: tuple[int, int] = (20, 900)
    rl_delete_user: tuple[int, int] = (5, 900)


def _derive(label: bytes) -> bytes:
    key = get_settings().secret_key
    if key is None or not key.get_secret_value():
        raise RuntimeError("METIS_SECRET_KEY is not set")
    return hmac.new(key.get_secret_value().encode(), b"metis-accounts:" + label, hashlib.sha256).digest()


def jwt_key() -> bytes:
    return _derive(b"jwt")


def csrf_key() -> bytes:
    return _derive(b"csrf")


def pepper() -> bytes:
    """Server-side pepper for rate-limit key hashing; raw IPs/emails are never stored."""
    return _derive(b"rate-limit-pepper")
