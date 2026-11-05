# Threat model (v1)

Classes of threat and mitigations. Specific findings from reviews are kept private until fixed.

| Threat | Mitigation |
| --- | --- |
| One user reads or changes another's data | owner_id on every row, forced RLS keyed on per-request id, app-level grant checks, 404 not 403 for others' ids, isolation tests per table |
| Credential theft/stuffing | bcrypt on SHA-256 prehash, uniform login errors, lockout after 5 failures, rate limits, email verification |
| Token replay | Tokens stored as hashes, expiring, single use (atomic UPDATE) |
| SQL injection | Parameterized SQLAlchemy only; app role has no DDL; bandit and grep checks |
| Hostile uploads (SDF/CSV/PDB) | Size and type limits, parsed in sandboxed worker with time/memory limits, never executed |
| Data leakage via logs/analytics | No structures, notes or personal data in logs; no third-party analytics without consent |
| Silent model/data misuse | Publish gate requires cleared licenses; consents default off; training only on consented data |
| Tampering with audit trail | Append-only trigger and privileges |
| Backup retention after deletion | deletion_request with 30-day purge due (proposed), purge test |
| Secrets in the public repo | .gitignore, gitleaks, secret scanning + push protection, placeholders only |
| Supply chain | Pinned deps, pip-audit, container scan, SBOM |
| Denial of service by large jobs | 10-compound cap, per-user quotas, statement timeouts, queue limits |
| Lead Studio (Beta) misuse: another user's runs, poses or saved sessions; path traversal to files; SSRF through the docking URL; runaway jobs | Every `/lead/*` route needs the Metis session, CSRF on writes and the terms gate; run state keyed by user id; saved runs, manual sessions and profiles in forced-RLS tables (migration 0005); structure files served from a whitelist; docking URL only from settings (loopback or explicit allow-list); per-user run caps, rate limits, size caps and timeouts |
| Third-party code in the browser (Ketcher editor) | Vendored with pinned hashes, strict per-path CSP, no third-party requests; isolation of the editor from the app origin is tracked in BACKLOG (F-121) |
| Model outputs mistaken for measurements | Required fields (interval, N, coverage, method note) on every prediction; models labelled sample/synthetic until benchmarks are published (G2) |

Out of scope for v1: SSO, enterprise tenancy, HIPAA/regulated data. The service is not for patient data.
Before launch: external security review, restore and deletion drills (gate G4).
