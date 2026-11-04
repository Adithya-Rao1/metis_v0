# Agent 8b: restyle the Metis Lead Optimization Studio to match Metis v1 (Wave 3)

Worktree: `/Users/adithyarao/.superset/worktrees/GraphDQN/restyle` (repo GraphDQN, branch `demo/restyle-metis`, created from `origin/demo/integrate`). Reference design: `/Users/adithyarao/.superset/projects/metis_v0/web/` (read-only for you). Python with Playwright for screenshots: `/Users/adithyarao/.superset/projects/metis_v0/.venv/bin/python`. Do not create virtualenvs. You may commit to branch `demo/restyle-metis` in this worktree; do NOT push, merge, or touch any other branch or worktree, and do not change backend behaviour.

You own: `studio/web/` only (css, html, js, fixtures only when a visual change needs it). You may NOT edit `studio/server`, `studio/engine`, `studio/policy`, scoring, API routes or tests outside `studio/web/`.

## Why
The owner will film a demo video that shows the Metis v1 site and this studio together, so the studio must look like the same product: same typography, colours, chrome, controls and logo, with every studio feature and behaviour unchanged.

## Make it match (read `web/css/app.css`, `web/index.html`, `web/app.html`, `web/js/chrome.js`, `web/assets/benzene.svg` first)
- Tokens: adopt Metis v1 light/dark tokens (`--bg --panel --line --soft --fg --muted --accent --accent-soft --note-* --err --s1..s8`), IBM Plex Sans (vendor the same woff2 files and OFL.txt from `web/fonts/` into `studio/web/fonts/`), 14px base type, dense panels with 1px lines, small radii (3px), light/dark via `prefers-color-scheme` and `data-theme` exactly like Metis v1.
- Remove what Metis v1 forbids in the studio chrome and panels: gradients (including the conic logo), box/text shadows, glows, blur, pill badges, animated decorative effects. Replace the logo with the RDKit benzene mark (`web/assets/benzene.svg`) + "Metis" wordmark. Keep the app bar structure (brand, breadcrumb/stage nav, spacer, status) consistent with Metis v1's `.appbar`/`.nav`.
- Colour semantics: no green/red verdict colours. Show property changes with a sign, an arrow glyph and the series colours (`--s1..s8`) or neutral text, and always label predicted vs measured/computed values as Metis v1 does ("predicted"). Source badge (mock/live) becomes a small flat bordered label using `--note-bg/--note-line` for mock.
- The 3D stage stays dark in both themes (atom colours must read the same) but with a flat background (no radial gradient) and no glow/halo overlays; keep ligand/protein rendering, morphs, ghost overlays and manual-edit overlays working.
- Controls: buttons, inputs, selects, tabs, tables, dialogs and charts use Metis v1 styles (`web/css/app.css`). Charts use the `--s*` series colours with direct labels; keep keyboard focus rings (`:focus-visible` 2px accent) and the phone-width layout (no horizontal page scroll at 390px).
- Do not change copy that carries scientific meaning; you may adjust wording only to match Metis v1 conventions (e.g. "predicted").
- Add a short `studio/web/README.md` section "Design parity with Metis v1" listing the tokens/files shared and any intentional differences.

## Verify
Use the offline dev harness and fixtures already in `studio/web/dev/` and `studio/web/fixtures/` (no backend needed). With Playwright capture before/after screenshots at 1440x900 and 390x844 in light and dark for each stage (binding, pose, pharmacology, manual, adapt, account, panels) into `/private/tmp/claude-501/-Users-adithyarao--superset-worktrees-GraphDQN-set-up-repository-work-e4471620/d6afe81d-5165-46cf-8d58-790ada740d85/scratchpad/restyle_shots/` (not into the repo). Confirm: zero console errors, no horizontal scroll at 390px, no `gradient`/`box-shadow`/`text-shadow`/`filter: blur` left in `studio/web/css` (grep and report any intentional exception with the reason), and existing studio web tests still pass (`studio/web/fixtures/test_fixtures.py` and any JS/py tests under `studio/web` or referencing it; run what exists and report counts). Compare side by side with the equivalent Metis v1 page screenshots and fix visible mismatches in header, fonts, spacing and colours.

End with the envelope:
```
SUPERSET_WORKER_DONE
task: wave3-restyle
summary: ...
files: ...
checks: ...
handoff: screenshot folder, intentional differences, anything that still looks off
```
or `SUPERSET_WORKER_BLOCKED` with `reason:` and `needs:`.
