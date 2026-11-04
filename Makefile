PY ?= python
.PHONY: install test lint type sec migrate
install:
	$(PY) -m pip install -r requirements-dev.txt
test:
	$(PY) -m pytest -q
lint:
	ruff check backend/core tests/foundation && ruff format --check backend/core tests/foundation
type:
	mypy
sec:
	bandit -c pyproject.toml -r backend && pip-audit -r requirements.txt
migrate:
	$(PY) -m backend.core.migrate
