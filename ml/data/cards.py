"""Data card generator."""
from __future__ import annotations

from pathlib import Path

from ml.data.manifest import get

CARD_DIR = Path(__file__).with_name("cards")

LIMITATIONS = {
    "regression": "Values come from heterogeneous assays and labs; assay noise is not modeled in this card. Applicability is limited to chemistry similar to the training set.",
    "classification": "Binary labels depend on assay-specific thresholds; class balance may differ from real-world prevalence. Applicability is limited to chemistry similar to the training set.",
}


def render_card(dataset_id: str, n_compounds: int | None, split_method: str | None, split_sizes: tuple[int, int, int] | None = None) -> str:
    e = get(dataset_id)
    size = "not yet downloaded or cleaned" if n_compounds is None else f"{n_compounds} compounds after cleaning"
    if split_method is None:
        sp = "not yet split"
    else:
        sp = split_method + (f" split (train/cal/test = {split_sizes[0]}/{split_sizes[1]}/{split_sizes[2]})" if split_sizes else " split")
        if split_method == "random":
            raise ValueError("random splits are not allowed")
    gate = "NOT cleared for training (gate G1: written license confirmation required)." if e.license_status != "cleared" else f"Cleared. Checked {e.date_checked} by {e.confirmed_by}."
    if e.license_status == "excluded":
        gate = "EXCLUDED. Must not be loaded, trained on, or redistributed."
    credit = ("Distributed through the Therapeutics Data Commons (TDC; Huang et al., NeurIPS Datasets and Benchmarks 2021). Please credit TDC and the original authors listed above."
              if "tdcommons" in e.source_url else "Not distributed through TDC. Credit the original authors.")
    return f"""# Data card: {e.name}

- Endpoint: {e.endpoint}
- Kind: {e.kind}
- Units: {e.units}
- Size: {size}
- Split: {sp}
- License as stated: {e.license_as_stated}
- License status: {e.license_status}. {gate}
- Source: {e.source_url}

## Limitations
{LIMITATIONS[e.kind]}

## Credit
{e.notes}
{credit}"""


def write_card(dataset_id: str, n_compounds=None, split_method=None, split_sizes=None, out_dir: Path | str = CARD_DIR) -> Path:
    p = Path(out_dir) / f"{dataset_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_card(dataset_id, n_compounds, split_method, split_sizes))
    return p
