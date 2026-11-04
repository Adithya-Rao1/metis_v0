import { el, loadJSON, qs, renderChrome } from "./chrome.js";
import { SharedViewer, CONFORMER_LABEL } from "./viewer.js";
import { fmtInt, fmtVal } from "./format.js";

const cid = Number(qs("c", 3)), ep = qs("e", "herg");
const project = await loadJSON("fixtures/project.json");
const training = await loadJSON("fixtures/training.json");
const c = project.compounds.find((x) => x.id === cid) || project.compounds[0];
const info = project.endpoints[ep] || project.endpoints.herg;
const key = project.endpoints[ep] ? ep : "herg";
const p = c.predictions[key];
renderChrome({ current: "app.html", crumb: `Project / ${c.name} / ${info.label}` });
document.title = `${c.name}, ${info.label} | Metis`;
document.getElementById("h").textContent = `Uncertainty panel (${c.name}, ${info.label})`;

// value card
const card = document.getElementById("valcard");
if (p.kind === "classification") {
  const cal = p.calibration;
  card.append(el("span", { class: "t", text: `Calibrated probability (${info.unit})` }),
    el("span", { class: "n mono", "data-testid": "panel-value", text: p.probability.toFixed(2) }),
    el("span", { class: "legend", text: `Of ${fmtInt(cal.n_near)} calibration compounds scored near ${p.probability.toFixed(2)}, ${Math.round(cal.observed_fraction_near * 100)}% were measured ${info.pos[0]}s. Ensemble N = ${p.ensemble_n}, scaffold-split calibration set (n=${fmtInt(cal.set_n)}).` }));
} else {
  card.append(el("span", { class: "t", text: `Predicted value (${info.unit})` }),
    el("span", { class: "n mono", "data-testid": "panel-value", text: `${fmtVal(p.value, "regression")} [${fmtVal(p.interval_low, "regression")}, ${fmtVal(p.interval_high, "regression")}]` }),
    el("span", { class: "legend", text: `${Math.round(p.coverage * 100)}% interval from split conformal prediction. Ensemble N = ${p.ensemble_n}, scaffold-split calibration set (n=${fmtInt(p.calibration.set_n)}); median calibration width ${p.calibration.median_width}.` }));
}
document.getElementById("methodnote").textContent = `Method: ${p.method_note}`;

// decomposition with resampling ranges
const b = p.breakdown, r = b.ranges;
const rowsB = [["Model disagreement", b.ensemble_disagreement, r[0]], ["Distance from training data", b.similarity_to_training, r[1]], ["Assay noise", b.assay_noise, r[2]]];
document.getElementById("shares").replaceChildren(...rowsB.map(([label, v, rg], i) => el("div", { "data-share": i },
  `${label} `, el("span", { class: "mono", text: `${Math.round(v * 100)}%` }), " ",
  el("span", { class: "legend", text: `range ${Math.round(rg[0] * 100)} to ${Math.round(rg[1] * 100)} over resamples` }),
  el("div", { class: "bar" }, el("i", { style: `width:${v * 100}%` })))));

// 2D + 3D with same fragment highlighted (atom-index map shared by RDKit depiction and conformer)
const f = c.fragment;
document.getElementById("d2").append(el("img", { class: "mol big", src: c.svg_highlight, alt: `2D structure of ${c.name} with the ${f.name} fragment highlighted` }));
document.getElementById("d2cap").replaceChildren(el("span", { class: "hl", text: `${f.name}: ${Math.round(p.fragment_share_of_disagreement * 100)}%` }));
const viewer = new SharedViewer(document.getElementById("viewer"));
const cap = document.getElementById("viewer-cap");
function showMain() { viewer.showMolblock(c.conformer.molblock, f.atom_indices); cap.textContent = `${c.name}, highlighted fragment: ${f.name}. ${CONFORMER_LABEL}.`; }
showMain();
document.getElementById("fragtext").replaceChildren(el("span", { class: "hl", text: f.name }),
  ` accounts for ${Math.round(p.fragment_share_of_disagreement * 100)}% of model disagreement. It appears in ${fmtInt(f.training_count)} of ${fmtInt(f.training_total)} training compounds.`);

// nearest training compounds: 2D thumbnail, 3D on click (same viewer)
const nb = document.getElementById("nearest");
c.nearest.forEach((n, i) => {
  const t = training.compounds.find((x) => x.id === n.train_id);
  let meas = "";
  if (p.kind === "classification") meas = key === "herg" ? (n.measured_herg_positive ? "blocker" : "non-blocker") : "non-mutagenic";
  else meas = "see model page";
  nb.append(el("div", { class: "molcell", "data-nearest": i },
    el("img", { class: "mol", src: t.svg, alt: `2D structure of training compound ${t.id}` }),
    el("span", { class: "mono", text: `Tanimoto ${n.tanimoto.toFixed(2)}, measured (fixture label): ${meas}` }),
    el("button", { class: "btn small", type: "button", "aria-label": `Show 3D structure of nearest training compound ${i + 1}`, text: "3D",
      onclick: () => { viewer.showMolblock(t.conformer_molblock); cap.textContent = `Training compound ${t.id} (stand-in). ${CONFORMER_LABEL}.`; } })));
});
nb.append(el("button", { class: "btn small", style: "align-self:flex-start", type: "button", text: "Back to this compound", onclick: showMain }));

document.getElementById("ens2").value = String(p.ensemble_n);
document.getElementById("rerun").addEventListener("click", () => {
  document.getElementById("rerun-msg").textContent = `Fixture mode: re-running at N = ${document.getElementById("ens2").value} needs the backend. Nothing was run.`;
});
for (const id of ["datalink", "modellink", "limlink", "howlink"]) {
  const a = document.getElementById(id);
  a.href = `model.html?e=${key}` + (a.getAttribute("href").includes("#") ? "#" + a.getAttribute("href").split("#")[1] : "");
}
document.getElementById("datalink").textContent = info.dataset;
