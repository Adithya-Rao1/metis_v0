import { el, loadJSON, qs, renderChrome } from "./chrome.js";
import { SharedViewer, CONFORMER_LABEL } from "./viewer.js";

const data = await loadJSON("fixtures/models.json");
const training = await loadJSON("fixtures/training.json");
const reqId = qs("e", "herg");
const id = Object.hasOwn(data.models, reqId) ? reqId : "herg";
const m = data.models[id];
renderChrome({ current: "model.html", crumb: `Models / ${m.label}` });
document.getElementById("h").textContent = `Model and data page (${m.label})`;
document.title = `${m.label} model | Metis`;
document.getElementById("picker").replaceChildren(...Object.values(data.models).map((x) =>
  el("a", { class: "chip", href: `model.html?e=${x.id}`, ...(x.id === id ? { "aria-current": "page" } : {}), text: x.label })));

// G-01: calibration / coverage / noise-ceiling / significance blocks. FIXTURE layout content (zeros), not results.
function calibrationBlocks(m) {
  const reg = m.kind !== "classification";
  const tbl = (cap, heads, rows) => el("div", { class: "tablewrap" }, el("table", {}, el("caption", { text: cap }),
    el("thead", {}, el("tr", {}, heads.map((x) => el("th", { scope: "col", text: x })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", { class: "mono", text: c })))))));
  const bins = ["0.0 to 0.2", "0.2 to 0.4", "0.4 to 0.6", "0.6 to 0.8", "0.8 to 1.0"];
  return [
    el("h2", { id: "calibration", text: "Calibration (FIXTURE content, placeholders)" }),
    el("p", { class: "legend", "data-testid": "fixture-calibration", text: "FIXTURE: every number below is a zero placeholder showing the layout. No calibration has been computed." }),
    tbl(reg ? "Reliability diagram data: nominal vs. observed interval coverage, held-out scaffold split." : "Reliability diagram data: mean predicted probability vs. observed fraction positive per bin, held-out scaffold split.",
      reg ? ["Nominal coverage", "Observed coverage", "n"] : ["Predicted probability bin", "Observed fraction", "n"],
      reg ? ["50%", "80%", "90%", "95%"].map((x) => [x, "0.00 (placeholder)", "0"]) : bins.map((x) => [x, "0.00 (placeholder)", "0"])),
    el("div", { class: "kv" }, el("span", { text: "Expected calibration error (ECE)" }), el("span", { class: "mono", "data-testid": "ece", text: "0.00 (placeholder)" })),
    tbl("Interval coverage by similarity bin (similarity of the query to its nearest training compounds). Sample sizes are never hidden.",
      ["Similarity bin (Tanimoto to nearest training compound)", "Interval coverage", "Compounds in bin (n)"],
      [["0.0 to 0.3 (far from training data)", "0.0% (placeholder)", "0"], ["0.3 to 0.5", "0.0% (placeholder)", "0"], ["0.5 to 0.7", "0.0% (placeholder)", "0"], ["0.7 to 1.0 (near training data)", "0.0% (placeholder)", "0"]]),
    reg ? el("div", { class: "kv" }, el("span", { text: "Noise ceiling (best achievable error given assay noise)" }), el("span", { class: "mono", "data-testid": "noise-ceiling", text: "R2 ceiling 0.00, RMSE floor 0.00 (placeholder)" }))
        : el("div", { class: "kv" }, el("span", { text: "Noise ceiling" }), el("span", { class: "mono", "data-testid": "noise-ceiling", text: "Not defined for a classification label; label agreement between sources is reported instead (placeholder)" })),
    tbl("Significance of the difference from the baseline: 5x2cv paired t-test over scaffold splits.",
      ["Comparison", "t statistic", "p value", "Result"],
      [["Metis ensemble vs. baseline (5x2cv paired t-test)", "0.00", "n/a", "placeholder: test not yet run"]]),
  ];
}

const rep = document.getElementById("report");
rep.append(
  el("p", { class: "draft-note", role: "note", "data-testid": "fixture-model-note", text: data.note }),
  el("div", { class: "rgrid" },
    ...m.datasets.map((d) => el("div", { class: "rcard", "data-dataset": d.name }, el("span", { class: "t", text: "Training data" }), el("span", { class: "n", text: d.name }),
      el("span", { class: "legend", text: `${d.n}. ${d.source}.` }), el("span", { class: "legend", text: `License: ${d.license}.` }))),
    el("div", { class: "rcard" }, el("span", { class: "t", text: "Split used for evaluation" }), el("span", { class: "n", text: "Scaffold" }), el("span", { class: "legend", text: `${m.split} Calibration set n = ${m.calibration_n}.` })),
    el("div", { class: "rcard" }, el("span", { class: "t", text: "Interval coverage, held out" }), el("span", { class: "n", text: String(m.heldout_coverage) }), el("span", { class: "legend", text: `Target ${Math.round(m.target_coverage * 100)}% (placeholder).` }))),
  el("h2", { id: "how-computed", text: "Benchmark against baselines (scaffold split; metric ranges shown in full on release)" }),
  el("div", { class: "tablewrap" }, el("table", {}, el("caption", { text: "Baseline sits on the first row. If the model does not beat it, this page says so." }),
    el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Model" }), m.metrics.map((x) => el("th", { scope: "col", text: x })), el("th", { scope: "col", text: "vs. baseline" }))),
    el("tbody", {}, m.benchmark.map((r) => el("tr", {}, el("td", { text: r.model }), r.values.map((v) => el("td", { class: "mono", text: v.toFixed(2) })), el("td", { text: r.note })))))),
  el("div", { class: "tablewrap" }, el("table", {}, el("caption", { text: "Coverage and runtime at each ensemble size offered (placeholders)." }),
    el("thead", {}, el("tr", {}, ["Ensemble size N", "Interval coverage (held out)", "Time per compound"].map((x) => el("th", { scope: "col", text: x })))),
    el("tbody", {}, m.ensemble_table.map((r) => el("tr", {}, el("td", { text: String(r.n) }), el("td", { class: "mono", text: r.coverage }), el("td", { class: "mono", text: r.runtime })))))),
  ...calibrationBlocks(m),
  el("div", { class: "note-box", id: "limitations" }, el("b", { text: "Known limitations of this dataset" }), " (written per dataset, linked to source): ", m.limitations, el("br"), m.attribution_check),
  el("p", { class: "legend", text: "Dataset licenses are shown as recorded. Where the status is pending, no claim about reuse is made." }),
  el("h2", { text: "Training examples" }),
  el("div", { class: "viewer-dock flush" },
    el("div", { class: "viewer-wrap" }, el("div", { class: "viewer", id: "viewer", "aria-label": "3D viewer" }), el("p", { class: "caption", id: "viewer-cap", text: "Press 3D on an example." })),
    el("div", { id: "examples", class: "checks" })));

const viewer = new SharedViewer(document.getElementById("viewer"));
const ex = document.getElementById("examples");
m.examples.forEach((tid) => {
  const t = training.compounds.find((x) => x.id === tid);
  ex.append(el("div", { class: "molcell" }, el("img", { class: "mol", src: t.svg, alt: `2D structure of example ${t.id}` }),
    el("span", { class: "mono", text: t.id }),
    el("button", { class: "btn small", type: "button", "aria-label": `Show 3D structure of example ${t.id}`, text: "3D",
      onclick: () => { viewer.showMolblock(t.conformer_molblock); document.getElementById("viewer-cap").textContent = `${t.id} (stand-in example). ${CONFORMER_LABEL}.`; } })));
});
