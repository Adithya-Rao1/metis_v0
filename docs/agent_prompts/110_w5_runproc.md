# Agent: Lead Studio run isolation (branch wave5/w5-runproc)

Worktree `/Users/adithyarao/.superset/worktrees/metis_v0/w5-runproc`. Python `/Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python` (absolute path: /Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python). Do NOT push, contact anyone, touch `web/`, migrations, `backend/accounts`. You own `leadstudio/` (server/core.py, engine, scoring plumbing needed), `tests/leadstudio/`, `docs/OPERATIONS.md` section 7, `docs/THREAT_MODEL.md` Lead Studio rows. Security is critical; read `/Users/adithyarao/.superset/projects/metis_v0/private/qa_wave4.md` (read-only, never copy exploit detail into public files).

## Goal
Today Lead Studio parses molecules and runs the GA inside the API process (a native RDKit crash would take the API down; a timed-out run leaks a Python thread, bounded to 4). Move the compute out of the API process.
1. Run each GA run in its own **child process** (multiprocessing `spawn`, not fork) with a hard wall-clock timeout, memory limit (RLIMIT_AS where the OS supports it), and `os.kill` on timeout/cancel/client-stop. Events stream back to the API process through a bounded pipe/queue; the SSE and run-state logic, caps (per user, per process, queue), user-keyed isolation, replay cache and `source: mock|live` stay exactly as they are. A crash in the child marks the run failed with a plain message; the API stays up. No thread leak: remove `MAX_ABANDONED`-style accounting or keep it only as a belt-and-braces counter.
2. Molecule validation / manual preview / SMARTS checks (the HeavyGate paths) also run in a killable worker process pool (small, e.g. 2 workers, recycled after N jobs, timeout per job) so a hang or native crash cannot stall or kill the API.
3. Children inherit NO secrets: pass only the run config and a minimal env; no DB credentials, no JWT key. Never log SMILES.
4. Keep the existing tests green (adjust test plumbing, not assertions) and add tests: child killed on timeout and on cancel, child crash leaves API serving, no orphan processes after shutdown, cross-user isolation unchanged, caps unchanged. Remove the strict-xfail markers in `tests/security/wave4/` that your fix turns into XPASS (that is the only edit allowed outside your tree).
5. Document in OPERATIONS.md: what runs where, new env vars (timeouts, memory limit, pool size), single-process API assumption (still true for run state; say what multi-process would need), and update the THREAT_MODEL Lead Studio row (now isolated in child processes).

## Acceptance
`pytest -q -p no:cacheprovider tests/leadstudio tests/security tests/foundation tests/api` green (real counts), ruff check/format --check, mypy, bandit clean as before. Do not weaken assertions; list any you changed and why. Commit logically; do not push.

End with the envelope (SUPERSET_WORKER_DONE / task: w5-runproc / summary / files / checks / handoff) or SUPERSET_WORKER_BLOCKED.
