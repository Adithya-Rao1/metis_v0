"""Password hashing, JWT, CSRF tokens, rate-limit key hashing. Never logs secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from functools import lru_cache
from typing import Any

import bcrypt
import jwt

from backend.accounts.config import JWT_ALG, JWT_ISSUER, csrf_key, jwt_key, pepper

MIN_PASSWORD = 10
MAX_PASSWORD = 128
_COMMON = frozenset(
    {"password12", "password123", "1234567890", "qwertyuiop", "letmein123", "iloveyou12", "administrator"}
)


def password_problem(pw: str, email: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if len(pw) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} characters"
    low = pw.lower()
    if low in _COMMON or low == email.lower() or len(set(low)) < 4 or low.isdigit():
        return "Password is too easy to guess"
    return None


def _prehash(pw: str) -> bytes:
    """bcrypt reads only 72 bytes; SHA-256 + base64 makes every character count (and avoids NUL bytes)."""
    return base64.b64encode(hashlib.sha256(pw.encode("utf-8")).digest())


def hash_password(pw: str, cost: int) -> str:
    return bcrypt.hashpw(_prehash(pw), bcrypt.gensalt(rounds=cost)).decode("ascii")


def check_password(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(pw), pw_hash.encode("ascii"))
    except ValueError:
        return False


@lru_cache(maxsize=8)
def _dummy_hash(cost: int) -> str:
    return hash_password(secrets.token_urlsafe(16), cost)


def burn_password_check(pw: str, cost: int) -> None:
    """Equalise timing when the user is unknown."""
    check_password(pw, _dummy_hash(cost))


# ---- JWT (algorithm pinned; none/unsigned and other algs are rejected) ----
def make_session_token(user_id: uuid.UUID, ttl_s: int) -> tuple[str, uuid.UUID, int]:
    now = int(time.time())
    jti = uuid.uuid4()
    exp = now + ttl_s
    claims = {"sub": str(user_id), "jti": str(jti), "iat": now, "exp": exp, "iss": JWT_ISSUER}
    return jwt.encode(claims, jwt_key(), algorithm=JWT_ALG), jti, exp


def read_session_token(token: str) -> dict[str, Any] | None:
    try:
        claims = jwt.decode(
            token,
            jwt_key(),
            algorithms=[JWT_ALG],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "sub", "jti", "iat", "iss"]},
        )
        uuid.UUID(claims["sub"])
        uuid.UUID(claims["jti"])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        return None
    return claims


# ---- CSRF: signed double-submit token (cookie value must equal header value) ----
def make_csrf_token() -> str:
    nonce = secrets.token_urlsafe(24)
    sig = hmac.new(csrf_key(), nonce.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{nonce}.{sig}"


def csrf_valid(cookie: str | None, header: str | None) -> bool:
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        return False
    nonce, _, sig = cookie.partition(".")
    expect = hmac.new(csrf_key(), nonce.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig.encode(), expect.encode())


# ---- opaque one-time tokens and rate-limit keys ----
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def rl_key(kind: str, route: str, value: str) -> str:
    """64-hex HMAC of kind|route|value under the server pepper; the raw value is never stored."""
    msg = f"{kind}\x00{route}\x00{value}".encode()
    return hmac.new(pepper(), msg, hashlib.sha256).hexdigest()
