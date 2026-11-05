"""Deploy/CI consistency: the image must ship every runtime package, and CI and Makefile must agree."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text()


def _copied_sources() -> list[str]:
    out = []
    for ln in DOCKERFILE.splitlines():
        if ln.startswith("COPY"):
            parts = [p for p in ln.split() if not p.startswith("--")][1:-1]
            out += parts
    return out


def _runtime_packages(top: str) -> list[str]:
    return sorted(
        f"{top}/{p.name}"
        for p in (ROOT / top).iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and p.name != "__pycache__"
    )


def test_dockerfile_copies_backend_and_ml_wholesale():
    srcs = _copied_sources()
    assert "backend/" in srcs
    assert "leadstudio/" in srcs  # Lead Studio, mounted by backend.main
    assert "ml/" in srcs  # whole tree: covers every present and future subpackage
    # every runtime package directory is therefore inside a copied tree
    for top in ("backend", "ml", "leadstudio"):
        pkgs = _runtime_packages(top)
        assert pkgs, top
        assert all(f"{top}/" in srcs for _ in pkgs)


def test_expected_packages_exist_in_scope():
    names = {p.split("/")[1] for p in _runtime_packages("backend")}
    assert {
        "accounts",
        "projects",
        "compounds",
        "predictions",
        "ranking",
        "exports",
        "jobs",
        "dockprep",
    } <= names
    assert {p.split("/")[1] for p in _runtime_packages("ml")} >= {"registry", "train", "uncertainty", "data"}


def test_dockerignore_does_not_exclude_runtime_trees():
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    active = [ln.strip() for ln in ignore if ln.strip() and not ln.startswith("#")]
    for blocked in ("backend", "ml", "deploy", "docs/legal"):
        assert blocked not in active and blocked + "/" not in active
    assert "docs/*" not in active or "!docs/legal" in active


def test_image_ships_disclaimer_and_ml_requirements_without_secrets():
    assert "docs/legal" in DOCKERFILE
    assert "ml/requirements.txt" in DOCKERFILE
    assert re.search(r"^USER 10001:10001", DOCKERFILE, re.M)
    assert not re.search(r"^(ENV|ARG)[^\n]*(SECRET|PASSWORD|TOKEN)", DOCKERFILE, re.M)
    assert "requirements-dev" not in DOCKERFILE


def test_compose_allow_lists_trust_proxy_default_false():
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
    assert "METIS_ACCOUNTS_TRUST_PROXY: ${METIS_ACCOUNTS_TRUST_PROXY:-false}" in compose


def test_ci_and_makefile_run_same_scope():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    mk = (ROOT / "Makefile").read_text()
    for cmd in (
        "ruff check backend ml leadstudio tests/foundation",
        "ruff format --check backend ml leadstudio tests/foundation",
        "bandit -c pyproject.toml -r backend ml leadstudio",
        "pip-audit -r requirements.txt -r ml/requirements.txt",
    ):
        assert cmd in ci and cmd in mk, cmd
    assert "mypy" in ci and "mypy" in mk
    assert 'files = ["backend", "ml"]' in (ROOT / "pyproject.toml").read_text()


def test_numpy_pinned_everywhere():
    for f in ("requirements.txt", "ml/requirements.txt"):
        assert re.search(r"^numpy==\d", (ROOT / f).read_text(), re.M), f
