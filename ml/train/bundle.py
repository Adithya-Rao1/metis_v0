"""On-disk model bundle: members/, calibration.json, train_data.json, meta.json. Lives under a git-ignored
artifacts dir. Nothing here is committed."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ml.train.ensemble import EnsemblePool
from ml.train.member import get_member_cls
from ml.uncertainty.conformal import ConformalByN, IsotonicRecalibrator, N_VALUES, VennAbers
from ml.uncertainty.similarity import SimilarityErrorModel, SimilarityIndex

ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent / "registry" / "artifacts"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def weights_sha256(members_dir: Path) -> str:
    """sha256 over (filename, file sha256) of every file in members/, sorted by name."""
    h = hashlib.sha256()
    for p in sorted(Path(members_dir).iterdir()):
        if p.is_file():
            h.update(p.name.encode() + b"\0" + sha256_file(p).encode() + b"\n")
    return h.hexdigest()


def code_commit() -> str:
    """HEAD commit read from .git directly (no subprocess). 'unknown' if not in a git checkout."""
    try:
        d = Path(__file__).resolve().parent
        while not (d / ".git").exists():
            if d.parent == d:
                return "unknown"
            d = d.parent
        g = d / ".git"
        if g.is_file():  # worktree: "gitdir: <path>"
            g = Path(g.read_text().split("gitdir:", 1)[1].strip())
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(" ", 1)[1]
        if (g / ref).exists():
            return (g / ref).read_text().strip()
        common = g / "commondir"
        roots = [g.parent.parent] if g.parent.name == "worktrees" else []
        if common.exists():
            roots.insert(0, (g / common.read_text().strip()).resolve())
        for r in roots:
            if (r / ref).exists():
                return (r / ref).read_text().strip()
            pr = r / "packed-refs"
            if pr.exists():
                for line in pr.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0]
        return "unknown"
    except Exception:
        return "unknown"


def dataset_sha256(smiles: list[str], y) -> str:
    h = hashlib.sha256()
    for s, v in sorted(zip(smiles, (float(a) for a in y))):
        h.update(f"{s}\t{v!r}\n".encode())
    return h.hexdigest()


class ClassCalibrators:
    """Per-N recalibration for classification: isotonic and Venn-Abers fitted on calibration-set scores."""

    def __init__(self, per_n: dict[int, dict]):
        self.per_n = per_n  # N -> {"scores": [...], "labels": [...]}

    @classmethod
    def fit(cls, member_probs, y, ns=N_VALUES) -> "ClassCalibrators":
        mp = np.asarray(member_probs, float)
        return cls({N: {"scores": mp[:N].mean(0).tolist(), "labels": [float(v) for v in y]} for N in ns})

    def venn_abers(self, N: int) -> VennAbers:
        d = self.per_n[N]
        return VennAbers().fit(d["scores"], d["labels"])

    def isotonic(self, N: int) -> IsotonicRecalibrator:
        d = self.per_n[N]
        return IsotonicRecalibrator().fit(d["scores"], d["labels"])

    def to_dict(self):
        return {str(N): v for N, v in self.per_n.items()}

    @classmethod
    def from_dict(cls, d):
        return cls({int(N): v for N, v in d.items()})


@dataclass
class Bundle:
    root: Path
    meta: dict
    pool: EnsemblePool
    conformal: ConformalByN | None          # regression
    class_cal: ClassCalibrators | None      # classification
    err_models: dict[int, SimilarityErrorModel]  # per N, expected abs error vs similarity (both kinds)
    index: SimilarityIndex
    train_smiles: list[str]
    train_y: list[float]

    @property
    def kind(self) -> str:
        return self.meta["kind"]


def save_calibration(root: Path, conformal, class_cal, err_models, extra: dict) -> None:
    d = {"conformal": conformal.to_dict() if conformal else None,
         "class_cal": class_cal.to_dict() if class_cal else None,
         "err_models": {str(N): m.to_dict() for N, m in err_models.items()}, **extra}
    (root / "calibration.json").write_text(json.dumps(d))


def load_bundle(root: Path | str, expected_weights_sha256: str | None = None) -> Bundle:
    """Loads a bundle. If expected_weights_sha256 is given the members are hash-verified BEFORE any
    deserialisation (sklearn members are joblib files)."""
    root = Path(root)
    meta = json.loads((root / "meta.json").read_text())
    actual = weights_sha256(root / "members")
    if expected_weights_sha256 is not None and actual != expected_weights_sha256:
        raise ValueError("weights hash mismatch: refusing to load members")
    cal = json.loads((root / "calibration.json").read_text())
    td = json.loads((root / "train_data.json").read_text())
    cls = get_member_cls(meta["backend"])
    pool = EnsemblePool(cls, meta["kind"], meta.get("member_params", {}), meta["n_members"], meta["base_seed"])
    pool.members = [cls.load(root / "members" / f"member_{k:02d}.ckpt") for k in range(meta["n_members"])]
    return Bundle(root, meta, pool,
                  ConformalByN.from_dict(cal["conformal"]) if cal["conformal"] else None,
                  ClassCalibrators.from_dict(cal["class_cal"]) if cal["class_cal"] else None,
                  {int(N): SimilarityErrorModel.from_dict(v) for N, v in cal["err_models"].items()},
                  SimilarityIndex(td["smiles"]), td["smiles"], td["y"])
