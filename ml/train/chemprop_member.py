"""Chemprop v2 ensemble-member adapter (D-MPNN). Imports chemprop/torch lazily so the rest of Metis
works without them. NOT exercised on real data in Wave 2 (owner instruction); see tests for what is checked."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ml.train.member import MemberModel

DEFAULTS = {
    "max_epochs": 50,
    "batch_size": 64,
    "hidden_dim": 300,
    "depth": 3,
    "dropout": 0.0,
    "accelerator": "cpu",
}


def _imports():
    import torch
    from lightning import pytorch as pl
    from chemprop import data, featurizers, models, nn

    return torch, pl, data, featurizers, models, nn


class ChempropMember(MemberModel):
    backend = "chemprop"

    def _hp(self) -> dict:
        return {**DEFAULTS, **self.params}

    def _build(self, hp: dict):
        _, _, _, _, models, nn = _imports()
        mp = nn.BondMessagePassing(d_h=hp["hidden_dim"], depth=hp["depth"], dropout=hp["dropout"])
        agg = nn.MeanAggregation()
        ffn_cls = nn.RegressionFFN if self.kind == "regression" else nn.BinaryClassificationFFN
        ffn = ffn_cls(input_dim=mp.output_dim, hidden_dim=hp["hidden_dim"], dropout=hp["dropout"])
        return models.MPNN(mp, agg, ffn, batch_norm=True)

    def _loader(self, smiles, y, shuffle: bool, seed: int):
        _, _, data, featurizers, _, _ = _imports()
        feat = featurizers.SimpleMoleculeMolGraphFeaturizer()
        pts = [
            data.MoleculeDatapoint.from_smi(s, None if y is None else [float(v)])
            for s, v in zip(smiles, y if y is not None else [None] * len(smiles))
        ]
        ds = data.MoleculeDataset(pts, feat)
        return data.build_dataloader(
            ds, batch_size=self._hp()["batch_size"], num_workers=0, shuffle=shuffle, seed=seed
        )

    def _trainer(self, max_epochs: int):
        _, pl, *_ = _imports()
        return pl.Trainer(
            logger=False,
            enable_checkpointing=False,
            enable_progress_bar=False,
            accelerator=self._hp()["accelerator"],
            devices=1,
            max_epochs=max_epochs,
            enable_model_summary=False,
            deterministic=True,
        )

    def fit(self, smiles, y, seed):
        _, pl, *_ = _imports()
        pl.seed_everything(seed, workers=True)
        hp = self._hp()
        y = np.asarray(y, float)
        self.y_mean, self.y_std = (
            (float(y.mean()), float(y.std() or 1.0)) if self.kind == "regression" else (0.0, 1.0)
        )
        self.model = self._build(hp)
        self._trainer(hp["max_epochs"]).fit(
            self.model, self._loader(smiles, (y - self.y_mean) / self.y_std, True, seed)
        )
        return self

    def predict(self, smiles):
        torch, *_ = _imports()
        out = self._trainer(1).predict(self.model, self._loader(smiles, None, False, 0))
        arr = torch.cat(out).reshape(-1).numpy().astype(float)
        return arr * self.y_std + self.y_mean if self.kind == "regression" else arr

    def save(self, path):
        torch, *_ = _imports()
        path = Path(path)
        torch.save(self.model.state_dict(), path)
        path.with_suffix(".json").write_text(
            json.dumps({"kind": self.kind, "params": self.params, "y_mean": self.y_mean, "y_std": self.y_std})
        )

    @classmethod
    def load(cls, path):
        torch, *_ = _imports()
        path = Path(path)
        meta = json.loads(path.with_suffix(".json").read_text())
        m = cls(meta["kind"], **meta["params"])
        m.y_mean, m.y_std = meta["y_mean"], meta["y_std"]
        m.model = m._build(m._hp())
        m.model.load_state_dict(torch.load(path, weights_only=True))
        return m
