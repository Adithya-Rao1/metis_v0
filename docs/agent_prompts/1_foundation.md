# Agent 1: Foundation
See 00_common.md. Owns: deploy/, CI, backend/core
Build: Docker compose (app, Postgres, queue, object-store emulator), settings, structured logging with no structures/PII, lint/format/type config, pre-commit, CI (tests, bandit, pip-audit, gitleaks, trivy), run baseline migration in CI against Postgres.
Depends on: Wave 0
Acceptance: tests for each item pass; TRACEABILITY rows for this agent can be closed.
