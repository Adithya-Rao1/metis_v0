import { el, loadJSON, renderChrome } from "./chrome.js";
import { SharedViewer, CONFORMER_LABEL } from "./viewer.js";
import { scoreAll, FORMULA } from "./ranking.js";
import { cellText, limitText } from "./format.js";

renderChrome({ current: "app.html", crumb: "Project / Series A analogs" });
const project = await loadJSON("fixtures/project.json");
document.getElementById("pname").textContent = project.project_name;
const viewer = new SharedViewer(document.getElementById("viewer"));
const eps = Object.keys(project.endpoints);
const config = JSON.parse(JSON.stringify(project.ranking_default));
let selected = project.compounds.find((c) => c.id === 3)?.id ?? project.compounds[0].id;
let rows = [];

// ---- ranking panel controls ----
const lam = document.getElementById("lam");
lam.addEventListener("input", () => { config.lam = Number(lam.value) / 100; refresh(); });
const wbox = document.getElementById("weights");
config.weights.forEach((w) => {
  const id = `w-${w.endpoint}`;
  wbox.append(el("label", { for: id, text: project.endpoints[w.endpoint].label }),
    el("input", { id, class: "num", type: "number", min: 0, max: 10, step: 1, value: w.weight, "data-weight": w.endpoint,
      oninput: (e) => { w.weight = Math.min(10, Math.max(0, Number(e.target.value) || 0)); refresh(); } }));
});
const lbox = document.getElementById("limits");
config.limits.forEach((l) => {
  const side = l.max != null ? "max" : "min";
  const id = `lim-${l.endpoint}`;
  lbox.append(el("div", { class: "kv kv-lim" },
    el("label", { for: id, text: `${project.endpoints[l.endpoint].label} ${side === "max" ? "<" : ">"}` }),
    el("input", { id, class: "num", type: "number", step: "any", value: l[side], "data-limit": l.endpoint,
      oninput: (e) => { l[side] = e.target.value === "" ? null : Number(e.target.value); refresh(); } })),
    el("label", { class: "legend" }, el("input", { type: "checkbox", "aria-label": `Use the unfavorable interval bound for the ${project.endpoints[l.endpoint].label} limit`,
      onchange: (e) => { l.use_unfavorable_bound = e.target.checked; refresh(); } }), " use unfavorable bound (regression only)"));
});

// ---- table ----
const thead = document.getElementById("thead");
thead.append(el("tr", {},
  el("th", { scope: "col" }, "Rank", el("small", { text: "blended (property-only)" })),
  el("th", { scope: "col", text: "Structure" }),
  eps.map((e) => el("th", { scope: "col" }, el("a", { href: `model.html?e=${e}`, text: project.endpoints[e].label }), el("small", { text: project.endpoints[e].unit }))),
  el("th", { scope: "col" }, "Measured", el("small", { text: "your assay values" }))));

function showCompound(id, with3d) {
  selected = id;
  const c = project.compounds.find((x) => x.id === id);
  const d2 = document.getElementById("dock2d");
  d2.replaceChildren(el("img", { class: "mol big", src: c.svg, alt: `2D structure of ${c.name}` }),
    el("p", { class: "legend", text: `${c.name}. ${c.smiles}` }));
  if (with3d) {
    viewer.showMolblock(c.conformer.molblock);
    document.getElementById("viewer-cap").textContent = `${c.name}: ${CONFORMER_LABEL}. (${c.conformer.method})`;
  }
  refresh(false);
  const row = document.querySelector(`tr[data-compound="${id}"]`);
  (with3d ? row?.querySelector("button") : row)?.focus({ preventScroll: true });
}

function refresh(rebuild = true) {
  document.getElementById("lamv").textContent = config.lam.toFixed(2);
  rows = scoreAll(project.compounds, config, project.endpoints);
  const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
  const tbody = document.getElementById("tbody");
  tbody.replaceChildren();
  rows.slice().sort((a, b) => a.blendedRank - b.blendedRank).forEach((r) => {
    const c = project.compounds.find((x) => x.id === r.id);
    const tr = el("tr", { class: "sel-row", tabindex: 0, "aria-selected": r.id === selected, "data-compound": c.id,
      onclick: (ev) => { if (!ev.target.closest("a,button,input")) showCompound(c.id, false); },
      onkeydown: (ev) => { if ((ev.key === "Enter" || ev.key === " ") && ev.target === tr) { ev.preventDefault(); showCompound(c.id, false); } } });
    tr.append(el("td", { class: "nowrap", "data-testid": "rank" }, `${r.blendedRank} (${r.propertyOnlyRank})`,
      r.passes ? null : el("span", { class: "limit-note", text: "fail · breaks a hard limit" })));
    tr.append(el("td", {}, el("div", { class: "molcell nowrap-row" },
      el("img", { class: "mol", src: c.svg, alt: `2D structure of ${c.name}` }),
      el("div", { class: "stackcol" },
        el("span", { class: "chip", text: c.name }),
        el("button", { class: "btn small", type: "button", "aria-label": `Show 3D structure of ${c.name}`, onclick: () => showCompound(c.id, true), text: "3D" })))));
    eps.forEach((e) => {
      const p = c.predictions[e], t = cellText(p);
      const lim = config.limits.find((l) => l.endpoint === e);
      tr.append(el("td", { "data-pred": e, "data-n": p.ensemble_n, "data-coverage": p.coverage, "data-sample-size": p.sample_size },
        el("div", { class: "val" },
          el("a", { href: `compound.html?c=${c.id}&e=${e}`, class: "mono", "aria-label": `${project.endpoints[e].label} for ${c.name}: ${t.main}. Open uncertainty panel`, text: t.main }),
          el("span", { class: "conf", text: t.sub })),
        r.failed.includes(e) && lim ? el("span", { class: "limit-note", text: limitText(lim) }) : null));
    });
    const m = c.measured.solubility;
    tr.append(el("td", {}, el("label", { class: "legend", for: `meas-${c.id}`, text: "logS " }),
      el("input", { id: `meas-${c.id}`, class: "num", type: "text", inputmode: "decimal", placeholder: "add value", value: m ?? "", "aria-label": `Measured solubility logS for ${c.name}` })));
    tbody.append(tr);
  });
  const nfail = rows.filter((r) => !r.passes).length;
  document.getElementById("chips").replaceChildren(
    el("span", { class: "chip", text: `${rows.length} of 10 compounds` }),
    el("span", { class: "chip", text: `Uncertainty weight: ${config.lam.toFixed(2)}` }),
    el("span", { class: "chip", text: `Ensemble N = ${project.ensemble_n}` }),
    el("span", { class: "chip", "data-testid": "nfail", text: `${nfail} fail a hard limit` }));
  renderBreakdown(byId[selected], lam);
}

function renderBreakdown(r) {
  const c = project.compounds.find((x) => x.id === r.id);
  document.getElementById("bd-title").textContent = `${c.name}: blended score R = ${r.R.toFixed(3)} (property-only S = ${r.S.toFixed(3)}, confidence C = ${r.C.toFixed(3)})`;
  const stack = document.getElementById("stack");
  stack.replaceChildren(...r.parts.map((p, i) => el("i", { class: `seg-${i}`, pct: (p.share * 100).toFixed(2), title: `${project.endpoints[p.endpoint].label}: ${p.share.toFixed(3)}` })));
  const tb = document.querySelector("#bd-table tbody");
  tb.replaceChildren(...r.parts.map((p, i) => el("tr", {},
    el("td", {}, el("span", { class: `swatch seg-${i}` }), project.endpoints[p.endpoint].label),
    el("td", { class: "mono", text: p.d.toFixed(3) }), el("td", { class: "mono", text: p.c.toFixed(3) }), el("td", { class: "mono", text: p.share.toFixed(3) }))));
  document.getElementById("formula-text").textContent = FORMULA;
  document.getElementById("formula-num").textContent = `R = (1 - ${config.lam.toFixed(2)}) x ${r.S.toFixed(3)} + ${config.lam.toFixed(2)} x ${r.C.toFixed(3)} = ${r.R.toFixed(3)}`;
}

showCompound(selected, true);
document.activeElement?.blur?.();
