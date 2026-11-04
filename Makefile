PY ?= python
# Commands and scopes are identical to .github/workflows/ci.yml (tests/foundation enforces this).
.PHONY: install test lint type sec migrate
install:
	$(PY) -m pip install -r requirements-dev.txt -r ml/requirements.txt
test:
	$(PY) -m pytest -q
lint:
	ruff check backend ml tests/foundation && ruff format --check backend/core tests/foundation
type:
	mypy
sec:
	bandit -c pyproject.toml -r backend ml && pip-audit -r requirements.txt -r ml/requirements.txt
migrate:
	$(PY) -m backend.core.migrate
