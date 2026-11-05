// Uncertainty panel for one prediction: GET /predictions/{id}/uncertainty (PredictionDetail) plus the compound's depiction and conformer.
import { el, qs, qsId, renderChrome, showError, showLoading, stateBox } from "./chrome.js";
import { authed, depictionUrl, describeError, get, post, requireSession, smilesPicture } from "./api.js";
import { SharedViewer, CONFORMER_LABEL } from "./viewer.js";
import { fmtInt, fmtVal } from "./format.js";

const $ = (id) => document.getElementById(id);
const user = await requireSession();
const pid = qsId("p"), cid = qsId("c"), ep = qs("e", "") || "";
renderChrome({ current: "app.html", crumb: "Projects / Uncertainty", user });
const page = $("page-state");
const ENSEMBLE_SIZES = [3, 5, 10, 20], TIER_MAX_N = { free: 10, edu: 20, paid: 20 };

if (!pid || !cid || !/^[a-z0-9_]{1,64}$/.test(ep)) {
  page.replaceChildren(stateBox("empty", "Open this panel from a value in the project table."));
  page.append(el("p", {}, el("a", { class: "btn", href: "app.html", text: "Go to projects" })));
} else load();

async function load() {
  showLoading(page, "Loading the uncertainty panel");
  $("back").href = `app.html?p=${pid}`;
  let project, detail, card = null, comp, entry;
  try {
    project = await authed(() => get(`/projects/${pid}`));
    comp = project.compounds.find((c) => c.id === cid);
    entry = comp && comp.predictions.find((x) => x.prediction.endpoint === ep);
    if (!entry) { page.replaceChildren(stateBox("empty", "No prediction exists for this compound and property yet. Run predictions from the project.")); return; }
    detail = await authed(() => get(`/predictions/${entry.id}/uncertainty`));
  } catch (e) {
    return showError(page, e, load);
  }
  try { card = (await get(`/models/${encodeURIComponent(ep)}`)); } catch (e) { card = null; }
  page.replaceChildren();
  $("panel-frame").hidden = false;
  const p = detail.prediction;
  const cname = comp.label || `Compound ${project.compounds.indexOf(comp) + 1}`;
  $("h").textContent = `Uncertainty panel (${cname}, ${ep})`;
  document.title = `${cname}, ${ep} | Metis`;
  document.querySelector(".crumb").textContent = `Projects / ${project.name} / ${cname} / ${ep}`;

  // ---- value card: every field the contract requires ----
  const units = card?.units ? ` (${card.units})` : "";
  const card0 = $("valcard");
  if (p.kind === "classification") {
    card0.append(el("span", { class: "t", text: `Calibrated probability${units}` }),
      el("span", { class: "n mono", "data-testid": "panel-value", text: p.probability.toFixed(2) }),
      el("span", { class: "legend", "data-testid": "panel-meta", text: `Calibrated probability. Ensemble N = ${p.ensemble_n}. Calibration set size n = ${fmtInt(p.sample_size)}. Empirical coverage ${Math.round(p.coverage * 100)}%.` }));
  } else {
    card0.append(el("span", { class: "t", text: `Predicted value${units}` }),
      el("span", { class: "n mono", "data-testid": "panel-value", text: `${fmtVal(p.value, "regression")} [${fmtVal(p.interval_low, "regression")}, ${fmtVal(p.interval_high, "regression")}]` }),
      el("span", { class: "legend", "data-testid": "panel-meta", text: `${Math.round(p.coverage * 100)}% interval. Ensemble N = ${p.ensemble_n}. Calibration set size n = ${fmtInt(p.sample_size)}.` }));
  }
  $("methodnote").textContent = `Method: ${p.method_note}`;
  $("runtime").textContent = `This detail view took ${detail.runtime_seconds.toFixed(2)} s to compute.`;
  $("modellink").href = `model.html?e=${encodeURIComponent(ep)}`;

  const b = p.breakdown;
  const rowsB = [["Model disagreement", b.ensemble_disagreement], ["Distance from training data", b.similarity_to_training], ["Assay noise", b.assay_noise]];
  $("shares").replaceChildren(...rowsB.map(([label, v], i) => el("div", { "data-share": i },
    `${label} `, el("span", { class: "mono", text: `${Math.round(v * 100)}%` }),
    el("div", { class: "bar" }, el("i", { pct: v * 100 })))));

  // ---- 2D + 3D with the same fragment highlighted ----
  const viewer = new SharedViewer($("viewer"));
  let molblock = null;
  const cap = $("viewer-cap");
  async function ensureConformer() {
    if (molblock) return molblock;
    try {
      const conf = await authed(() => get(`/projects/${pid}/compounds/${cid}/conformer`));
      molblock = conf.molblock; return molblock;
    } catch (e) { cap.textContent = `3D view unavailable: ${describeError(e).message}`; return null; }
  }
  async function show(frag) {
    const hl = frag ? frag.atom_indices : [];
    $("d2").replaceChildren(el("img", { class: "mol big", src: depictionUrl(pid, cid, hl), alt: `2D structure of ${cname}${frag ? " with the selected fragment highlighted" : ""}` }));
    $("d2cap").textContent = frag ? `Highlighted fragment: ${Math.round(frag.share * 100)}% of model disagreement.` : cname;
    const mb = await ensureConformer();
    if (mb) { viewer.showMolblock(mb, hl); cap.textContent = `${cname}${frag ? ", highlighted fragment" : ""}. ${CONFORMER_LABEL}.`; }
  }
  const fr = $("fragments");
  if (!detail.fragments.length) fr.replaceChildren(stateBox("empty", "No fragment attribution was returned for this prediction."));
  else {
    fr.replaceChildren(...detail.fragments.map((f, i) => el("div", { class: "inline", "data-fragment": i },
      el("button", { class: "btn small", type: "button", "aria-label": `Highlight fragment ${i + 1}`, text: "Highlight", onclick: () => show(f) }),
      el("span", { class: "hl", text: `${Math.round(f.share * 100)}%` }),
      el("span", { class: "legend", text: `range ${Math.round(f.share_low * 100)} to ${Math.round(f.share_high * 100)}% | atoms ${f.atom_indices.join(", ")} | in ${Math.round(f.train_frequency * 100)}% of training compounds` }))));
  }
  const nb = $("nearest");
  if (!detail.nearest_training.length) nb.replaceChildren(stateBox("empty", "No nearest training compounds were returned."));
  else nb.replaceChildren(...detail.nearest_training.map((n, i) => el("div", { "data-nearest": i, class: "legend mono",
    text: `Tanimoto ${Number(n.similarity).toFixed(2)} | measured label ${n.label} | ${n.smiles}` })));
  // Pictures of the nearest training compounds, only if the server can draw an arbitrary SMILES (otherwise the text above stays as it is).
  detail.nearest_training.forEach(async (n, i) => {
    let url = null;
    try { url = await smilesPicture(n.smiles); } catch (e) { return; }
    const host = nb.querySelector(`[data-nearest="${i}"]`);
    if (url && host) host.prepend(el("img", { class: "mol", src: url, alt: `2D structure of nearest training compound ${i + 1}` }));
  });
  await show(null);

  // ---- re-run ----
  const cap2 = TIER_MAX_N[user.tier] ?? 10;
  const ens2 = $("ens2");
  ens2.replaceChildren(...ENSEMBLE_SIZES.map((n) => {
    const calibrated = !card || String(n) in card.coverage_by_n;
    const ok = n <= cap2 && calibrated;
    return el("option", { value: n, disabled: !ok, selected: n === p.ensemble_n, text: ok ? String(n) : `${n} (${n > cap2 ? "tier cap" : "not calibrated"})` });
  }));
  $("rerun").addEventListener("click", async () => {
    const msg = $("rerun-msg");
    msg.textContent = "Starting...";
    try {
      const j = await authed(() => post(`/projects/${pid}/predict`, { endpoints: [ep], ensemble_n: Number(ens2.value) }));
      msg.textContent = `Started: about ${Math.round(j.estimated_seconds)} s for ${j.total} predictions. The project table shows the new values when it finishes.`;
    } catch (e) { msg.textContent = describeError(e).message; }
  });
}
