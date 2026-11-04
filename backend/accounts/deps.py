"""Request-scoped dependencies: app wiring, CSRF, authentication (`current_user`), rate limiting.

Wiring lives on `app.state` (injectable): `session_factory`, `accounts_config`, `mailer`.
Identity reaches Postgres only through `backend.core.db.request_session` (SET LOCAL semantics).
"""

from __future__ import annotations

import hmac
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.accounts import security
from backend.accounts.config import COOKIE_CSRF, COOKIE_SESSION, CSRF_HEADER, AccountsConfig
from backend.accounts.mailer import ConsoleMailer, Mailer
from backend.core.db import make_engine, make_session_factory, request_session
from backend.schemas.interfaces import UserCtx

log = logging.getLogger("metis.accounts")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TERMS_DOCS = ("terms", "privacy", "disclaimer")
_lock = threading.Lock()


def get_config(request: Request) -> AccountsConfig:
    cfg = getattr(request.app.state, "accounts_config", None)
    if cfg is None:
        with _lock:
            cfg = getattr(request.app.state, "accounts_config", None)
            if cfg is None:
                cfg = request.app.state.accounts_config = AccountsConfig()
    return cfg


def get_factory(request: Request) -> sessionmaker[Session]:
    f = getattr(request.app.state, "session_factory", None)
    if f is None:
        with _lock:
            f = getattr(request.app.state, "session_factory", None)
            if f is None:
                f = request.app.state.session_factory = make_session_factory(make_engine())
    return f


def get_mailer(request: Request) -> Mailer:
    m = getattr(request.app.state, "mailer", None)
    if m is None:
        m = request.app.state.mailer = ConsoleMailer()
    return m


def client_ip(request: Request, cfg: AccountsConfig) -> str:
    if cfg.trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def csrf_bind(request: Request) -> str:
    """CSRF tokens are bound to the session jti when a valid session cookie is present, else to "anon"."""
    token = request.cookies.get(COOKIE_SESSION)
    claims = security.read_session_token(token) if token else None
    return claims["jti"] if claims else security.ANON_BIND


def csrf_protect(request: Request) -> None:
    """Double-submit check on every mutating request, including pre-login ones."""
    if request.method in SAFE_METHODS:
        return
    cfg = get_config(request)
    if not security.csrf_valid(
        request.cookies.get(COOKIE_CSRF),
        request.headers.get(CSRF_HEADER),
        csrf_bind(request),
        cfg.session_ttl_s,
    ):
        raise HTTPException(403, "csrf_failed")


PURGE_PROBABILITY = 0.01
PURGE_MIN_AGE_S = 7200  # longer than every configured window (max 3600 s)


def purge_expired_buckets(factory: sessionmaker[Session], older_than_s: int = PURGE_MIN_AGE_S) -> int:
    """Opportunistic cleanup (F-65): drop buckets whose window ended long ago. Never raises."""
    try:
        with request_session(factory, None) as s:
            res = s.execute(
                text("DELETE FROM rate_limit_bucket WHERE window_start < now() - make_interval(secs => :age)"),
                {"age": older_than_s},
            )
            return res.rowcount or 0
    except Exception:  # best effort; the limiter must keep working
        log.warning("rate_limit_bucket purge failed (needs SELECT(window_start) grant?)")
        return 0


def rate_limit(factory: sessionmaker[Session], key_hash: str, limit: int, window_s: int) -> None:
    """Fixed-window counter via rate_limit_hit (own transaction so a rejected request still counts)."""
    window_start = int(time.time()) // window_s * window_s
    with request_session(factory, None) as s:
        hits = s.execute(
            text("SELECT rate_limit_hit(:k, to_timestamp(:w))"), {"k": key_hash, "w": window_start}
        ).scalar_one()
    if random.random() < PURGE_PROBABILITY:
        purge_expired_buckets(factory, max(2 * window_s, PURGE_MIN_AGE_S))
    if hits > limit:
        retry = max(1, window_s - (int(time.time()) - window_start))
        raise HTTPException(429, "rate_limited", headers={"Retry-After": str(retry)})


@dataclass(frozen=True)
class Session_:
    user: UserCtx
    jti: uuid.UUID
    exp: int


def _authenticate(request: Request) -> Session_:
    token = request.cookies.get(COOKIE_SESSION)
    claims = security.read_session_token(token) if token else None
    if claims is None:
        raise HTTPException(401, "not_authenticated")
    uid, jti = uuid.UUID(claims["sub"]), uuid.UUID(claims["jti"])
    factory = get_factory(request)
    with request_session(factory, None) as s:
        revoked = s.execute(text("SELECT session_is_revoked(:j)"), {"j": jti}).scalar_one()
    if revoked:
        raise HTTPException(401, "not_authenticated")
    with request_session(factory, uid) as s:
        row = s.execute(
            text(
                "SELECT tier, email_verified_at IS NOT NULL AS v, password_hash FROM app_user "
                "WHERE id = :u AND deleted_at IS NULL"
            ),
            {"u": uid},
        ).one_or_none()
    if row is None or not row.v:
        raise HTTPException(401, "not_authenticated")
    pwv = claims.get("pwv")  # absent only on tokens we no longer issue; present tokens die on password change
    if pwv is not None and not hmac.compare_digest(str(pwv), security.pw_version(row.password_hash)):
        raise HTTPException(401, "not_authenticated")
    return Session_(UserCtx(id=uid, tier=row.tier, email_verified=row.v), jti, int(claims["exp"]))


def authed_session(request: Request) -> Session_:
    """CSRF + authentication; no terms check (re-accept, logout, deletion use this)."""
    csrf_protect(request)
    return _authenticate(request)


def current_user_no_terms(request: Request) -> UserCtx:
    return authed_session(request).user


def terms_current(factory: sessionmaker[Session], uid: uuid.UUID, required: str) -> bool:
    with request_session(factory, uid) as s:
        rows = s.execute(
            text("SELECT document, version FROM terms_current WHERE owner_id = :u"), {"u": uid}
        ).all()
    have: dict[str, Any] = {r.document: r.version for r in rows}
    return all(have.get(d) == required for d in TERMS_DOCS)


def current_user(request: Request) -> UserCtx:
    """FastAPI dependency for every authenticated route.

    Mutating methods additionally require a valid CSRF double-submit and the current terms version
    (403 `terms_reacceptance_required` until re-accepted).
    """
    sess = authed_session(request)
    required = get_config(request).terms_version
    if request.method not in SAFE_METHODS and not terms_current(get_factory(request), sess.user.id, required):
        raise HTTPException(403, "terms_reacceptance_required")
    return sess.user
