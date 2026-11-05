"""Run Metis v1 locally for a demo: `make demo` (or `python scripts/demo.py [--port 8080]`).

Starts, on ONE address: an embedded Postgres (pgserver, data under .demo/pg) with every migration applied, the real
API (accounts, projects, predictions, ranking, exports, docking prep) and the static web/ site with the real
Content-Security-Policy from web/_headers.

HONESTY: the models are SYNTHETIC (tests/e2e/synth_registry.py: random-forest members on generated molecules with
generated labels; endpoints `synreg` and `syncls`). Predictions here prove the interface works; they are NOT real
ADMET predictions and must be described as sample data. Nothing is trained on real data and nothing leaves this machine.

Local only: binds 127.0.0.1, cookies are not Secure (plain http), e-mails are printed to this terminal.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))  # reuse the one-origin site + synthetic registry builder

DEMO_EMAIL = "demo@example.org"
DEMO_PASSWORD = "metis-demo-2026"  # local demo account only; created on every start if missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--dir", default=".demo", help="state directory (database, models); default .demo")
    ap.add_argument("--reset", action="store_true", help="delete .demo/ (database and synthetic models) first")
    args = ap.parse_args()

    import shutil

    work = (ROOT / args.dir).resolve()
    if args.reset and work.exists():
        shutil.rmtree(work)
    work.mkdir(exist_ok=True)

    # Session/CSRF signing key: random, created once, kept owner-only under the git-ignored .demo/ (local demo only).
    import os
    import secrets

    key_file = work / "secret_key"  # secrets.token_urlsafe(64) = 86 chars, above the 64-byte production minimum
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(64))
        key_file.chmod(0o600)
    os.environ.setdefault("METIS_SECRET_KEY", key_file.read_text().strip())

    import pgserver
    import psycopg
    import uvicorn
    from onesite import Site
    from sqlalchemy.engine import make_url
    from synth_registry import build_registry

    from backend.accounts import security
    from backend.accounts.config import AccountsConfig
    from backend.accounts.mailer import ConsoleMailer
    from backend.core.db import make_engine, make_session_factory
    from backend.core.migrate import apply_migrations
    from backend.core.settings import get_settings
    from backend.jobs import deps
    from backend.jobs.queue import InProcessJobQueue
    from backend.main import create_app
    from ml.registry import sync_db
    from ml.registry.registry import FileModelRegistry

    print("Starting Postgres ...", flush=True)
    srv = pgserver.get_server(work / "pg", cleanup_mode="stop")
    uri = srv.get_uri()
    apply_migrations(uri)

    registry_root = work / "registry"
    if not (registry_root / "datasets.yaml").exists():
        print("Building the SYNTHETIC demo models (a few seconds) ...", flush=True)
        registry_root.mkdir(parents=True, exist_ok=True)
        build_registry(registry_root)
    sync_db.sync(uri, registry_root, manifest_path=registry_root / "datasets.yaml")

    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS demo_app_login")
        c.execute("CREATE ROLE demo_app_login LOGIN PASSWORD 'local-demo-only' IN ROLE metis_app")
        exists = c.execute("SELECT 1 FROM app_user WHERE email_lower = %s", (DEMO_EMAIL,)).fetchone()
        if not exists:
            uid = c.execute(
                "SELECT auth_register(%s, %s, %s)",
                (DEMO_EMAIL, security.hash_password(DEMO_PASSWORD, 12), AccountsConfig().terms_version),
            ).fetchone()[0]
            c.execute("UPDATE app_user SET email_verified_at = now() WHERE id = %s", (uid,))

    u = make_url(uri.replace("postgresql://", "postgresql+psycopg://", 1))
    url = u.set(username="demo_app_login", password="local-demo-only").render_as_string(hide_password=False)
    get_settings.cache_clear()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    app = create_app()
    app.state.session_factory = factory
    app.state.accounts_config = AccountsConfig(cookie_secure=False)  # plain http on localhost
    app.state.mailer = ConsoleMailer()  # verification / reset links are printed here
    queue = InProcessJobQueue(max_workers=2, factory=factory)
    registry = FileModelRegistry(registry_root)
    app.dependency_overrides[deps.get_factory] = lambda: factory
    app.dependency_overrides[deps.get_registry] = lambda: registry
    app.dependency_overrides[deps.get_queue] = lambda: queue

    print(
        f"\nMetis v1 demo:   http://127.0.0.1:{args.port}\n"
        f"Demo account:    {DEMO_EMAIL}  /  {DEMO_PASSWORD}   (or sign up; the link is printed below)\n"
        "Models:          SYNTHETIC sample models (endpoints synreg, syncls): describe numbers as sample data.\n"
        "Docking prep:    upload web/fixtures/structures/1m17_chainA_pocket.pdb  (PDB-ID fetch is off)\n"
        "Stop:            Ctrl+C\n",
        flush=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(Site(app, ROOT / "web"), host="127.0.0.1", port=args.port, log_level="warning", lifespan="off")
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping ...", flush=True)
        queue.shutdown(wait=False)
        engine.dispose()
        srv.cleanup()
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
