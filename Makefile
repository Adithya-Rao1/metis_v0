PY ?= python
# Commands and scopes are identical to .github/workflows/ci.yml (tests/foundation enforces this).
.PHONY: install test lint type sec migrate demo
install:
	$(PY) -m pip install -r requirements-dev.txt -r ml/requirements.txt
test:
	$(PY) -m pytest -q
lint:
	ruff check backend ml leadstudio tests/foundation && ruff format --check backend ml leadstudio tests/foundation
type:
	mypy
sec:
	bandit -c pyproject.toml -r backend ml leadstudio && pip-audit -r requirements.txt -r ml/requirements.txt
migrate:
	$(PY) -m backend.core.migrate
demo:  # local demo on http://127.0.0.1:8080 with SYNTHETIC sample models (see scripts/demo.py)
	$(PY) scripts/demo.py
