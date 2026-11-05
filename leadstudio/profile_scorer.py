"""ProfileScorer: pure wrapper that applies a saved domain profile on top of any Scorer.

* profile.objectives override/extend RunConfig.objectives before the base scorer runs.
* every avoid-SMARTS that matches adds AlertHit(family="USER") (first match's atoms) and
  multiplies the composite by USER_PENALTY per matching pattern (documented, deliberately simple).
* descriptors.alerts is increased by the number of USER hits; components["user_avoid"] is added.

Server resolution: `leadstudio.server.routes` loads the caller's own profile row (404 if it is not theirs) and runs
the scorer wrapped for that run only; the shared engine is never mutated.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from rdkit import Chem

from leadstudio.matching import match
from leadstudio.profiles import MAX_SMARTS_ATOMS, ProfileBody
from leadstudio.schema import AlertHit, RunConfig, ScoreResult

USER_PENALTY = 0.8


class AllowlistRegistry:
    """Read-only view of a registry restricted to the edit ids of a saved profile (`edit_allowlist`).

    The GA's mutation step only sees `applicable()`, so filtering there confines every proposed edit to the list.
    The wrapped registry is never changed (it is shared by all runs). BRICS crossover is not a registry edit and
    stays governed by `RunConfig.use_crossover`. An id the registry does not know is simply never matched."""

    def __init__(self, inner, allowed):
        self._inner = inner
        self.allowed = frozenset(allowed)

    def __getattr__(self, name):  # demo_safe etc.: anything else is the inner registry's
        return getattr(self._inner, name)

    def all(self):
        return [op for op in self._inner.all() if op.id in self.allowed]

    def get(self, edit_id):
        if edit_id not in self.allowed:
            raise KeyError(edit_id)
        return self._inner.get(edit_id)

    def applicable(self, smiles, locked_atoms=(), *args, **kw):
        return [
            (op, site)
            for op, site in self._inner.applicable(smiles, locked_atoms, *args, **kw)
            if op.id in self.allowed
        ]


SCORE_MATCH_BUDGET_S = 0.5


class ProfileScorer:
    def __init__(self, base, profile) -> None:
        self.base = base
        self.profile = profile if isinstance(profile, ProfileBody) else ProfileBody.model_validate(profile)
        self._patterns = [(s, Chem.MolFromSmarts(s)) for s in self.profile.avoid_smarts]
        # a stored pattern above the validation cap (older rows) is skipped, never matched
        self._patterns = [
            (s, q) for s, q in self._patterns if q is not None and q.GetNumAtoms() <= MAX_SMARTS_ATOMS
        ]

    def _config(self, config: RunConfig) -> RunConfig:
        if not self.profile.objectives:
            return config
        merged = {**config.objectives, **self.profile.objectives}
        return config.model_copy(update={"objectives": merged})

    def score(self, smiles: str, config: RunConfig) -> ScoreResult:
        res = self.base.score(smiles, self._config(config))
        mol = Chem.MolFromSmiles(smiles)
        hits: list[AlertHit] = []
        if mol is not None:
            deadline = (
                time.monotonic() + SCORE_MATCH_BUDGET_S
            )  # one budget for all patterns of this candidate
            for smarts, q in self._patterns:
                found = match(mol, q, max(0.0, deadline - time.monotonic()))
                if found.matches:
                    hits.append(
                        AlertHit(name=f"avoid: {smarts}", family="USER", atom_indices=list(found.matches[0]))
                    )
        if not hits:
            return res
        n = len(hits)
        desc = res.descriptors.model_copy(update={"alerts": res.descriptors.alerts + n})
        comps = {**res.components, "user_avoid": max(0.0, 1.0 - 0.2 * n)}
        return res.model_copy(
            update={
                "descriptors": desc,
                "components": comps,
                "alerts": [*res.alerts, *hits],
                "composite": max(0.0, res.composite * (USER_PENALTY**n)),
            }
        )

    def score_many(self, smiles: Sequence[str], config: RunConfig) -> list[ScoreResult]:
        return [self.score(s, config) for s in smiles]
