# Spec and wireframe traceability
Status: `schema` = covered by Wave 0 tests; `todo` = later wave. Spec: https://claude.ai/artifact/KudmXbhLuoMbWZFZmmvVzw; wireframes: https://claude.ai/artifact/5jJVkyZNkt617PFQ1N3tyS

| Requirement | Agent | Where | Test | Status |
| --- | --- | --- | --- | --- |
| 10-compound cap | 0/6 | contracts, compound trigger | test_contracts, test_compound_cap_10 | schema |
| Prediction fields required, no verdicts | 0 | schemas.Prediction, prediction CHECKs | test_required_fields, test_no_verdict_fields_accepted | schema |
| Ensemble N in 3/5/10/20, tier caps | 0/5/6 | contracts, prediction CHECK | test_ensemble_sizes | schema |
| Ranking config (weights, limits, lambda default 0.25) | 0/6 | schemas.RankingConfig | test_ranking_config_defaults_and_bounds | schema (formula todo, Wave 2) |
| Consents default off | 0/4 | consent table | test_consent_default_none... | schema |
| Terms acceptance at sign-up, versioned | 0/4 | auth_register | test_register_* | schema (UI todo) |
| Model publish gate | 0/5 | model_version CHECK | test_model_publish_gate | schema |
| Deletion cascade | 0/4 | FKs | test_account_delete_cascades_everything | schema |
| Home page layout (logo TL, wordmark mid-left, 1M17 complex TR, button bottom-center) | 3 | web/ | bounding-box test | todo (Wave 1) |
| 2D and 3D wherever a molecule appears | 3 | web/ | component test per location | todo (Wave 1) |
| Uncertainty methods, calibration, attribution | 5 | ml/ | conformal coverage tests | todo (Wave 2) |
| Docking prep (Vina, GNINA) | 7 | backend/dockprep | 1M17 tests | todo (Wave 2) |
| Exports PDF/SDF/CSV with disclaimer | 6 | backend/exports | content tests | todo (Wave 2) |
