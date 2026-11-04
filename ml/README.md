# ml/: models, uncertainty, benchmarks, registry

Status (Wave 2): code and tests only. Nothing has been trained on real data. Tests use generated molecules
and labels. The shipped registry is empty and answers `NotTrained`.

Layout: `train/` (ensemble pool, Chemprop v2 adapter, training CLI, configs), `uncertainty/` (conformal,
Venn-Abers, decomposition, fragment attribution), `benchmarks/` (spec 12 protocol), `registry/` (file
registry, publish, manifest). `ml/data/` belongs to the data agent.

## Hard rules
- The training CLI refuses unless every dataset in its config is `cleared` in `ml/data/datasets.yaml`
  (`ml.data.manifest.assert_trainable`). Today all are `pending` or `excluded`, so it exits with code 2.
  `publish` re-checks the gate.
- Only scaffold or Butina splits (or time, when dates exist). Random-split results are never reported.
- Artifacts go to `ml/registry/artifacts/` (git-ignored). Never commit weights or data.

## Running training later on the remote Lambda machine (owner runs these; do not run in CI)
Commands only, after the licence gate has been cleared and the data cached and cleaned.

```bash
# 1. environment (Python >= 3.11)
git clone <repo> && cd metis_v0 && git checkout <release commit>
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r ml/requirements.txt chemprop      # chemprop v2 pulls torch; use the CUDA wheel on the GPU box

# 2. fetch + clean data into git-ignored data/ (explicit flag, after licence review)
python -m ml.data.loader Solubility_AqSolDB --download

# 3. train one endpoint (20 members, resumable: re-run the same command after an interruption)
python -m ml.train.train_endpoint --config ml/train/configs/solubility.yaml
#   outputs ml/registry/artifacts/<endpoint>/<version>/{members,calibration.json,train_data.json,meta.json}

# 4. benchmark and publish (python; writes ml/registry/manifest.json)
python - <<'PY'
import json
from ml.benchmarks.protocol import run_benchmark
from ml.registry.publish import publish
from ml.train.member import get_member_cls
from ml.train.bundle import ARTIFACTS_ROOT
meta = json.loads((ARTIFACTS_ROOT/"solubility"/"v1"/"meta.json").read_text())
rep = run_benchmark(meta["all_smiles"], meta["all_y"], meta["kind"], split_method=meta["split"],
                    member_cls=get_member_cls(meta["backend"]), member_params=meta["member_params"],
                    endpoint=meta["endpoint"], seed=meta["base_seed"], frac=(0.7, 0.15, 0.15),
                    noise_sd=None, out_path="ml/registry/artifacts/solubility/v1/benchmark.json")
print(publish(ARTIFACTS_ROOT/"solubility"/"v1", rep).model_dump_json(indent=1))
PY

# 5. copy ml/registry/artifacts/ and manifest.json to the serving host (rsync), then run tests
pytest tests/ml
```

Notes: set `member_params.accelerator` in the config (`cpu` or `auto`). Re-measure runtime on the
serving host; `time_estimate_seconds` uses the numbers measured at publish time on the machine that ran
`publish`. The 5x2cv test trains 10 extra small ensembles per benchmark, so budget for it.
