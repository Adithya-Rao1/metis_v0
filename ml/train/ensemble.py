"""Pool of 20 ensemble members per endpoint. Each member has a distinct seed and a scaffold-resampled
training subset (random 80% of Murcko-scaffold groups; acyclic molecules are their own groups).
N in {3,5,10,20} always uses the first N members, so calibration per N is well defined."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import numpy as np
from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

from ml.train.member import MemberModel

POOL_SIZE = 20


def member_seed(base_seed: int, k: int) -> int:
    return int(base_seed) * 1000 + k


def scaffold_groups(smiles: list[str]) -> list[list[int]]:
    groups: dict[str, list[int]] = {}
    singles: list[list[int]] = []
    for i, s in enumerate(smiles):
        mol = Chem.MolFromSmiles(s)
        if mol is None:
            raise ValueError(f"unparseable SMILES at {i}")
        sc = MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
        if sc:
            groups.setdefault(sc, []).append(i)
        else:
            singles.append([i])
    return list(groups.values()) + singles


def member_subset(groups: list[list[int]], seed: int, frac: float = 0.8) -> list[int]:
    rng = np.random.default_rng(seed)
    k = len(groups) if len(groups) < 3 else max(2, int(round(frac * len(groups))))
    chosen = rng.choice(len(groups), size=k, replace=False)
    return sorted(i for g in chosen for i in groups[g])


def _hash_ints(a: list[int]) -> str:
    return hashlib.sha256(json.dumps(a).encode()).hexdigest()[:16]


class EnsemblePool:
    def __init__(self, member_cls: type[MemberModel], kind: str, member_params: dict | None = None,
                 n_members: int = POOL_SIZE, base_seed: int = 0, subset_frac: float = 0.8):
        self.member_cls, self.kind, self.params = member_cls, kind, member_params or {}
        self.n_members, self.base_seed, self.subset_frac = n_members, base_seed, subset_frac
        self.members: list[MemberModel] = []

    def train(self, smiles: list[str], y, ckpt_dir: Path | str | None = None, resume: bool = True,
              on_member: Callable[[int, bool], None] | None = None) -> "EnsemblePool":
        """Trains all members. With ckpt_dir, each finished member is saved (member_XX.ckpt + .json sidecar);
        re-running skips members whose checkpoint exists and whose seed/subset hash match (resumable, deterministic)."""
        y = np.asarray(y)
        groups = scaffold_groups(smiles)
        ckpt = Path(ckpt_dir) if ckpt_dir else None
        if ckpt:
            ckpt.mkdir(parents=True, exist_ok=True)
        self.members = []
        for k in range(self.n_members):
            seed = member_seed(self.base_seed, k)
            idx = member_subset(groups, seed, self.subset_frac)
            side = {"member": k, "seed": seed, "subset_sha": _hash_ints(idx), "n_train": len(idx)}
            path = ckpt / f"member_{k:02d}.ckpt" if ckpt else None
            side_path = ckpt / f"member_{k:02d}.json" if ckpt else None
            reused = False
            if resume and path and path.exists() and side_path.exists() and json.loads(side_path.read_text()) == side:
                self.members.append(self.member_cls.load(path))
                reused = True
            else:
                m = self.member_cls(self.kind, **self.params).fit([smiles[i] for i in idx], y[idx], seed)
                if path:
                    m.save(path)
                    side_path.write_text(json.dumps(side))
                self.members.append(m)
            if on_member:
                on_member(k, reused)
        return self

    def predict_members(self, smiles: list[str], n: int | None = None) -> np.ndarray:
        """(n_members_used, len(smiles)). n=None uses the whole pool; else the first n members."""
        ms = self.members[: n or len(self.members)]
        return np.vstack([m.predict(list(smiles)) for m in ms])
