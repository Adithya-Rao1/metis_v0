"""Wave 4: launch readiness that can be judged offline (deploy/, CI, runbook) and the claims in docs/THREAT_MODEL.md, docs/DB_SCHEMA.md and
docs/OPERATIONS.md checked against code and the migrated database. xfail(strict) = a documented launch gap or a doc that is wrong today."""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
OPS = (ROOT / "docs/OPERATIONS.md").read_text()
TM = (ROOT / "docs/THREAT_MODEL.md").read_text()
DBS = (ROOT / "docs/DB_SCHEMA.md").read_text()
COMPOSE = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
DOCKERFILE = (ROOT / "deploy/Dockerfile").read_text()
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text()
CI = yaml.safe_load(CI_TEXT)


# ================================================================================================= image / compose
def test_the_image_runs_unprivileged_with_no_secrets_and_one_uvicorn_worker():
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert "--workers" not in DOCKERFILE and "WEB_CONCURRENCY" not in DOCKERFILE + str(COMPOSE)       # in-process job queue and Lead Studio state need one process
    assert "--no-access-log" in DOCKERFILE and "--log-config" in DOCKERFILE                         # the access log prints query strings
    assert not re.search(r"^(ENV|ARG)\s+\w*(SECRET|PASSWORD|TOKEN|KEY)\w*\s*=", DOCKERFILE, re.M | re.I)
    assert "COPY --chown=root:root leadstudio/" in DOCKERFILE and "docs/legal" in DOCKERFILE
    assert re.search(r"^FROM python:3\.12\.\d+-slim-bookworm AS build$", DOCKERFILE, re.M)


def test_the_app_service_is_hardened_and_bound_to_loopback():
    app = COMPOSE["services"]["app"]
    assert app["read_only"] is True and "ALL" in app["cap_drop"] and "no-new-privileges:true" in app["security_opt"]
    assert all(p.startswith("127.0.0.1:") for p in app["ports"])
    env = app["environment"]
    for forbidden in ("POSTGRES_PASSWORD", "METIS_MIGRATION_URL", "METIS_APP_DB_PASSWORD", "REDIS_PASSWORD"):
        assert forbidden not in env
    assert "METIS_DATABASE_URL" in env and "metis_app_login" in env["METIS_DATABASE_URL"]
    for svc in ("postgres", "redis", "minio"):
        assert not COMPOSE["services"][svc].get("ports")                                       # backing services are not published


@pytest.mark.xfail(strict=True, reason="F-151 MEDIUM: the compose 'app' environment is an allow-list that omits every documented tunable except METIS_ACCOUNTS_TRUST_PROXY: METIS_TERMS_VERSION, "
                                        "METIS_LOG_LEVEL, METIS_ACCOUNTS_COOKIE_SECURE/SESSION_TTL/RL_*, METIS_DB_*, METIS_LEAD_DOCK_URL and every STUDIO_* setting cannot reach the container, "
                                        "so the runbook (OPERATIONS sections 1 and 7) cannot be followed with the shipped compose file")
def test_every_documented_setting_can_reach_the_app_container():
    names = sorted(set(re.findall(r"`((?:METIS|STUDIO)_[A-Z0-9_]+)`", OPS)) - {"METIS_MIGRATION_URL", "METIS_APP_DB_PASSWORD"})
    env = COMPOSE["services"]["app"]["environment"]
    passthrough = [n for n in names if n in env]
    missing = [n for n in names if n not in env and not n.startswith("METIS_ACCOUNTS_RL_")]
    assert missing == [], (len(passthrough), missing)


@pytest.mark.xfail(strict=True, reason="F-154 LOW: no resource limits or liveness for the app container: no mem_limit/cpus/pids_limit (in-process Lead Studio run state, RDKit and torch can use all of the "
                                        "host), no restart policy, no HEALTHCHECK in the image or the service")
def test_the_app_container_has_resource_limits_a_restart_policy_and_a_healthcheck():
    app = COMPOSE["services"]["app"]
    limits = {"mem_limit", "cpus", "pids_limit", "deploy"} & set(app)
    assert limits and "restart" in app and ("healthcheck" in app or "HEALTHCHECK" in DOCKERFILE), (sorted(app))


@pytest.mark.xfail(strict=True, reason="F-159 LOW: in the compose stack the application connects to MinIO with the MinIO ROOT credentials (MINIO_ROOT_USER/PASSWORD are the same variables the app "
                                        "receives as access/secret key): a compromised app owns the whole object store, not one bucket")
def test_the_app_does_not_hold_the_object_store_root_credential():
    minio = COMPOSE["services"]["minio"]["environment"]
    app = COMPOSE["services"]["app"]["environment"]
    assert minio["MINIO_ROOT_USER"] != app["METIS_OBJECT_STORE_ACCESS_KEY"]


@pytest.mark.xfail(strict=True, reason="F-161 MEDIUM: nothing in deploy/ terminates TLS or routes web + API under one origin, although cookies are Secure/host-only, HSTS is a host feature (F-132), "
                                        "OPERATIONS requires a proxy that appends X-Forwarded-For, and SSE needs response buffering off. There is no proxy/Caddy/nginx definition to review or run")
def test_deploy_contains_a_tls_terminating_reverse_proxy_definition():
    text = (ROOT / "deploy/docker-compose.yml").read_text().lower()
    files = [p.name.lower() for p in (ROOT / "deploy").iterdir()]
    assert re.search(r"image:\s*(?:caddy|nginx|traefik|envoyproxy|haproxy)", text) or any(n in ("caddyfile", "nginx.conf", "traefik.yml") for n in files)


# ===================================================================================================== CI / release
def test_ci_actions_are_pinned_to_commit_shas_and_tokens_are_read_only():
    assert CI["permissions"] == {"contents": "read"}
    uses = re.findall(r"uses:\s*(\S+)", CI_TEXT)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses
    assert "secrets." not in CI_TEXT and "pull_request_target" not in CI_TEXT
    steps = " ".join(str(j) for j in CI["jobs"].values())
    assert "gitleaks" in steps and "trivy" in steps and "pip-audit" in steps and "bandit" in steps and "mypy" in steps


@pytest.mark.xfail(strict=True, reason="F-153 MEDIUM: no Docker image has ever been built by CI (or anywhere in the repo's automation): deploy/Dockerfile, the compose file, the migrate job, the non-root "
                                        "user, the read-only root and the log redaction config are untested as a unit; trivy scans the source tree, not the image; there is no SBOM although "
                                        "THREAT_MODEL lists 'container scan, SBOM' as the supply-chain mitigation")
def test_ci_builds_and_scans_the_image_and_produces_an_sbom():
    assert re.search(r"docker (?:compose )?build|build-push-action|buildx", CI_TEXT)
    assert re.search(r"image-ref|trivy image|grype|syft|sbom", CI_TEXT, re.I)


@pytest.mark.xfail(strict=True, reason="F-158 LOW: the secret scanner binary is downloaded with curl in the job and executed without a checksum or signature check (a supply-chain hop in the job "
                                        "that guards secrets); everything else in the workflow is pinned by commit SHA")
def test_ci_verifies_the_downloaded_scanner():
    block = CI_TEXT.split("gitleaks", 1)[1][:600]
    assert re.search(r"sha256sum|shasum|cosign|gpg --verify|--digest", block)


def test_ci_runs_this_security_suite_and_the_migrations_twice():
    run = CI_TEXT
    assert "python -m pytest -q --ignore=tests/web --ignore=tests/security/web" in run         # tests/security (incl. wave3, wave4) runs on pgserver
    assert run.count("python -m backend.core.migrate") >= 2
    assert re.search(r"ruff check backend ml leadstudio", run) and re.search(r"bandit -c pyproject.toml -r backend ml leadstudio", run)


def test_requirements_are_exactly_pinned():
    for rel in ("requirements.txt", "ml/requirements.txt", "requirements-dev.txt"):
        for ln in (ROOT / rel).read_text().splitlines():
            ln = ln.split("#")[0].strip()
            if ln and not ln.startswith("-r"):
                assert re.fullmatch(r"[A-Za-z0-9_.\[\]-]+==[\w.]+", ln), (rel, ln)


# =============================================================================================== operations gaps
@pytest.mark.xfail(strict=True, reason="F-155 HIGH (launch blocker): production has no real Mailer. The only implementations are ConsoleMailer (logs 'would send') and an in-memory test double, "
                                        "and get_mailer() silently falls back to ConsoleMailer when none is injected, in production too. Nobody can verify an address or reset a password, so "
                                        "nobody can sign in")
def test_production_can_deliver_verification_and_reset_mail():
    import inspect

    from backend.accounts import deps, mailer

    classes = {n for n, c in inspect.getmembers(mailer, inspect.isclass) if c.__module__ == mailer.__name__}
    real = classes - {"ConsoleMailer", "InMemoryMailer", "Mailer"}
    assert real, classes
    assert "ConsoleMailer()" not in inspect.getsource(deps.get_mailer) or "production" in inspect.getsource(deps.get_mailer)


@pytest.mark.xfail(strict=True, reason="F-156 MEDIUM: no backup, restore or deletion-drill tooling exists in the repository (no pg_dump/pg_restore script, no compose job, no restore test), although the "
                                        "30-day backup-purge promise (deletion_request) and THREAT_MODEL gate G4 ('restore and deletion drills') depend on them. OPERATIONS only says backups 'must be rotated'")
def test_backup_restore_and_deletion_drill_tooling_exists():
    hay = ""
    for d in ("scripts", "deploy", ".github"):
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and p.suffix in (".sh", ".py", ".yml", ".yaml", ".md", ""):
                hay += p.read_text(errors="ignore").lower()
    assert "pg_dump" in hay and ("pg_restore" in hay or "psql" in hay and "restore" in hay)
    assert "## backup" in OPS.lower() or "restore" in OPS.lower()


@pytest.mark.xfail(strict=True, reason="F-157 LOW: GET /health always answers ok and touches nothing (no database, no migration check, no queue), and there is no readiness or metrics route; a load "
                                        "balancer keeps routing to an instance whose database is gone. Structure-free monitoring (error and 429 counters, queue depth, run counts) does not exist")
def test_there_is_a_readiness_signal_that_checks_the_database():
    from backend.main import create_app

    app = create_app()
    paths = set(app.openapi()["paths"])
    health = next(r for r in app.routes if getattr(r, "path", "") == "/health")
    assert (paths & {"/ready", "/readyz", "/health/ready", "/healthz", "/metrics"}) or health.dependant.dependencies


def test_maintenance_is_documented_but_not_scheduled_by_compose():
    """Known gap (OPERATIONS section 4 says so): without it unverified accounts and revocation rows never shrink. Pinned so a change is noticed."""
    assert "maintenance" not in COMPOSE["services"] and "A compose service for it is **not** defined" in OPS


# ================================================================================ OPERATIONS.md vs code (Lead Studio)
def _doc_defaults():
    out = {}
    for ln in OPS.split("## 7.", 1)[1].splitlines():
        cells = [c.strip(" `") for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 2 and "STUDIO_" in cells[0]:
            names = [n.strip(" `") for n in cells[0].split(",")]
            vals = [v.strip(" `") for v in cells[1].split(",")]
            out.update(dict(zip(names, vals, strict=False)) if len(names) == len(vals) else {})
    return out


def test_documented_lead_studio_defaults_equal_the_code():
    from leadstudio.server.core import Settings

    d, s = _doc_defaults(), Settings()
    expected = {"STUDIO_MAX_GENERATIONS": s.max_generations, "STUDIO_MAX_POPULATION": s.max_population, "STUDIO_PER_USER_RUNS": s.per_user_active,
                "STUDIO_MAX_CONCURRENT": s.max_concurrent, "STUDIO_MAX_QUEUE": s.max_queue, "STUDIO_RUNS_PER_MIN": s.runs_per_minute,
                "STUDIO_VALIDATES_PER_MIN": s.validates_per_minute, "STUDIO_HEARTBEAT": s.heartbeat, "STUDIO_MOCK_DELAY": s.mock_delay, "STUDIO_REPLAY_DELAY": s.replay_delay}
    checked = 0
    for k, v in expected.items():
        if k in d:
            assert float(d[k]) == float(v), (k, d[k], v)
            checked += 1
    assert checked >= 8
    assert re.search(r"`STUDIO_RUN_TIMEOUT` \| 90 ", OPS) and s.run_timeout == 90
    assert "http://127.0.0.1:8100" in OPS
    from leadstudio.server import poses

    assert poses.DEFAULT_URL == "http://127.0.0.1:8100"


@pytest.mark.xfail(strict=True, reason="F-150 LOW: OPERATIONS section 7 omits settings the code reads: STUDIO_ALL_EDITS (=1 turns OFF the demo-safe edit filter for every user), STUDIO_EMBED "
                                        "(chemberta downloads model weights from Hugging Face at run time), STUDIO_CACHE_DIR (adapter files and caches; the default .cache/studio is read-only "
                                        "under the compose root filesystem), STUDIO_BINDING_MAX_GENERATIONS/POPULATION (listed only in the row title), DYNAMICBIND_POLL and DYNAMICBIND_TIMEOUT")
def test_every_environment_variable_the_lead_studio_code_reads_is_documented():
    used = set()
    for p in (ROOT / "leadstudio").rglob("*.py"):
        used |= set(re.findall(r"""["']((?:STUDIO|DYNAMICBIND)_[A-Z_]+)["']""", p.read_text()))
    used |= {"METIS_LEAD_DOCK_URL"}
    section = OPS.split("## 7.", 1)[1]
    undocumented = sorted(n for n in used if f"`{n}`" not in section and not re.search(rf"`[^`]*{n}[^`]*`", section))
    assert undocumented == [], undocumented


def test_lead_studio_settings_object_reads_exactly_the_documented_names():
    src = (ROOT / "leadstudio/server/core.py").read_text()
    for name in re.findall(r'"(STUDIO_[A-Z_]+)"', src):
        assert name in OPS, name
    assert "lead_dock_url" in (ROOT / "backend/core/settings.py").read_text()


# ==================================================================================== THREAT_MODEL.md vs code
@pytest.mark.xfail(strict=True, reason="F-152 LOW: THREAT_MODEL.md does not mention Lead Studio at all (SSRF through the docking URL, SMARTS/SMILES cost, long-lived SSE streams, in-process run "
                                        "state, optional torch/transformers model loading, copied third-party code) and states that hostile uploads are 'parsed in sandboxed worker with "
                                        "time/memory limits' while the Lead Studio routes parse SMILES and MOL files inside the request thread (only dockprep uses the sandbox)")
def test_the_threat_model_covers_lead_studio_and_its_sandbox_claim_is_true_for_it():
    assert "lead studio" in TM.lower() and ("ssrf" in TM.lower() or "docking" in TM.lower())
    lead_src = "".join(p.read_text() for p in (ROOT / "leadstudio/server").glob("*.py"))
    assert "sandbox" in lead_src.lower() or "multiprocessing" in lead_src or "ProcessPool" in lead_src


def test_threat_model_claims_that_hold_in_code():
    # parameterised SQL only: no f-string / % / + built SQL anywhere in backend or leadstudio
    import ast

    bad = []
    for base in ("backend", "leadstudio"):
        for p in (ROOT / base).rglob("*.py"):
            tree = ast.parse(p.read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("execute", "text", "exec_driver_sql") and n.args:
                    a = n.args[0]
                    if isinstance(a, ast.JoinedStr) and any(isinstance(v, ast.FormattedValue) for v in a.values):
                        bad.append((str(p.relative_to(ROOT)), n.lineno, "f-string"))
                    elif isinstance(a, ast.BinOp) and isinstance(a.op, (ast.Add, ast.Mod)) and not isinstance(a.left, ast.Constant):
                        bad.append((str(p.relative_to(ROOT)), n.lineno, "concat"))
                    elif isinstance(a, ast.Call) and getattr(a.func, "attr", "") == "format":
                        bad.append((str(p.relative_to(ROOT)), n.lineno, "format"))
    assert [b for b in bad if not b[0].startswith("backend/db/")] == [], bad
    log_cfg = (ROOT / "deploy/uvicorn_log_config.json").read_text()
    assert "RedactionFilter" in log_cfg                                                         # 'no personal data in logs' has a mechanism behind it
    from backend.core.settings import Settings

    assert Settings.model_fields["db_statement_timeout_ms"].default == 30000                    # 'statement timeouts'


@pytest.mark.xfail(strict=True, reason="F-160 LOW: THREAT_MODEL lists 'container scan, SBOM' as the supply-chain mitigation; neither exists (see F-153), and 'secret scanning + push protection' is a "
                                        "repository setting that cannot be verified from the tree (gitleaks in CI is real)")
def test_supply_chain_mitigations_named_in_the_threat_model_exist():
    assert "container scan" in TM and "SBOM" in TM
    assert re.search(r"trivy image|image-ref|syft|grype|sbom|cyclonedx", CI_TEXT, re.I)


# ===================================================================================== DB_SCHEMA.md vs the database
def test_db_schema_claims_about_roles_and_ownership_hold(su):
    q = lambda sql, *a: su.execute(sql, a).fetchall()  # noqa: E731
    app = q("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles WHERE rolname='metis_app'")[0]
    assert app == (False, False, False, False, False)
    ro = q("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='metis_readonly'")[0]
    assert ro == (False, False, False)
    readable = {r[0] for r in q("SELECT table_name FROM information_schema.role_table_grants WHERE grantee='metis_readonly' AND privilege_type='SELECT' AND table_schema='public'")}
    readable |= {r[0] for r in q("SELECT table_name FROM information_schema.role_column_grants WHERE grantee='metis_readonly' AND privilege_type='SELECT' AND table_schema='public'")}
    assert readable == {"model_version", "deletion_request"}, readable                            # 'sees only model_version, deletion_request'
    assert not q("SELECT 1 FROM pg_namespace WHERE nspname='public' AND has_schema_privilege('metis_app','public','CREATE')")     # 'no DDL'
    owned = q("SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE c.relnamespace='public'::regnamespace AND r.rolname='metis_app'")
    assert owned == []


def test_every_table_with_an_owner_column_has_forced_rls_a_not_null_fk_to_app_user_and_a_cascade(su):
    rows = su.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, a.attnotnull, "
        "(SELECT count(*) FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='f' AND k.confrelid='app_user'::regclass AND k.confdeltype='c' AND a.attnum = ANY(k.conkey)) "
        "FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='owner_id' AND NOT a.attisdropped "
        "WHERE c.relkind='r' AND c.relnamespace='public'::regnamespace ORDER BY 1").fetchall()
    names = {r[0] for r in rows}
    assert {"project", "compound", "prediction", "measurement", "note", "job", "lead_saved_run", "lead_manual_session", "lead_manual_step", "lead_profile"} <= names
    for name, rls, force, notnull, fks in rows:
        assert rls and force and notnull, name
        assert fks >= 1 or name == "deletion_request", name          # documented exception: 'no FK (survives user)'


def test_primary_keys_are_uuids_except_the_documented_exceptions(su):
    rows = su.execute(
        "SELECT c.relname, format_type(a.atttypid, a.atttypmod) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
        "JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum = k.conkey[1] WHERE k.contype='p' AND c.relnamespace='public'::regnamespace "
        "AND array_length(k.conkey,1)=1").fetchall()
    non_uuid = {n: t for n, t in rows if t != "uuid"}
    assert "audit_log" in non_uuid and non_uuid["audit_log"] == "bigint"                          # documented
    assert set(non_uuid) <= {"audit_log", "rate_limit_bucket", "session_revocation"}, non_uuid     # anything else contradicts 'UUID primary keys everywhere'


def test_db_schema_0005_section_matches_the_code_and_migration():
    from leadstudio.server import store

    assert (store.MAX_RUNS, store.MAX_MANUAL, store.MAX_PROFILES) == (200, 100, 20) and "200 runs, 100 manual sessions, 20 profiles" in DBS
    assert (ROOT / "tests/db/test_migration_0005.py").is_file() and (ROOT / "tests/security/test_lead_rls.py").is_file()
    sql = (ROOT / "backend/db/migrations/0005_lead_studio.sql").read_text()
    assert "1900000" in sql and "16384" in sql and "seq < 500" in sql
