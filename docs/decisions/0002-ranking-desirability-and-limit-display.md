# 0002 Ranking desirability range and failed-limit display (owner decision, 2026-11-04)
- Desirability d_ij maps a property value linearly between a min and max (0 at the bad end, 1 at the good end, clamped; flipped for lower-is-better). Default min/max come from the model's training data (to be produced by the models agent and stored with the model version); users can edit them per property in the ranking panel. Contract: `PropertyWeight.target_min/target_max` (both or neither).
- A compound that breaks a hard limit stays in the table, ranks below all passers, and its rank cell shows BOTH the numeric rank and the word "fail" (plus the per-cell "limit: ..." note).
- TODO (Wave 2/3): API returns default ranges per endpoint; ranking panel gets editable min/max inputs.
