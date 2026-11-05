"""CSV, SDF and PDF writers. Every export carries the disclaimer (docs/legal/DISCLAIMER.md)."""

from __future__ import annotations

import csv
import io
import pathlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from backend.schemas.contracts import Prediction

DISCLAIMER_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "legal" / "DISCLAIMER.md"
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@lru_cache
def disclaimer() -> tuple[str, str]:
    """(heading, body) from the legal document; raises if it is missing (an export must never ship without it)."""
    raw = DISCLAIMER_PATH.read_text(encoding="utf-8").strip()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    heading = lines[0].lstrip("# ").strip()
    body = " ".join(ln.strip() for ln in lines[1:])
    if not body:
        raise RuntimeError("disclaimer text is empty")
    return heading, body


@dataclass
class ExportCompound:
    index: int
    label: Optional[str]
    canonical_smiles: str
    predictions: dict[str, Prediction] = field(default_factory=dict)
    measured: dict[str, list[float]] = field(default_factory=dict)
    molblock: Optional[str] = None
    conformer_method: Optional[str] = None


def _cell(v: object) -> object:
    """Neutralize spreadsheet formula injection."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(project_name: str, compounds: list[ExportCompound], endpoints: list[str]) -> str:
    head, body = disclaimer()
    out = io.StringIO()
    out.write(
        f"# {head}\n# {body}\n# Values labelled predicted are computational estimates with stated uncertainty; measured values are user-entered.\n"
    )
    w = csv.writer(out, lineterminator="\n")
    cols = ["compound", "label", "canonical_smiles"]
    for e in endpoints:
        cols += [
            f"{e}_predicted",
            f"{e}_interval_low",
            f"{e}_interval_high",
            f"{e}_probability",
            f"{e}_ensemble_n",
            f"{e}_coverage",
            f"{e}_sample_size",
            f"{e}_measured",
        ]
    w.writerow(cols)
    for c in compounds:
        row: list[object] = [c.index, _cell(c.label or ""), _cell(c.canonical_smiles)]
        for e in endpoints:
            p = c.predictions.get(e)
            if p is None:
                row += [""] * 7
            else:
                row += [
                    p.value,
                    p.interval_low if p.interval_low is not None else "",
                    p.interval_high if p.interval_high is not None else "",
                    p.probability if p.probability is not None else "",
                    p.ensemble_n,
                    p.coverage,
                    p.sample_size,
                ]
            row.append(";".join(f"{m:g}" for m in c.measured.get(e, [])))
        w.writerow(row)
    return out.getvalue()


def _sdf_value(v: str) -> str:
    v = _CTRL.sub(" ", v).replace("$$$$", "$ $ $ $")
    return "\n".join((" " + ln if ln.startswith(">") else ln) for ln in v.split("\n") if ln.strip())


def to_sdf(project_name: str, compounds: list[ExportCompound]) -> str:
    head, body = disclaimer()
    recs = []
    for c in compounds:
        mb = (c.molblock or "").replace("\r\n", "\n").rstrip("\n").split("\n")
        if len(mb) < 4:
            raise ValueError("missing molblock")
        mb[0] = f"Metis compound {c.index}; computed structure, see DISCLAIMER field"
        method = c.conformer_method or "none: 2D depiction coordinates only (3D conformer generation failed)"
        three_d = c.conformer_method is not None
        fields = {
            "canonical_smiles": c.canonical_smiles,
            "label": c.label or "",
            "conformer_method": method,
            "conformer_label": "computed (not experimental)"
            if three_d
            else "computed 2D depiction (no 3D conformer)",
            "DISCLAIMER": f"{head}. {body}",
        }
        for e, p in sorted(c.predictions.items()):
            if p.kind == "regression":
                txt = f"predicted {p.value:g} (interval {p.interval_low:g} to {p.interval_high:g}, coverage {p.coverage:g})"
            else:
                txt = f"predicted probability {p.probability:g} (calibrated)"
            fields[f"pred_{e}"] = f"{txt}; ensemble N={p.ensemble_n}; training sample size {p.sample_size}"
        for e, vals in sorted(c.measured.items()):
            fields[f"measured_{e}"] = ";".join(f"{m:g}" for m in vals)
        props = "".join(f">  <{k}>\n{_sdf_value(v) or '-'}\n\n" for k, v in fields.items())
        recs.append("\n".join(mb) + "\n" + props + "$$$$\n")
    return "".join(recs)


def _latin(s: str) -> str:
    return s.encode("latin-1", "replace").decode("latin-1")


def to_pdf(project_name: str, compounds: list[ExportCompound], endpoints: list[str]) -> bytes:
    from fpdf import FPDF

    head, body = disclaimer()
    footer_text = _latin(f"{head}. {body}")

    class Doc(FPDF):
        def footer(self) -> None:
            self.set_y(-26)
            self.set_font("Helvetica", size=7)
            self.multi_cell(0, 3.2, footer_text, wrapmode="CHAR")

    pdf = Doc(format="A4")
    pdf.set_compression(False)
    pdf.set_auto_page_break(auto=True, margin=30)
    pdf.set_title("Metis report")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(
        0, 7, _latin(f"Metis report: {project_name}"), wrapmode="CHAR", new_x="LMARGIN", new_y="NEXT"
    )
    pdf.set_font("Helvetica", size=8)
    pdf.multi_cell(
        0,
        4,
        "Predicted values are computational estimates with stated uncertainty. Measured values are user-entered.",
        wrapmode="CHAR",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)
    for c in compounds:
        pdf.set_font("Helvetica", "B", 10)
        pdf.multi_cell(
            0,
            5,
            _latin(f"Compound {c.index}" + (f": {c.label}" if c.label else "")),
            wrapmode="CHAR",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_font("Courier", size=8)
        pdf.multi_cell(
            0, 4, _latin(f"SMILES {c.canonical_smiles}"), wrapmode="CHAR", new_x="LMARGIN", new_y="NEXT"
        )
        pdf.set_font("Helvetica", size=8)
        for e in endpoints:
            p = c.predictions.get(e)
            if p is None:
                continue
            if p.kind == "regression":
                line = f"{e}: predicted {p.value:g} (interval {p.interval_low:g} to {p.interval_high:g}, coverage {p.coverage:g})"
            else:
                line = f"{e}: predicted probability {p.probability:g} (calibrated)"
            line += f"; N={p.ensemble_n}; sample size {p.sample_size}"
            if c.measured.get(e):
                line += "; measured " + ", ".join(f"{m:g}" for m in c.measured[e])
            pdf.multi_cell(0, 4, _latin(line), wrapmode="CHAR", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    return bytes(pdf.output())
