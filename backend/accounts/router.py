"""Account endpoints. Pre-auth DB access only through the SECURITY DEFINER auth_* functions."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.accounts import security
from backend.accounts.config import COOKIE_CSRF, COOKIE_SESSION, AccountsConfig
from backend.accounts.deps import (
    TERMS_DOCS,
    Session_,
    authed_session,
    client_ip,
    csrf_bind,
    csrf_protect,
    current_user,
    current_user_no_terms,
    get_config,
    get_factory,
    get_mailer,
    rate_limit,
    terms_current,
)
from backend.core.db import request_session
from backend.schemas.contracts import (
    ENSEMBLE_SIZES,
    MAX_COMPOUNDS,
    MAX_ENDPOINTS,
    TIER_MAX_N,
    allowed_ensemble_sizes,
)
from backend.schemas.interfaces import UserCtx

log = logging.getLogger("metis.accounts")
router = APIRouter(tags=["accounts"])

_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BAD_CHARS = re.compile(
    "["
    + "".join(
        map(
            chr,
            [0x7F, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0xFEFF],
        )
    )
    + "\\x00-\\x1f]"
)
CONSENT_SCOPES = ("train_models", "share_dataset", "analytics", "contact")


def _norm_email(v: str) -> str:
    v = unicodedata.normalize("NFKC", v).strip().lower()
    if len(v) > 254 or _BAD_CHARS.search(v) or not _EMAIL_OK.match(v):
        raise ValueError("invalid email")
    return v


def _login_email(v: str) -> str:
    """Same canonical form as registration (NFKC + strip + lower) without rejecting: bad input just fails to log in."""
    return unicodedata.normalize("NFKC", v).strip().lower()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(_Body):
    email: str
    password: str = Field(max_length=security.MAX_PASSWORD)
    accepted_terms_version: str = Field(min_length=1, max_length=32)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _norm_email(v)


class LoginIn(_Body):
    email: str = Field(max_length=254)
    password: str = Field(max_length=security.MAX_PASSWORD)


class EmailIn(_Body):
    email: str = Field(max_length=254)


class ResetIn(_Body):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(max_length=security.MAX_PASSWORD)


class VerifyIn(_Body):
    token: str = Field(min_length=10, max_length=200)


class ConsentIn(_Body):
    scope: Literal["train_models", "share_dataset", "analytics", "contact"]
    granted: bool


class TermsIn(_Body):
    version: str = Field(min_length=1, max_length=32)


class ChangePasswordIn(_Body):
    current_password: str = Field(max_length=security.MAX_PASSWORD)
    new_password: str = Field(max_length=security.MAX_PASSWORD)


class DeleteIn(_Body):
    password: str = Field(max_length=security.MAX_PASSWORD)


def _audit(s: Any, uid: uuid.UUID, action: str, resource: str | None = None) -> None:
    s.execute(
        text("INSERT INTO audit_log (actor_id, action, resource) VALUES (:u, :a, :r)"),
        {"u": uid, "a": action, "r": resource},
    )


def _set_cookies(
    resp: Response, cfg: AccountsConfig, session_token: str | None, bind: str | None = None
) -> str:
    csrf = security.make_csrf_token(bind or security.ANON_BIND)
    if session_token:
        resp.set_cookie(
            COOKIE_SESSION,
            session_token,
            max_age=cfg.session_ttl_s,
            httponly=True,
            secure=cfg.cookie_secure,
            samesite="lax",
            path="/",
        )
    resp.set_cookie(
        COOKIE_CSRF,
        csrf,
        max_age=cfg.session_ttl_s,
        httponly=False,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
    )
    return csrf


@router.get("/auth/csrf")
def csrf(response: Response, request: Request) -> dict:
    """Issue a CSRF cookie for pre-login mutating calls (register, login, verify)."""
    return {"csrf_token": _set_cookies(response, get_config(request), None, csrf_bind(request))}


@router.get("/auth/terms")
def required_terms(request: Request) -> dict:
    return {"version": get_config(request).terms_version, "documents": ["terms", "privacy", "disclaimer"]}


# ---------------------------------------------------------------- register
@router.post("/auth/register", status_code=202, dependencies=[Depends(csrf_protect)])
def register(body: RegisterIn, request: Request) -> dict:
    cfg, factory, mailer = get_config(request), get_factory(request), get_mailer(request)
    ip = client_ip(request, cfg)
    rate_limit(factory, security.rl_key("ip", "register", ip), *cfg.rl_register_ip)
    rate_limit(factory, security.rl_key("email", "register", body.email), *cfg.rl_register_email)
    if body.accepted_terms_version != cfg.terms_version:
        raise HTTPException(400, "terms_acceptance_required")
    problem = security.password_problem(body.password, body.email)
    if problem:
        raise HTTPException(422, problem)
    pw_hash = security.hash_password(body.password, cfg.bcrypt_cost)  # always hashed: equal timing either way
    token = security.new_token()
    created = True
    try:
        with request_session(factory, None) as s:
            uid = s.execute(
                text("SELECT auth_register(:e, :h, :v)"),
                {"e": body.email, "h": pw_hash, "v": body.accepted_terms_version},
            ).scalar_one()
            s.execute(
                text("SELECT auth_issue_token(:u, 'verify_email', :t, make_interval(hours => :h))"),
                {"u": uid, "t": security.token_hash(token), "h": cfg.verify_ttl_hours},
            )
    except IntegrityError:
        created = False  # email already registered: same response, no signal to the caller
    if created:
        mailer.send_verification(body.email, token)
    elif not _resend_verification(factory, cfg, mailer, body.email):
        mailer.send_already_registered(body.email)
    return {"status": "verification_sent"}


def _resend_verification(factory: Any, cfg: AccountsConfig, mailer: Any, email: str) -> bool:
    """If `email` belongs to an UNVERIFIED account, mint a fresh token and mail it to that address.

    The stored password hash is never touched, so a stranger who registers someone else's address cannot
    change what the real owner's row looks like; the mail only ever goes to the address owner.
    """
    with request_session(factory, None) as s:
        row = s.execute(text("SELECT * FROM auth_lookup(:e)"), {"e": email}).one_or_none()
        if row is None or row.email_verified:
            return False
        token = security.new_token()
        s.execute(
            text("SELECT auth_issue_token(:u, 'verify_email', :t, make_interval(hours => :h))"),
            {"u": row.id, "t": security.token_hash(token), "h": cfg.verify_ttl_hours},
        )
    mailer.send_verification(email, token)
    return True


@router.post("/auth/resend-verification", status_code=202, dependencies=[Depends(csrf_protect)])
def resend_verification(body: EmailIn, request: Request) -> dict:
    cfg, factory, mailer = get_config(request), get_factory(request), get_mailer(request)
    email = _login_email(body.email)
    rate_limit(factory, security.rl_key("ip", "resend", client_ip(request, cfg)), *cfg.rl_resend_ip)
    rate_limit(factory, security.rl_key("email", "resend", email), *cfg.rl_resend_email)
    _resend_verification(factory, cfg, mailer, email)  # same response whether or not it applied
    return {"status": "verification_sent"}


# ------------------------------------------------------------ password reset
@router.post("/auth/forgot", status_code=202, dependencies=[Depends(csrf_protect)])
def forgot_password(body: EmailIn, request: Request) -> dict:
    cfg, factory, mailer = get_config(request), get_factory(request), get_mailer(request)
    email = _login_email(body.email)
    rate_limit(factory, security.rl_key("ip", "forgot", client_ip(request, cfg)), *cfg.rl_forgot_ip)
    rate_limit(factory, security.rl_key("email", "forgot", email), *cfg.rl_forgot_email)
    token = security.new_token()
    send = False
    with request_session(factory, None) as s:
        row = s.execute(text("SELECT * FROM auth_lookup(:e)"), {"e": email}).one_or_none()
        if row is not None and row.email_verified:
            s.execute(
                text("SELECT auth_issue_token(:u, 'reset_password', :t, make_interval(secs => :s))"),
                {"u": row.id, "t": security.token_hash(token), "s": cfg.reset_ttl_s},
            )
            send = True
    if send:
        mailer.send_reset(email, token)
    return {"status": "reset_sent"}


@router.post("/auth/reset", dependencies=[Depends(csrf_protect)])
def reset_password(body: ResetIn, request: Request) -> dict:
    cfg, factory = get_config(request), get_factory(request)
    rate_limit(factory, security.rl_key("ip", "reset", client_ip(request, cfg)), *cfg.rl_reset_ip)
    problem = security.password_problem(body.password, "")
    if problem:  # checked before the token is spent
        raise HTTPException(422, problem)
    pw_hash = security.hash_password(body.password, cfg.bcrypt_cost)
    with request_session(factory, None) as s:
        # One transaction: sets the hash, clears any lock, spends the token and audits (migration 0004). If the
        # email-based password rule below fails we raise, the transaction rolls back and the token stays valid.
        uid = s.execute(
            text("SELECT auth_reset_password(:t, :h)"), {"t": security.token_hash(body.token), "h": pw_hash}
        ).scalar_one()
        if uid is None:
            raise HTTPException(400, "invalid_or_expired_token")
        s.execute(text("SELECT set_config('metis.user_id', :u, true)"), {"u": str(uid)})
        email = s.execute(text("SELECT email_lower FROM app_user WHERE id = :u"), {"u": uid}).scalar_one()
        if security.password_problem(body.password, email):
            raise HTTPException(422, "Password is too easy to guess")
        # The new hash also invalidates every existing session (tokens carry a hash fingerprint).
    return {"status": "password_reset"}


# ------------------------------------------------------------------ verify
@router.post("/auth/verify", dependencies=[Depends(csrf_protect)])
def verify(body: VerifyIn, request: Request) -> dict:
    cfg, factory = get_config(request), get_factory(request)
    rate_limit(factory, security.rl_key("ip", "verify", client_ip(request, cfg)), *cfg.rl_verify_ip)
    with request_session(factory, None) as s:
        uid = s.execute(
            text("SELECT auth_consume_token(:t, 'verify_email')"), {"t": security.token_hash(body.token)}
        ).scalar_one()
        if uid is None:
            raise HTTPException(400, "invalid_or_expired_token")
        s.execute(text("SELECT auth_upgrade_edu(:u)"), {"u": uid})
    return {"status": "verified"}


# ------------------------------------------------------------------- login
def _invalid() -> HTTPException:
    return HTTPException(401, "invalid_credentials")


@router.post("/auth/login", dependencies=[Depends(csrf_protect)])
def login(body: LoginIn, request: Request, response: Response) -> dict:
    cfg, factory = get_config(request), get_factory(request)
    email = _login_email(body.email)
    rate_limit(factory, security.rl_key("ip", "login", client_ip(request, cfg)), *cfg.rl_login_ip)
    rate_limit(factory, security.rl_key("email", "login", email), *cfg.rl_login_email)
    with request_session(factory, None) as s:
        row = s.execute(text("SELECT * FROM auth_lookup(:e)"), {"e": email}).one_or_none()
    if row is None:
        security.burn_password_check(body.password, cfg.bcrypt_cost)
        raise _invalid()
    ok = security.check_password(body.password, row.password_hash)
    now = datetime.now(UTC)
    locked = row.locked_until is not None and row.locked_until > now
    with request_session(factory, None) as s:
        s.execute(text("SELECT auth_record_login(:u, :ok)"), {"u": row.id, "ok": ok})
    if not ok or locked:
        raise _invalid()
    if not row.email_verified:
        raise HTTPException(403, "email_not_verified")
    token, jti, _exp = security.make_session_token(
        row.id, cfg.session_ttl_s, security.pw_version(row.password_hash)
    )
    csrf_token = _set_cookies(response, cfg, token, str(jti))
    with request_session(factory, row.id) as s:
        t = s.execute(text("SELECT tier FROM app_user WHERE id = :u"), {"u": row.id}).scalar_one()
    reaccept = not terms_current(factory, row.id, cfg.terms_version)
    return {
        "user": {"id": str(row.id), "tier": t, "email_verified": True},
        "csrf_token": csrf_token,
        "terms_reaccept_required": reaccept,
    }


# ---------------------------------------------------------- change password
@router.post("/auth/change-password")
def change_password(
    body: ChangePasswordIn,
    sess: Annotated[Session_, Depends(authed_session)],
    request: Request,
    response: Response,
) -> dict:
    """Authenticated + CSRF (via authed_session). Needs the current password. Every other session dies because the
    session token carries a fingerprint of the password hash; this session continues with a re-issued cookie."""
    cfg, factory, uid = get_config(request), get_factory(request), sess.user.id
    rate_limit(factory, security.rl_key("user", "change_password", str(uid)), *cfg.rl_change_password_user)
    with request_session(factory, uid) as s:
        row = s.execute(
            text("SELECT password_hash, email_lower FROM app_user WHERE id = :u AND deleted_at IS NULL"),
            {"u": uid},
        ).one_or_none()
    if row is None or not security.check_password(body.current_password, row.password_hash):
        raise HTTPException(403, "password_incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(422, "New password must differ from the current one")
    problem = security.password_problem(body.new_password, row.email_lower)
    if problem:
        raise HTTPException(422, problem)
    new_hash = security.hash_password(body.new_password, cfg.bcrypt_cost)
    with request_session(factory, uid) as s:
        s.execute(text("UPDATE app_user SET password_hash = :h WHERE id = :u"), {"h": new_hash, "u": uid})
        s.execute(
            text(
                "INSERT INTO session_revocation (jti, owner_id, expires_at) VALUES (:j, :u, to_timestamp(:e)) "
                "ON CONFLICT (jti) DO NOTHING"
            ),
            {"j": sess.jti, "u": uid, "e": sess.exp},
        )
        _audit(s, uid, "password_change", f"app_user:{uid}")
    token, jti, _exp = security.make_session_token(uid, cfg.session_ttl_s, security.pw_version(new_hash))
    csrf_token = _set_cookies(response, cfg, token, str(jti))
    return {"status": "password_changed", "csrf_token": csrf_token}


@router.post("/auth/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request, response: Response) -> dict:
    cfg = get_config(request)
    try:
        sess: Session_ | None = authed_session(request)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        sess = None  # idempotent: nothing to revoke
    if sess is not None:
        with request_session(get_factory(request), sess.user.id) as s:
            s.execute(
                text(
                    "INSERT INTO session_revocation (jti, owner_id, expires_at) "
                    "VALUES (:j, :u, to_timestamp(:e))"
                ),
                {"j": sess.jti, "u": sess.user.id, "e": sess.exp},
            )
            _audit(s, sess.user.id, "logout", f"app_user:{sess.user.id}")
    response.delete_cookie(COOKIE_SESSION, path="/", secure=cfg.cookie_secure, httponly=True, samesite="lax")
    response.delete_cookie(COOKIE_CSRF, path="/", secure=cfg.cookie_secure, samesite="lax")
    return {"status": "logged_out"}


@router.get("/auth/me")
def me(user: Annotated[UserCtx, Depends(current_user_no_terms)], request: Request) -> dict:
    ok = terms_current(get_factory(request), user.id, get_config(request).terms_version)
    return {
        "id": str(user.id),
        "tier": user.tier,
        "email_verified": user.email_verified,
        "terms_current": ok,
        "terms_reaccept_required": not ok,
    }


# ------------------------------------------------------------------- terms
def _terms_state(factory: Any, uid: uuid.UUID, required: str) -> dict:
    with request_session(factory, uid) as s:
        rows = s.execute(
            text("SELECT document, version FROM terms_current WHERE owner_id = :u"), {"u": uid}
        ).all()
    have = {r.document: r.version for r in rows}
    accepted = {d: have.get(d) for d in TERMS_DOCS}
    ok = all(v == required for v in accepted.values())
    return {"required_version": required, "accepted_versions": accepted, "reaccept_required": not ok}


@router.get("/auth/terms/status")
def terms_status(user: Annotated[UserCtx, Depends(current_user_no_terms)], request: Request) -> dict:
    return _terms_state(get_factory(request), user.id, get_config(request).terms_version)


@router.post("/auth/terms/accept")
@router.post("/terms/accept")  # original path, kept for existing clients
def accept_terms(
    body: TermsIn, user: Annotated[UserCtx, Depends(current_user_no_terms)], request: Request
) -> dict:
    if body.version != get_config(request).terms_version:
        raise HTTPException(400, "terms_version_not_current")
    with request_session(get_factory(request), user.id) as s:
        for doc in ("terms", "privacy", "disclaimer"):
            s.execute(
                text("INSERT INTO terms_acceptance (owner_id, document, version) VALUES (:u, :d, :v)"),
                {"u": user.id, "d": doc, "v": body.version},
            )
        _audit(s, user.id, "terms_reaccept", f"app_user:{user.id}")
    return {"status": "accepted", "version": body.version}


# ---------------------------------------------------------------- consents
def _consent_state(s: Any, uid: uuid.UUID) -> dict:
    rows = s.execute(
        text("SELECT scope, granted_at, revoked_at FROM consent WHERE owner_id = :u ORDER BY granted_at, id"),
        {"u": uid},
    ).all()
    state = {sc: {"granted": False, "granted_at": None} for sc in CONSENT_SCOPES}
    history = []
    for r in rows:
        history.append({"scope": r.scope, "granted_at": r.granted_at, "revoked_at": r.revoked_at})
        if r.revoked_at is None:
            state[r.scope] = {"granted": True, "granted_at": r.granted_at}
    return {"consents": state, "history": history}


@router.get("/consents")
def get_consents(user: Annotated[UserCtx, Depends(current_user)], request: Request) -> dict:
    with request_session(get_factory(request), user.id) as s:
        return _consent_state(s, user.id)


@router.post("/consents")
def set_consent(body: ConsentIn, user: Annotated[UserCtx, Depends(current_user)], request: Request) -> dict:
    with request_session(get_factory(request), user.id) as s:
        if body.granted:
            s.execute(
                text(
                    "INSERT INTO consent (owner_id, scope) VALUES (:u, :s) ON CONFLICT (owner_id, scope) "
                    "WHERE revoked_at IS NULL DO NOTHING"
                ),
                {"u": user.id, "s": body.scope},
            )
        else:
            s.execute(
                text(
                    "UPDATE consent SET revoked_at = now() "
                    "WHERE owner_id = :u AND scope = :s AND revoked_at IS NULL"
                ),
                {"u": user.id, "s": body.scope},
            )
        _audit(s, user.id, "consent_grant" if body.granted else "consent_revoke", f"consent:{body.scope}")
        return _consent_state(s, user.id)


# ----------------------------------------------------------------- account
@router.get("/account")
def account(user: Annotated[UserCtx, Depends(current_user_no_terms)], request: Request) -> dict:
    """Summary for the account page: tier, caps, verification, edu flag, consent and terms state."""
    factory, cfg = get_factory(request), get_config(request)
    with request_session(factory, user.id) as s:
        row = s.execute(
            text(
                "SELECT tier, is_edu, email_verified_at IS NOT NULL AS v, created_at FROM app_user WHERE id = :u"
            ),
            {"u": user.id},
        ).one()
        consents = _consent_state(s, user.id)["consents"]
    return {
        "id": str(user.id),
        "tier": row.tier,
        "email_verified": row.v,
        "is_edu": row.is_edu,
        "created_at": row.created_at,
        "caps": {
            "max_ensemble_n": TIER_MAX_N[row.tier],
            "allowed_ensemble_sizes": list(allowed_ensemble_sizes(row.tier)),
            "all_ensemble_sizes": list(ENSEMBLE_SIZES),
            "max_compounds_per_project": MAX_COMPOUNDS,
            "max_endpoints_per_request": MAX_ENDPOINTS,
        },
        "consents": {k: v["granted"] for k, v in consents.items()},
        "terms": _terms_state(factory, user.id, cfg.terms_version),
    }


# ---------------------------------------------------------- export / delete
_OWNED = {
    "project": "SELECT to_jsonb(t) FROM project t WHERE t.owner_id = :u",
    "compound": "SELECT to_jsonb(t) FROM compound t WHERE t.owner_id = :u",
    "prediction": "SELECT to_jsonb(t) FROM prediction t WHERE t.owner_id = :u",
    "measurement": "SELECT to_jsonb(t) FROM measurement t WHERE t.owner_id = :u",
    "note": "SELECT to_jsonb(t) FROM note t WHERE t.owner_id = :u",
    "grants": "SELECT to_jsonb(t) FROM grant_ t WHERE t.owner_id = :u",
}


@router.get("/account/data")
def export_data(user: Annotated[UserCtx, Depends(current_user)], request: Request) -> Response:
    uid = user.id
    out: dict[str, Any] = {"exported_at": datetime.now(UTC).isoformat()}
    with request_session(get_factory(request), uid) as s:
        out["account"] = s.execute(
            text("SELECT to_jsonb(u) - 'password_hash' FROM app_user u WHERE u.id = :u"), {"u": uid}
        ).scalar_one()
        # Explicit owner filter: RLS also exposes shared projects/compounds, which are not this user's data.
        for name, sql in _OWNED.items():
            out[name] = list(s.execute(text(sql), {"u": uid}).scalars())
        for name, sql in (
            ("terms_acceptance", "SELECT to_jsonb(t) FROM terms_acceptance t WHERE t.owner_id = :u"),
            ("consents", "SELECT to_jsonb(t) FROM consent t WHERE t.owner_id = :u"),
            ("deletion_requests", "SELECT to_jsonb(t) FROM deletion_request t WHERE t.owner_id = :u"),
            (
                "audit_log",
                "SELECT jsonb_build_object('at', a.at, 'action', a.action, 'resource', a.resource) "
                "FROM audit_log a WHERE a.actor_id = :u ORDER BY a.id",
            ),
        ):
            out[name] = list(s.execute(text(sql), {"u": uid}).scalars())
        _audit(s, uid, "data_export", f"app_user:{uid}")
    return Response(
        json.dumps(out, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="metis-export.json"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/account/data")
def delete_account(
    body: DeleteIn, sess: Annotated[Session_, Depends(authed_session)], request: Request, response: Response
) -> dict:
    cfg, factory, uid = get_config(request), get_factory(request), sess.user.id
    rate_limit(factory, security.rl_key("user", "delete", str(uid)), *cfg.rl_delete_user)
    with request_session(factory, uid) as s:
        pw_hash = s.execute(
            text("SELECT password_hash FROM app_user WHERE id = :u"), {"u": uid}
        ).scalar_one_or_none()
    if pw_hash is None or not security.check_password(body.password, pw_hash):
        raise HTTPException(403, "password_incorrect")
    with request_session(factory, uid) as s:
        s.execute(text("INSERT INTO deletion_request (owner_id, scope) VALUES (:u, 'account')"), {"u": uid})
        _audit(s, uid, "account_delete", f"app_user:{uid}")
        s.execute(
            text("DELETE FROM app_user WHERE id = :u"), {"u": uid}
        )  # ON DELETE CASCADE removes all rows
    response.delete_cookie(COOKIE_SESSION, path="/", secure=cfg.cookie_secure, httponly=True, samesite="lax")
    response.delete_cookie(COOKIE_CSRF, path="/", secure=cfg.cookie_secure, samesite="lax")
    return {"status": "deleted", "backups_purge_within_days": 30}
