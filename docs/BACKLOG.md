# Backlog (deferred work)
Failure explanations; MD; route planning; GPU hosting and docking runs; bulk uploads; Glide and other engines; recommender from user preferences; company-hosted-platform channel; paid collaboration UI (shared datasets, notes, fine-tuned models); fine-tuning on own data; API access; kinetic solubility if data not cleared; logP dataset (TDC set is logD 7.4); Metis RL and lead optimization; fragment-attribution improvements; grantee access to predictions/measurements; edit/comment permission enforcement; rate-limit and session tables; object-storage key model; SSO/institutional accounts.

Wave 2/3 items from decisions 0002/0003: default desirability ranges from training data + editable min/max inputs in the ranking panel; replace draw placeholder with vendored Ketcher.

## Added after Wave 2 QA
- Migration 0004: rate_limit purge grant, session_revocation purge, lock reset in SQL, unverified-account expiry/replacement, reset clears lock.
- F-75: bound ranking weights (0-10) in the contract. F-105: add a measured-coverage field; stop reporting 1-ECE as "coverage".
- Wire the job table to a durable worker; per-user rate limit on /dockprep; fpocket in the image; `__Host-` cookie prefix; remove blanket noqa headers in backend/dockprep; extend ruff format beyond backend/core.

## Added after Wave 3 (approved 2026-11-04)
Done in Wave 3: migration 0004 items, durable job records, change-password, terms re-acceptance, maintenance runner, Ketcher, real-API web.
Open QA findings (strict xfail, details private):
- MEDIUM: F-122 unverified registration keeps first caller's password (set password at verification); F-126 jobs assume one API process (second process fails live jobs as interrupted); F-129 runbook purge query returns nothing as metis_readonly (forced RLS; needs SECURITY DEFINER function, migration 0005, with `job_fail_interrupted()`).
- LOW: F-120 model page prints bins as [object Object]; F-121 Ketcher in same-origin unsandboxed iframe; F-123 terms gate only blocks mutating methods; F-124 terms accept unrate-limited; F-125 old reset tokens survive new reset/forgot/change; F-127 project delete resets job quota; F-128 maintenance runner needs full production settings; F-131 mail timing/500 reveals registered addresses once a real mailer exists; F-132 no HSTS; F-133 authenticated JSON lacks Cache-Control no-store; F-134 rate-limit windows over 2 h reset by purge floor. Plus F-75, F-105.
Backend gaps from the web wiring: /auth/me lacks email; no .edu upgrade route; no pre-start time estimate; no SMILES depiction route for docking-prep ligands; no project rename or compound/note/measurement delete routes; model card lacks higher_is_better.
Operations: no real mailer (console only; launch blocker); METIS_REDIS_URL required but unused; metis_app_login provisioning unscripted; deletion_request purge bookkeeping manual; Docker never built; compose `maintenance` service; raise secret-key minimum to 64 bytes; blanket noqa in backend/dockprep.
Licences: record Ketcher NOTICE (LGPL-2.1 listed) and dockprep tools (pdb2pqr BSD-3, propka LGPL-2.1, Open Babel GPL-2.0, Meeko LGPL-2.1, fpocket unverified) in docs/LICENSES.md; update TRACEABILITY.

## Before launch: legal review
The Terms of Service, Privacy Policy and Disclaimer linked from sign-up are drafts. They are not legal advice and need lawyer review before launch. (The warning box was removed from the sign-up page on the owner's instruction; the DRAFT notes on the three pages under web/legal/ remain.)

## Before launch: Lead Studio disclaimer
The banner on Metis Lead Studio (Beta) ("Generated molecules, binding scores and poses are model outputs, not measurements. Treat them as hypotheses to test.") was removed from the page on the owner's instruction. Restore a model-output disclaimer before launch (the terms/disclaimer drafts still need lawyer review).
