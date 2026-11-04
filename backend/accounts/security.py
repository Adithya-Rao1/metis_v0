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
_COMMON_BASES = frozenset(
    """password passw0rd p4ssword pass1234 passwort administrator admin adminadmin letmein letmein123 welcome
    welcome1 qwerty qwertyuiop qwertyui qwerty123 qwertz azerty asdfgh asdfghjkl zxcvbn zxcvbnm 1qaz2wsx
    1q2w3e4r 1q2w3e4r5t iloveyou iloveu ilovemylife monkey dragon master sunshine princess football baseball
    soccer hockey batman superman starwars trustno1 shadow michael jennifer jordan hunter ranger buster
    thomas robert charlie daniel andrew george summer winter spring autumn secret changeme changeit default
    login access flower hello hello123 hellohello freedom whatever cheese computer internet google metis
    university student science chemistry biology abcdefgh abcdefghij abcd1234 abc12345 aaaaaaaa mustang
    killer pepper ginger cookie banana chocolate maggie jessica hannah ashley nicole liverpool arsenal
    chelsea barcelona madrid yankees lakers matrix master123 test1234 testtest tester guest root toor
    """
    .split()
)
# Short list of fully-literal weak passwords (digits-only runs are caught separately).
_COMMON = frozenset(
    {"1234567890", "0123456789", "9876543210", "1234512345", "0000000000", "1111111111", "qwertyuiop",
     "asdfghjkl;", "zaq12wsxcde3", "correcthorsebatterystaple"}
)
_LEET = str.maketrans("@$!0134578", "asioleastb")


def _local_part(email: str) -> str:
    return email.lower().split("@", 1)[0]


def password_problem(pw: str, email: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if len(pw) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} characters"
    low = pw.lower()
    local = _local_part(email) if email else ""
    stripped = low.rstrip("0123456789!@#$%^&*()._-+= ")
    leet = stripped.translate(_LEET).rstrip("0123456789")
    if (
        low in _COMMON
        or low == email.lower()
        or (local and (low == local or stripped == local))
        or stripped in _COMMON_BASES
        or leet in _COMMON_BASES
        or len(set(low)) < 4
        or low.isdigit()
    ):
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
def pw_version(password_hash: str) -> str:
    """Short fingerprint of the current password hash; changing the password invalidates every old session."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_session_token(
    user_id: uuid.UUID, ttl_s: int, pwv: str | None = None
) -> tuple[str, uuid.UUID, int]:
    now = int(time.time())
    jti = uuid.uuid4()
    exp = now + ttl_s
    claims = {"sub": str(user_id), "jti": str(jti), "iat": now, "exp": exp, "iss": JWT_ISSUER}
    if pwv:
        claims["pwv"] = pwv
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


# ---- CSRF: signed double-submit token bound to the session (jti) or "anon", with an issue timestamp ----
ANON_BIND = "anon"


def _csrf_sig(bind: str, nonce: str, ts: str) -> str:
    return hmac.new(csrf_key(), f"{bind}|{nonce}|{ts}".encode(), hashlib.sha256).hexdigest()[:32]


def make_csrf_token(bind: str = ANON_BIND) -> str:
    nonce, ts = secrets.token_urlsafe(24), str(int(time.time()))
    return f"{nonce}.{ts}.{_csrf_sig(bind, nonce, ts)}"


def csrf_valid(cookie: str | None, header: str | None, bind: str = ANON_BIND, max_age_s: int = 3600) -> bool:
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        return False
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    nonce, ts, sig = parts
    if not ts.isdigit() or not (0 <= time.time() - int(ts) <= max_age_s):
        return False
    return hmac.compare_digest(sig.encode(), _csrf_sig(bind, nonce, ts).encode())


# ---- opaque one-time tokens and rate-limit keys ----
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def rl_key(kind: str, route: str, value: str) -> str:
    """64-hex HMAC of kind|route|value under the server pepper; the raw value is never stored."""
    msg = f"{kind}\x00{route}\x00{value}".encode()
    return hmac.new(pepper(), msg, hashlib.sha256).hexdigest()
