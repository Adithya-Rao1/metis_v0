# Agent: update tests for the FreeSolv licence decision (branch wave5/w5-freesolv)

Worktree `<worktrees>/metis_v0/w5-freesolv`. Python `<project>/metis_v0/.venv/bin/python` (absolute path <project>/metis_v0/.venv/bin/python). Do NOT push or contact anyone; no absolute home paths in committed files.

## Context
The owner verified FreeSolv is CC-BY-4.0 (2026-11-05). Main now has `ml/data/datasets.yaml` FreeSolv = cleared (date_checked 2026-11-05, confirmed_by owner) and `ml/data/manifest.py` no longer hard-codes FreeSolv (`ALWAYS_REFUSED` is empty, alias/source regex removed). The licence gate for everything else must stay exactly as strict: only allow-listed licences can be cleared, non-commercial / no-derivatives terms (e.g. CC BY-NC-SA, CC BY-ND) can never be cleared under ANY id/name/alias, a cleared entry needs an ISO `date_checked` (not in the future) and a real `confirmed_by`, pending/excluded/unknown datasets are not trainable.

## Do
Update the tests that encoded "FreeSolv can never be cleared / is excluded" so they test the NEW policy without weakening the gate:
- tests/data/test_baselines_cards_loader_data.py (test_card_content, test_loader_cache_and_gates, test_cli_does_not_download_without_flag)
- tests/data/test_manifest_data.py (test_entries_and_fields, test_all_tdc_pending_and_untrainable, test_freesolv_never_loadable, test_f25_f26_cleared_rejects_freesolv_alias_and_nc[kw0,kw1])
- tests/ml/test_ml_train.py (test_run_training_refuses_pending_and_excluded_before_reading_data)
- tests/security/test_wave1_redteam.py (the freesolv_* tests, test_pending_and_unknown_datasets_are_not_trainable, test_aliased_freesolv_cannot_be_cleared, test_data_card_flags_pending_and_excluded_status)
- tests/security/test_wave2_redteam.py::TestML::test_license_gate_direct_calls_and_aliases
Rules: replace "FreeSolv is refused" by (a) a positive test that FreeSolv is cleared and trainable with CC-BY-4.0 and the recorded date/confirmer, and (b) STRONGER generic tests that a dataset with a non-commercial/no-derivatives licence is refused under every id and alias spelling the old tests used (use a synthetic dataset id such as `SomeNCSet`, and keep checking the old alias spellings against a manifest entry whose licence is CC BY-NC-SA). Where a test needs an "excluded" dataset, build a temp manifest with an excluded entry. Keep the `excluded` status, ALWAYS_REFUSED mechanism and `assert_loadable` semantics working (tests can inject an id into ALWAYS_REFUSED with monkeypatch). Do not weaken any other assertion; list every test you changed and why. Also check `tests/security/wave3/test_wave3_lead.py::test_rate_limit_is_enforced_by_the_database_limiter_across_requests` fails for an unrelated reason (it is probably a timing/load flake): run it alone and report.

## Acceptance
`pytest -q -p no:cacheprovider tests/data tests/ml tests/security/test_wave1_redteam.py tests/security/test_wave2_redteam.py` green with real counts; `ruff check` and `ruff format --check` on the files you touched. Commit; do not push.

End with the envelope (SUPERSET_WORKER_DONE / task: w5-freesolv / summary / files / checks / handoff) or SUPERSET_WORKER_BLOCKED.
