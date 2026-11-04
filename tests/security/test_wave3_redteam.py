"""Wave 3 red-team suite (Agent 10): migration 0004 and the janitor role, the maintenance runner, and docs/OPERATIONS.md
claims checked against the code. Accounts/jobs/web checks live in tests/security/wave3/.

Convention (as before): xfail(strict=True, reason="F-xxx SEVERITY: ...") reproduces a REAL defect and fails hard (XPASS-strict)
the day it is fixed, so the marker must then be removed. Unmarked tests are protections that hold today.
Write-up and fixes: private/qa_wave3.md.
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import select
import signal
import subprocess
import sys
import time

import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs/OPERATIONS.md").read_text()
HASH = "$2b$04$" + "a" * 53


def defect(fid, sev, msg):
    return pytest.mark.xfail(strict=True, reason=f"{fid} {sev}: {msg}")


def mkuser(su, email, *, verified=False, age="0 seconds", locked=None, failed=0):
    uid = su.execute(
        "insert into app_user (email_lower, password_hash, email_verified_at, created_at, failed_logins, locked_until) "
        "values (%s, %s, case when %s then now() else null end, now() - %s::interval, %s, %s) returning id",
        (email, HASH, verified, age, failed, locked),
    ).fetchone()[0]
    return uid


# ================================================================================ A. janitor role and grants
class TestJanitorRole:
    def test_role_attributes(self, su):
        r = su.execute("select rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls from pg_roles where rolname = 'metis_janitor'").fetchone()
        assert r == (False, False, False, False, False, True)

    def test_nobody_is_a_member_and_it_is_a_member_of_nothing(self, su):
        assert su.execute("select count(*) from pg_auth_members m join pg_roles r on r.oid = m.roleid where r.rolname = 'metis_janitor'").fetchone()[0] == 0
        assert su.execute("select count(*) from pg_auth_members m join pg_roles r on r.oid = m.member where r.rolname = 'metis_janitor'").fetchone()[0] == 0

    def test_privileges_are_exactly_the_documented_ones(self, su):
        tabs = {(t, p) for t, p in su.execute("select table_name, privilege_type from information_schema.role_table_grants where grantee = 'metis_janitor'").fetchall()}
        assert tabs == {("rate_limit_bucket", "DELETE"), ("session_revocation", "DELETE"), ("app_user", "DELETE"), ("audit_log", "INSERT")}
        cols = {(t, c) for t, c, p in su.execute("select table_name, column_name, privilege_type from information_schema.role_column_grants where grantee = 'metis_janitor' and privilege_type = 'SELECT'").fetchall()}
        assert cols == {("rate_limit_bucket", "window_start"), ("session_revocation", "expires_at"), ("app_user", "id"), ("app_user", "email_verified_at"), ("app_user", "deleted_at"), ("app_user", "created_at")}
        for col in ("email_lower", "password_hash", "locked_until", "failed_logins", "tier"):
            assert not su.execute("select has_column_privilege('metis_janitor', 'app_user', %s, 'SELECT')", (col,)).fetchone()[0], col
        for t in ("project", "compound", "prediction", "measurement", "note", "consent", "auth_token", "job", "terms_acceptance", "model_version"):
            assert not su.execute("select has_table_privilege('metis_janitor', %s, 'SELECT')", (t,)).fetchone()[0], t
        assert not su.execute("select has_table_privilege('metis_janitor', 'audit_log', 'DELETE') or has_table_privilege('metis_janitor', 'audit_log', 'UPDATE')").fetchone()[0]
        assert not su.execute("select has_schema_privilege('metis_janitor', 'public', 'CREATE')").fetchone()[0]

    def test_app_role_cannot_become_the_janitor_or_delete_directly(self, login_role, su):
        c = login_role()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SET ROLE metis_janitor")
        c = login_role()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM rate_limit_bucket")
        c = login_role()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM session_revocation")
        c = login_role()
        assert c.execute("DELETE FROM app_user").rowcount == 0           # RLS: no identity, no rows (and no BYPASSRLS)
        assert not su.execute("select has_schema_privilege('metis_app', 'public', 'CREATE')").fetchone()[0]       # search_path cannot be planted

    @pytest.mark.parametrize("fn", ["rate_limit_purge()", "session_purge_expired()", "auth_purge_unverified(interval)", "auth_reset_password(text, text)"])
    def test_functions_are_definer_pinned_and_executable_only_by_the_app_role(self, su, fn):
        row = su.execute("select prosecdef, proconfig, pg_get_userbyid(proowner), proacl::text from pg_proc where oid = %s::regprocedure", (fn,)).fetchone()
        definer, config, owner, acl = row
        assert definer is True
        assert any(c.startswith("search_path=") and "pg_temp" in c and c.split("=", 1)[1].split(",")[0].strip() == "pg_catalog" for c in config), config
        assert owner == ("metis_auth" if fn.startswith("auth_reset") else "metis_janitor")
        assert "metis_app=X" in acl and not re.search(r"(^|[{,])=X", acl), acl                          # no PUBLIC execute
        for role in ("metis_readonly",):
            assert not su.execute("select has_function_privilege(%s, %s::regprocedure, 'EXECUTE')", (role, fn)).fetchone()[0]

    def test_a_non_app_role_cannot_call_the_purges(self, su):
        su.execute("drop role if exists w3_outsider")
        su.execute("create role w3_outsider")
        try:
            su.execute("SET ROLE w3_outsider")
            for f in ("rate_limit_purge()", "session_purge_expired()", "auth_purge_unverified('2 days')"):
                with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedFunction)):          # no schema USAGE either
                    su.execute(f"SELECT {f}")
        finally:
            su.execute("RESET ROLE")
            su.execute("drop role w3_outsider")


# ================================================================================ B. purge floors and audit
class TestPurges:
    def test_rate_limit_floor_is_two_hours_and_not_caller_controlled(self, su, login_role):
        for age, key in (("30 minutes", "1"), ("119 minutes", "2"), ("121 minutes", "3"), ("3 days", "4")):
            su.execute("insert into rate_limit_bucket (key_hash, window_start) values (%s, now() - %s::interval)", (key * 64, age))
        n = login_role().execute("SELECT rate_limit_purge()").fetchone()[0]
        left = {r[0][0] for r in su.execute("select key_hash from rate_limit_bucket").fetchall()}
        assert n == 2 and left == {"1", "2"}
        with pytest.raises(psycopg.errors.UndefinedFunction):
            login_role().execute("SELECT rate_limit_purge('0 seconds')")                              # no parameter to widen it

    def test_session_purge_removes_only_expired_revocations(self, su, login_role):
        uid = mkuser(su, "a@example.com", verified=True)
        su.execute("insert into session_revocation (jti, owner_id, expires_at) values (gen_random_uuid(), %s, now() - interval '1 second'), (gen_random_uuid(), %s, now() + interval '1 hour')", (uid, uid))
        assert login_role().execute("SELECT session_purge_expired()").fetchone()[0] == 1
        assert su.execute("select count(*) from session_revocation").fetchone()[0] == 1

    @pytest.mark.parametrize("arg", ["'0 seconds'", "'-5 days'", "NULL", "'1 microsecond'", "'23 hours'"])
    def test_unverified_purge_has_a_one_day_floor_and_spares_verified_accounts(self, su, login_role, arg):
        young = mkuser(su, "young@example.com", age="2 hours")
        mid = mkuser(su, "mid@example.com", age="30 hours")
        old = mkuser(su, "old@example.com", age="10 days")
        ver = mkuser(su, "verified@example.com", verified=True, age="400 days")
        su.execute("insert into terms_acceptance (owner_id, document, version) values (%s, 'terms', 'v')", (old,))
        n = login_role().execute(f"SELECT auth_purge_unverified({arg})").fetchone()[0]
        left = {r[0] for r in su.execute("select id from app_user").fetchall()}
        want_gone = {old, mid} if arg != "NULL" else {old}                                 # NULL falls back to the 7 day default
        assert n == len(want_gone) and left == {young, mid, old, ver} - want_gone
        assert su.execute("select count(*) from terms_acceptance where owner_id = %s", (old,)).fetchone()[0] == 0     # cascade

    def test_unverified_purge_is_audited_without_email_and_default_is_seven_days(self, su, login_role):
        gone = mkuser(su, "secret-person@example.com", age="9 days")
        keep = mkuser(su, "recent@example.com", age="3 days")
        assert login_role().execute("SELECT auth_purge_unverified()").fetchone()[0] == 1
        rows = su.execute("select actor_id, action, resource, detail::text from audit_log where action = 'purge_unverified'").fetchall()
        assert rows == [(None, "purge_unverified", f"app_user:{gone}", rows[0][3])]
        assert "secret-person" not in str(rows) and su.execute("select count(*) from app_user where id = %s", (keep,)).fetchone()[0] == 1

    def test_audit_log_stays_append_only_for_the_janitor_path(self, su, login_role):
        mkuser(su, "old@example.com", age="9 days")
        login_role().execute("SELECT auth_purge_unverified()")
        with pytest.raises(psycopg.Error):
            su.execute("update audit_log set action = 'x'")
        with pytest.raises(psycopg.Error):
            su.execute("delete from audit_log")


# ============================================================================== C. unlocking is not available
class TestLockAndReset:
    def test_a_caller_cannot_clear_a_lock_with_a_success_or_a_direct_update(self, su, login_role):
        uid = mkuser(su, "a@example.com", verified=True, failed=5)
        su.execute("update app_user set locked_until = now() + interval '10 minutes' where id = %s", (uid,))
        before = su.execute("select failed_logins, locked_until from app_user where id = %s", (uid,)).fetchone()
        c = login_role(uid)
        c.execute("SELECT auth_record_login(%s, true)", (uid,))                           # a 'success' while locked is ignored
        c.execute("SELECT auth_record_login(%s, false)", (uid,))                          # and a failure neither extends it nor counts
        assert su.execute("select failed_logins, locked_until from app_user where id = %s", (uid,)).fetchone() == before
        assert su.execute("select count(*) from audit_log where actor_id = %s", (uid,)).fetchone()[0] == 0
        for sql in ("UPDATE app_user SET locked_until = NULL WHERE id = %s", "UPDATE app_user SET failed_logins = 0 WHERE id = %s", "UPDATE app_user SET email_verified_at = now() WHERE id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                login_role(uid).execute(sql, (uid,))
        assert su.execute("select locked_until is not null from app_user where id = %s", (uid,)).fetchone()[0]

    def test_reset_without_a_valid_token_does_nothing_and_says_nothing(self, su, login_role):
        uid = mkuser(su, "a@example.com", verified=True, failed=5)
        su.execute("update app_user set locked_until = now() + interval '10 minutes' where id = %s", (uid,))
        c = login_role()
        for tok in ("0" * 64, "f" * 64, "", "x"):
            assert c.execute("SELECT auth_reset_password(%s, %s)", (tok, HASH.replace("a", "b"))).fetchone()[0] is None
        assert su.execute("select password_hash, locked_until is not null from app_user where id = %s", (uid,)).fetchone() == (HASH, True)

    def test_reset_refuses_unverified_and_deleted_accounts_without_a_hint(self, su, login_role):
        un = mkuser(su, "un@example.com")
        dl = mkuser(su, "dl@example.com", verified=True)
        su.execute("update app_user set deleted_at = now() where id = %s", (dl,))
        for uid, tok in ((un, "1" * 64), (dl, "2" * 64)):
            su.execute("select auth_issue_token(%s, 'reset_password', %s, interval '1 hour')", (uid, tok))
            assert login_role().execute("SELECT auth_reset_password(%s, %s)", (tok, HASH.replace("a", "c"))).fetchone()[0] is None
        assert su.execute("select count(*) from app_user where password_hash = %s", (HASH,)).fetchone()[0] == 2

    def test_a_lapsed_lock_restarts_the_count_F60_stays_fixed(self, su, login_role):
        uid = mkuser(su, "a@example.com", verified=True, failed=5)
        su.execute("update app_user set locked_until = now() - interval '1 second' where id = %s", (uid,))
        login_role().execute("SELECT auth_record_login(%s, false)", (uid,))
        assert su.execute("select failed_logins, locked_until from app_user where id = %s", (uid,)).fetchone() == (1, None)
        for _ in range(4):
            login_role().execute("SELECT auth_record_login(%s, false)", (uid,))
        failed, until = su.execute("select failed_logins, locked_until from app_user where id = %s", (uid,)).fetchone()
        assert failed == 5 and until is not None


# ===================================================================================== D. maintenance runner
@pytest.fixture
def runner_env(pg, su, monkeypatch):
    """(factory, url): the runner connects exactly as production would, as a LOGIN member of metis_app."""
    sys.path.insert(0, str(ROOT / "tests" / "security" / "wave2"))
    import w2stack as S
    from sqlalchemy.engine import make_url

    factory, engine = S.login_factory(pg, role="w3_maint_login")
    u = make_url(pg.replace("postgresql://", "postgresql+psycopg://", 1)).set(username="w3_maint_login", password="test-only")
    url = u.render_as_string(hide_password=False)
    yield factory, url
    engine.dispose()


class TestMaintenanceRunner:
    def test_counts_only_in_logs_and_the_work_is_done(self, runner_env, su, caplog):
        from backend.maintenance import run_once

        factory, _ = runner_env
        uid = mkuser(su, "zz-distinct-7f3a@example.com", age="9 days")
        su.execute("insert into rate_limit_bucket (key_hash, window_start) values (%s, now() - interval '5 hours')", ("e" * 64,))
        with caplog.at_level(logging.DEBUG):
            counts = run_once(factory)
        assert counts == {"rate_limit_buckets": 1, "session_revocations": 0, "unverified_accounts": 1}
        text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
        assert "maintenance done: rate_limit_buckets=1 session_revocations=0 unverified_accounts=1" in text
        for secret in ("zz-distinct", "example.com", str(uid), HASH, "e" * 20):
            assert secret not in text
        assert su.execute("select count(*) from app_user").fetchone()[0] == 0

    def test_one_failing_step_does_not_hide_the_others_and_exit_code_is_one(self, runner_env, su, caplog, monkeypatch):
        from backend.maintenance import runner

        factory, url = runner_env
        mkuser(su, "old@example.com", age="9 days")
        monkeypatch.setattr(runner, "_STEPS", (("broken", "SELECT no_such_function_zz('secret-arg')"), *runner._STEPS))
        with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as ei:
            runner.run_once(factory)
        assert str(ei.value) == "maintenance steps failed: broken"
        text = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
        assert "unverified_accounts=1" in text and "secret-arg" not in text and "no_such_function" not in text
        assert su.execute("select count(*) from app_user").fetchone()[0] == 0                       # the later steps still ran
        from backend.core.settings import get_settings

        monkeypatch.setenv("METIS_DATABASE_URL", url)
        monkeypatch.setenv("METIS_ENV", "test")
        get_settings.cache_clear()
        try:
            assert runner.main([]) == 1
        finally:
            get_settings.cache_clear()

    def test_exit_codes_cannot_start_vs_failed_vs_usage(self, monkeypatch, caplog):
        from backend.core.settings import get_settings
        from backend.maintenance import runner

        monkeypatch.delenv("METIS_DATABASE_URL", raising=False)
        monkeypatch.setenv("METIS_ENV", "test")
        get_settings.cache_clear()
        try:
            with caplog.at_level(logging.DEBUG):
                assert runner.main([]) == 2
            assert "cannot start: RuntimeError" in caplog.text
            for bad in (["--loop", "5"], ["--loop", "-1"], ["--loop", "abc"], ["--nonsense"]):
                with pytest.raises(SystemExit) as ei:
                    runner.main(bad)
                assert ei.value.code == 2, bad
        finally:
            get_settings.cache_clear()

    def test_bad_credentials_exit_one_and_never_echo_the_password_or_url(self, pg, monkeypatch, caplog, capfd):
        from sqlalchemy.engine import make_url

        from backend.core.settings import get_settings
        from backend.maintenance import runner

        url = make_url(pg.replace("postgresql://", "postgresql+psycopg://", 1)).set(username="no_such_role_zz", password="hunter2-very-secret").render_as_string(hide_password=False)
        monkeypatch.setenv("METIS_DATABASE_URL", url)
        monkeypatch.setenv("METIS_ENV", "test")
        get_settings.cache_clear()
        try:
            with caplog.at_level(logging.DEBUG):
                assert runner.main([]) == 1
        finally:
            get_settings.cache_clear()
        out = caplog.text + "".join(capfd.readouterr())
        assert "hunter2" not in out and "no_such_role_zz" not in out and "postgresql" not in out.lower().replace("psycopg", "")

    def test_sigterm_stops_the_loop_cleanly_with_exit_zero(self, runner_env):
        _, url = runner_env
        env = {**os.environ, "METIS_DATABASE_URL": url, "METIS_ENV": "test", "PYTHONPATH": str(ROOT)}
        p = subprocess.Popen([sys.executable, "-m", "backend.maintenance", "--loop", "60"], cwd=ROOT, env=env, stderr=subprocess.PIPE, text=True)
        try:
            deadline, seen = time.time() + 40, ""
            while time.time() < deadline and "maintenance done" not in seen:
                if select.select([p.stderr], [], [], 1.0)[0]:
                    seen += p.stderr.readline()
            assert "maintenance done: rate_limit_buckets=" in seen, seen
            p.send_signal(signal.SIGTERM)
            assert p.wait(timeout=15) == 0
        finally:
            if p.poll() is None:
                p.kill()

    @defect("F-128", "LOW", "docs/OPERATIONS.md says the runner needs only METIS_DATABASE_URL and 'METIS_SECRET_KEY is not needed', but make_engine() calls get_settings(), whose production validator requires the secret key, Redis URL and the three object-store variables; the runner exits 2 ('cannot start') in a container given only the documented variables (the same mismatch for any new worker process)")
    def test_runner_starts_in_production_with_only_the_documented_variables(self, runner_env, monkeypatch):
        from backend.core.settings import get_settings
        from backend.maintenance import runner

        _, url = runner_env
        for k in ("METIS_SECRET_KEY", "METIS_REDIS_URL", "METIS_OBJECT_STORE_ENDPOINT", "METIS_OBJECT_STORE_ACCESS_KEY", "METIS_OBJECT_STORE_SECRET_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("METIS_ENV", "production")
        monkeypatch.setenv("METIS_DATABASE_URL", url)
        get_settings.cache_clear()
        try:
            assert runner.main([]) in (0, 1)
        finally:
            get_settings.cache_clear()


# ===================================================================================== E. OPERATIONS.md claims
def _code_text():
    return "\n".join(p.read_text() for p in sorted((ROOT / "backend").rglob("*.py")))


class TestOperationsDocMatchesCode:
    def test_every_variable_named_in_the_runbook_exists_in_code_or_deploy(self):
        code = _code_text() + (ROOT / "deploy/docker-compose.yml").read_text()
        from backend.accounts.config import AccountsConfig
        from backend.core.settings import Settings

        known = {"METIS_" + f.upper() for f in Settings.model_fields} | {"METIS_ACCOUNTS_" + f.upper() for f in AccountsConfig.model_fields} | {"METIS_TERMS_VERSION", "METIS_MIGRATION_URL"}
        for name in {n for n in re.findall(r"`(METIS_[A-Z0-9_]+)", DOC) if not n.endswith("_")}:     # METIS_ACCOUNTS_RL_<ROUTE>_<SCOPE> is a pattern
            assert name in known or name in code, name

    def test_every_setting_and_accounts_tunable_is_documented(self):
        from backend.accounts.config import AccountsConfig
        from backend.core.settings import Settings

        for f in Settings.model_fields:
            assert f"METIS_{f.upper()}" in DOC, f
        for f in AccountsConfig.model_fields:
            name = f"METIS_ACCOUNTS_{f.upper()}"
            if f.startswith("rl_"):
                assert "METIS_ACCOUNTS_RL_<ROUTE>_<SCOPE>" in DOC
            elif f == "terms_version":
                assert "METIS_TERMS_VERSION" in DOC
            else:
                assert name in DOC or "METIS_ACCOUNTS_" + f.split("_")[0].upper() in DOC, name

    def test_documented_defaults_equal_the_code_defaults(self):
        from backend.accounts.config import AccountsConfig
        from backend.core.settings import Settings

        a, s = AccountsConfig(), Settings()
        assert (a.terms_version, a.session_ttl_s, a.bcrypt_cost, a.verify_ttl_hours, a.reset_ttl_s, a.cookie_secure, a.trust_proxy) == ("2026-01", 3600, 12, 24, 3600, True, False)
        assert (s.log_level, s.db_pool_size, s.db_statement_timeout_ms, s.object_store_bucket) == ("INFO", 5, 30000, "metis")
        for needle in ("`2026-01`", "`3600`", "`12`", "`24`", "`false`", "`true`", "`INFO`", "`5`", "`30000`", "`metis`"):
            assert needle in DOC, needle
        from backend.accounts.security import MAX_PASSWORD, MIN_PASSWORD

        assert (MIN_PASSWORD, MAX_PASSWORD) == (10, 128) and "10 to 128 characters" in DOC

    def test_the_web_forms_never_allow_a_shorter_password_than_the_server(self):
        from backend.accounts.security import MIN_PASSWORD

        for page in ("auth.html",):
            for m in re.finditer(r'type="password"[^>]*minlength="(\d+)"', (ROOT / "web" / page).read_text()):
                assert int(m.group(1)) >= MIN_PASSWORD

    def test_the_app_container_gets_none_of_the_owner_secrets_and_the_proxy_switch_is_passed(self):
        import yaml

        compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
        env = compose["services"]["app"]["environment"]
        assert not {"POSTGRES_PASSWORD", "METIS_MIGRATION_URL", "METIS_APP_DB_PASSWORD", "REDIS_PASSWORD"} & set(env)
        assert "METIS_ACCOUNTS_TRUST_PROXY" in env and "METIS_DATABASE_URL" in env
        assert "METIS_MIGRATION_URL" in compose["services"]["migrate"]["environment"]
        assert "maintenance" not in str(compose).lower() and re.search(r"\*\*not\*\* defined in `deploy/docker-compose.yml` yet", DOC)          # the doc says so while true

    def test_nothing_in_the_backend_uses_redis_as_the_doc_says(self):
        assert not re.search(r"^\s*(import|from)\s+redis\b|redis\.Redis|aioredis", _code_text(), re.M)

    def test_one_worker_only_in_the_shipped_image(self):
        cmd = (ROOT / "deploy/Dockerfile").read_text()
        assert "--workers" not in cmd and "WEB_CONCURRENCY" not in cmd           # F-126 is latent only while this holds

    def test_mail_is_console_only_unless_injected(self):
        from fastapi import Request

        from backend.accounts.deps import get_mailer
        from backend.accounts.mailer import ConsoleMailer
        from backend.main import create_app

        app = create_app()
        scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/", "query_string": b""}
        assert isinstance(get_mailer(Request(scope)), ConsoleMailer) and "Mail is not wired" in DOC

    def test_session_and_csrf_claims(self, pg, su):
        sys.path.insert(0, str(ROOT / "tests" / "security" / "wave2"))
        sys.path.insert(0, str(ROOT / "tests" / "api"))
        import jwt
        import w2stack as S
        from w2stack import Cl

        from backend.accounts.config import COOKIE_CSRF, COOKIE_SESSION, AccountsConfig, jwt_key

        factory, engine = S.login_factory(pg, role="w3_doc_login")
        try:
            app = S.build_app(factory, None, None, AccountsConfig(bcrypt_cost=4, terms_version=S.TERMS, cookie_secure=True, rl_register_ip=(10**6, 3600), rl_login_ip=(10**6, 300)))
            c = Cl(app)
            c.register("doc@example.com")
            c.verify("doc@example.com")
            r = c.login("doc@example.com")
            claims = jwt.decode(c.http.cookies.get(COOKIE_SESSION), jwt_key(), algorithms=["HS256"], issuer="metis")
            assert set(claims) == {"sub", "jti", "iat", "exp", "iss", "pwv"} and claims["exp"] - claims["iat"] == 3600
            hs = {h.split("=", 1)[0]: h.lower() for h in r.headers.get_list("set-cookie")}
            s, x = hs[COOKIE_SESSION], hs[COOKIE_CSRF]
            assert "httponly" in s and "secure" in s and "samesite=lax" in s and "max-age=3600" in s and "domain" not in s and "domain" not in x
            assert "httponly" not in x and "secure" in x
        finally:
            engine.dispose()

    def test_account_deletion_procedure_as_documented(self, pg, su):
        sys.path.insert(0, str(ROOT / "tests" / "security" / "wave2"))
        import w2stack as S
        from w2stack import Cl

        from backend.accounts.config import AccountsConfig

        factory, engine = S.login_factory(pg, role="w3_doc_login")
        try:
            app = S.build_app(factory, None, None, AccountsConfig(bcrypt_cost=4, terms_version=S.TERMS, cookie_secure=True, rl_register_ip=(10**6, 3600), rl_login_ip=(10**6, 300)))
            c = Cl(app)
            uid = c.signup("gone@example.com")
            pid = c.post("/projects", {"name": "secret-project-name"}).json()["id"]
            su.execute("insert into job (owner_id, project_id, kind, state) values (%s, %s, 'predict', 'running')", (str(uid), pid))
            assert c.delete("/account/data", {"password": S.PW}).json() == {"status": "deleted", "backups_purge_within_days": 30}
            for t in ("app_user", "project", "job", "consent", "terms_acceptance", "session_revocation", "auth_token"):
                assert su.execute(f"select count(*) from {t}").fetchone()[0] == 0, t
            due, purged, rows_purged = su.execute("select backups_purge_due - requested_at, backups_purged_at, rows_purged_at from deletion_request where owner_id = %s", (str(uid),)).fetchone()
            assert 29.99 < due.total_seconds() / 86400 < 30.01 and purged is None and rows_purged is None
            assert su.execute("select count(*) from audit_log where action = 'account_delete'").fetchone()[0] == 1
            dump = str(su.execute("select * from audit_log").fetchall()) + str(su.execute("select * from deletion_request").fetchall())
            assert "gone@example.com" not in dump and "secret-project-name" not in dump
            # the app role cannot touch the bookkeeping columns or remove requests
            with psycopg.connect(pg, autocommit=True) as a:
                a.execute("SET ROLE metis_app")
                a.execute("SELECT set_config('metis.user_id', %s, false)", (str(uid),))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    a.execute("UPDATE deletion_request SET backups_purged_at = now()")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    a.execute("DELETE FROM deletion_request")
        finally:
            engine.dispose()


@defect("F-129", "MEDIUM", "docs/OPERATIONS.md section 6 tells the operator to find due backup purges with `SELECT id, backups_purge_due FROM deletion_request WHERE backups_purged_at IS NULL AND backups_purge_due < now()` 'as the owner or metis_readonly', but deletion_request has FORCE row level security with the policy owner_id = current_user_id() and metis_readonly is NOBYPASSRLS with no identity: it sees ZERO rows, so an operator following the runbook with the read-only role concludes nothing is due and the 30-day backup purge (a manual step) is silently never done")
def test_the_runbook_review_query_returns_the_pending_requests_for_the_readonly_role(pg, su):
    su.execute("insert into deletion_request (owner_id, scope, backups_purge_due) values (gen_random_uuid(), 'account', now() - interval '1 day')")
    try:
        su.execute("SET ROLE metis_readonly")
        rows = su.execute("SELECT id, backups_purge_due FROM deletion_request WHERE backups_purged_at IS NULL AND backups_purge_due < now()").fetchall()
    finally:
        su.execute("RESET ROLE")
    assert len(rows) == 1


@defect("F-134", "LOW", "the rate-limit windows are free configuration (METIS_ACCOUNTS_RL_<ROUTE>_<SCOPE>=(limit, seconds), no upper bound) but rate_limit_purge() deletes every bucket older than 2 hours: a window longer than 2 h (for example a daily registration cap) is silently reset by the purge, so the configured limit is weaker than it says; the migration comment 'longer than every configured window (max 1 hour)' is not enforced by the config")
def test_rate_limit_windows_longer_than_the_purge_floor_are_rejected():
    from pydantic import ValidationError

    from backend.accounts.config import AccountsConfig

    with pytest.raises(ValidationError):
        AccountsConfig(rl_register_ip=(10, 86400))
