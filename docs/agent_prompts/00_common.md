# Common rules for every agent
- Spec: https://claude.ai/artifact/KudmXbhLuoMbWZFZmmvVzw ; wireframes: https://claude.ai/artifact/5jJVkyZNkt617PFQ1N3tyS . They are the contract; if your task conflicts with them, stop and report BLOCKED.
- Edit only paths you own (docs/OWNERSHIP.md). Never edit `backend/schemas/`, `backend/db/migrations/` or `docs/` (integrator-only); request changes in your handoff.
- Never push, never run paid compute, never contact anyone, never commit secrets, real data or model weights.
- No verdicts or traffic-light language for predictions; always show value, interval or probability, method note, N, coverage, sample size.
- Design: dark charcoal, plain scientific look, no gradients, no shadows, no decorative effects.
- Label results honestly: placeholder, predicted, measured.
- Tests required for everything you build; run them and report real output.
- Security findings go to `private/`, not into committed files.
- End with the completion envelope:

```
SUPERSET_WORKER_DONE
task: <id>
summary: <one line>
files: <paths>
checks: <commands and outcomes>
handoff: <context or none>
```
or `SUPERSET_WORKER_BLOCKED` with `task`, `reason`, `needs`.
