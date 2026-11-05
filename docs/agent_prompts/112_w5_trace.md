# Agent: TRACEABILITY re-key (branch wave5/w5-trace)

Worktree `<worktrees>/metis_v0/w5-trace`. Python `<project>/metis_v0/.venv/bin/python`. Read-only on all code and tests. You write ONLY `docs/TRACEABILITY.md`. Do not push.

Re-key `docs/TRACEABILITY.md` row by row: every row of the spec (`<project>/metis_v0/private/SPEC_DIGEST.md`, read-only, git-ignored, never copy it verbatim into public files) and every wireframe screen, plus the Lead Studio, must map to the code path and to NAMED passing tests (file::test_name), verified by actually running those tests (`pytest -q -p no:cacheprovider <ids>`). Status per row: covered / partially covered / not covered (state the gap) / not applicable until real models (G1-G3). Be honest: a row with no passing test says so. Remove the stale "Status note" paragraph. Also add a short table of the gates G1-G4 with their current status. Commit; do not push.

End with the envelope (task: w5-trace; summary incl. counts of covered/partial/not covered rows) or SUPERSET_WORKER_BLOCKED.
