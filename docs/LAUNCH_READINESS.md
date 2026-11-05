# Launch readiness (gate G4) - status as of 2026-11-05

What is built, what blocks a public launch, and what the owner must decide. Nothing here is a claim that the service is ready.

## Gates
| Gate | Status |
| --- | --- |
| G1 written licence answers (TDC; ADMET-AI if its weights are wanted) | OPEN: nothing is trained on real data; training compute is the owner's remote GPU box |
| G2 benchmarks vs baselines for every hosted model | OPEN: no real models exist; the site runs SYNTHETIC sample models |
| G3 owner validates fragment attribution | OPEN: attribution is implemented but unvalidated |
| G4 external security review + closed beta | OPEN: review package below is ready for a reviewer; beta not started |

## Launch blockers (ordered)
1. Real models: train, calibrate, benchmark, publish model cards (G1, G2). Until then the public site must say sample models.
2. Real mailer (sign-up verification, reset): the current mailer prints to the console. Needs a domain, SPF/DKIM, and a provider.
3. Docker images have never been built (Docker is not installed on the development machine); `docker compose up` from a clean clone is unverified.
4. Production services: managed Postgres with TLS, encryption at rest and backups; private object storage; secrets manager; TLS + HSTS at the edge; domain.
5. Lawyer review of Terms, Privacy and Disclaimer (drafts in docs/legal); the draft warning box was removed from sign-up on the owner's instruction, and the Lead Studio disclaimer banner too: restore a model-output disclaimer before launch.
6. Lead Studio: runs state is in one process (single worker only); DynamicBind and PLAPT need a GPU host; their model licences are unchecked (docs/LICENSES.md).
7. Drills: restore-from-backup drill and account/data deletion drill (including the 30-day backup purge), documented in docs/OPERATIONS.md.
8. Monitoring and uptime checks that store no structures or personal data.
9. Open QA findings: see docs/BACKLOG.md and the Wave 4 report.

## Security review package (hand to the external reviewer)
docs/THREAT_MODEL.md, docs/DB_SCHEMA.md (roles, RLS, SECURITY DEFINER functions), docs/OPERATIONS.md, docs/OWNERSHIP.md, the migration files in backend/db/migrations, the security test suites in tests/security (strict-xfail tests document known open defects), CI configuration. Private QA reports stay out of the public repo and are shared with the reviewer directly by the owner.

## Closed beta plan (owner to run; I only draft)
Two or three chemist contacts, invitation by account creation on a staging deploy; collect: unclear uncertainty displays, wrong chemistry, missing endpoints, time-to-result; success = they can predict, rank and prepare docking files for their own molecules and explain what the interval means. No real proprietary structures until the deletion drill has passed. Draft invitation text to be written on request; nothing is sent by me.
