# Agent: Wave 4 QA / security review of Lead Studio and launch readiness (branch wave4/w4-qa)

Worktree: `<worktrees>/metis_v0/w4-qa`. Python `<project>/.venv/bin/python`. You write ONLY `tests/security/**` and the git-ignored `private/qa_wave4.md` (create it in the MAIN checkout `<project>/private/`, not in the worktree). Do NOT modify product code, migrations, web, docs. Do not push or commit exploit detail to public files. Same conventions as `private/qa_wave3.md` (read it first): `xfail(strict=True, reason="F-xxx SEV: ...")` for a reproducible defect; start IDs at F-140.

## Review (independent, adversarial)
1. `leadstudio/` and migration `0005_lead_studio.sql`: authn/authz on every `/lead/*` route (sweep from `app.openapi()` paths), cross-user isolation (runs, SSE, poses, manual sessions, adapt jobs, library, profiles), RLS on the four new tables incl. forced RLS and grants, CSRF, size/row caps, rate limits, the structure-file route (path traversal, symlinks, encoding tricks), SSRF via the dock URL, SMARTS/SMILES DoS (time/size), SSE resource leaks, memory growth of in-process run state, logging of SMILES, pickle/yaml/eval use in copied code (`molmods`, `sa`, `registry`, `policy`), `deap`/torch model loading safety (no untrusted pickles), secrets in the copied data.
2. Public-repo hygiene: scan the whole repo (incl. `leadstudio/data`) for secrets, keys, personal data, absolute home paths, hostnames/IPs (e.g. the GPU box address), cookies; check `.gitignore`, large/binary files, licence notices of copied data and third-party code against `docs/LICENSES.md` (list gaps: deap LGPL-3.0, moocore, Ketcher NOTICE, PLAPT/ProtBert/ChemBERTa, DynamicBind, 3Dmol, IBM Plex fonts, pdb2pqr, propka, Open Babel, Meeko, fpocket).
3. Re-test the Wave 3 open findings that the backend/web agents are fixing in parallel (do not wait for them; just note state).
4. Deployment readiness: read `deploy/`, `docs/OPERATIONS.md`, `.github/workflows/ci.yml`: list what blocks a public launch (no Docker build ever run, no real mailer, TLS/HSTS/cookie assumptions, backup/restore and deletion drills, monitoring without structures, secret management, single-process assumptions). Tests only where something is testable offline.
5. Verify claims in `docs/THREAT_MODEL.md`, `docs/DB_SCHEMA.md` and `docs/OPERATIONS.md` against code; list mismatches.

## Acceptance
`.venv/bin/python -m pytest -q -p no:cacheprovider tests/security -rxX` run to completion with real counts (new defects are strict xfails; protections that hold are unmarked passing tests). Commit on your branch; do not push.

End with the envelope (task: w4-qa; counts; severity table; verified protections; launch blockers; handoff) or SUPERSET_WORKER_BLOCKED.
