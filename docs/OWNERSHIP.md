# Directory ownership
Only the owner edits a path. Contracts, migrations and docs are integrator-only.

| Path | Owner |
| --- | --- |
| docs/, backend/schemas/, backend/db/migrations/, tests/db, tests/contract | Integrator (0) |
| deploy/, CI, backend/core | Foundation (1) |
| ml/data/, docs/LICENSES.md | Data and licensing (2) |
| web/ (design system, screens) | Design (3) |
| backend/accounts | Accounts and security (4) |
| ml/train, ml/uncertainty, ml/benchmarks | Models (5) |
| backend/projects, compounds, predictions, ranking, exports, jobs | Domain API (6) |
| backend/dockprep | Docking prep (7) |
| web API client, tests/e2e | Wiring (8) |
| tests/security, private/ reviews | QA/security (9) |
