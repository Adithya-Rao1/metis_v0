# Agent: Wave 4 web hardening (branch wave4/w4-web)

Worktree: `/Users/adithyarao/.superset/worktrees/metis_v0/w4-web`. Python `/Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python`. Do NOT push or touch `backend/`, `leadstudio/`, migrations, `tests/security/` (except removing the strict-xfail marker of a finding you fix), docs other than `web/README.md`/`docs/WEB_WIRING.md`. You own `web/`, `tests/web/`, `tests/e2e/`. Findings detail: `/Users/adithyarao/.superset/projects/metis_v0/private/qa_wave3.md` (read-only, never copy exploit detail into public files).

## Do
1. F-120: model page prints arrays of objects as `[object Object]`: render reliability bins and per-similarity-bin coverage as small readable tables (textContent only).
2. F-121: stop running Ketcher in a same-origin unsandboxed iframe. Preferred: wrap it in a thin same-origin page loaded in `<iframe sandbox="allow-scripts">` (opaque origin) with a postMessage bridge for `getSmiles()`/`setMolecule()`; origin-check every message; keep the vendored bundle and VERSIONS.txt hashes unchanged; update `_headers`. Prove with a test that the bundle cannot read the parent's cookies/DOM or call `/api` with credentials.
3. F-132: add `Strict-Transport-Security` (max-age 31536000; includeSubDomains) and keep all existing headers in `web/_headers`; add a test that every path rule has it.
4. Wire the new backend routes as they land (another agent is building them in parallel, contract: `GET /auth/me` returns `email`; project rename `PATCH /projects/{id}`; delete for compounds/notes/measurements; SMILES depiction route for docking-prep ligands; model card `higher_is_better`; 403 `terms_reaccept_required` also on GETs). Build against `tests/web/mockapi.py` (extend it to match), show account email on the account page, rename/delete controls with confirm dialogs, and render the ligand/nearest-training pictures in docking prep when the route exists.
5. Lead Studio (`web/lead/`): after pose the right-hand card and 3D marks must not remain on other stages (re-verify), make the pose wait cheap: one protein cartoon during the wait and `devicePixelRatio` capped at 1.5 for the viewer; show elapsed seconds in the wait label; keep keyboard access; the header Metis nav stays.
6. Accessibility pass (axe or the existing a11y helpers) on every page incl. `lead/index.html`: zero serious/critical issues; phone width without horizontal scroll.

## Acceptance
`.venv/bin/python -m pytest -q -p no:cacheprovider tests/web tests/security/web tests/e2e --deselect tests/web/test_screenshots.py` green, real counts (note: runs ~10 min; run focused files while working). No weakened assertions; list changes. Commit on your branch; do not push.

End with the envelope (task: w4-web; summary; files; checks; handoff incl. anything unverified) or SUPERSET_WORKER_BLOCKED.
