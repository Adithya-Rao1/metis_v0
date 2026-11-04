import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from ml_synth import classification_labels, make_molecules, regression_labels  # noqa: E402

CLEARED_YAML = """datasets:
  - id: SynthReg
    name: Synthetic regression set
    source_url: https://example.org/synth-reg
    endpoint: synthetic logP
    kind: regression
    units: log units
    license_as_stated: "CC BY 4.0"
    license_status: cleared
    date_checked: "2026-01-02"
    confirmed_by: "Test Owner"
    notes: "generated for tests"
  - id: SynthCls
    name: Synthetic classification set
    source_url: https://example.org/synth-cls
    endpoint: synthetic class
    kind: classification
    units: "binary"
    license_as_stated: "CC0"
    license_status: cleared
    date_checked: "2026-01-02"
    confirmed_by: "Test Owner"
    notes: "generated for tests"
"""


@pytest.fixture(scope="session")
def mols():
    return make_molecules(260, seed=1)


@pytest.fixture(scope="session")
def yreg(mols):
    return regression_labels(mols)


@pytest.fixture(scope="session")
def ycls(mols):
    return classification_labels(mols)


@pytest.fixture(scope="session")
def cleared_manifest(tmp_path_factory):
    p = tmp_path_factory.mktemp("manifest") / "datasets.yaml"
    p.write_text(CLEARED_YAML)
    return p
