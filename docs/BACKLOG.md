# Backlog (deferred work)
Failure explanations; MD; route planning; GPU hosting and docking runs; bulk uploads; Glide and other engines; recommender from user preferences; company-hosted-platform channel; paid collaboration UI (shared datasets, notes, fine-tuned models); fine-tuning on own data; API access; kinetic solubility if data not cleared; logP dataset (TDC set is logD 7.4); Metis RL and lead optimization; fragment-attribution improvements; grantee access to predictions/measurements; edit/comment permission enforcement; rate-limit and session tables; object-storage key model; SSO/institutional accounts.

Wave 2/3 items from decisions 0002/0003: default desirability ranges from training data + editable min/max inputs in the ranking panel; replace draw placeholder with vendored Ketcher.

## Added after Wave 2 QA
- Migration 0004: rate_limit purge grant, session_revocation purge, lock reset in SQL, unverified-account expiry/replacement, reset clears lock.
- F-75: bound ranking weights (0-10) in the contract. F-105: add a measured-coverage field; stop reporting 1-ECE as "coverage".
- Wire the job table to a durable worker; per-user rate limit on /dockprep; fpocket in the image; `__Host-` cookie prefix; remove blanket noqa headers in backend/dockprep; extend ruff format beyond backend/core.
