import { el, loadJSON, renderChrome } from "./chrome.js";
import { SharedViewer, CONFORMER_LABEL } from "./viewer.js";

renderChrome({ current: "add.html", crumb: "Project / Add compounds" });
// Mirrors backend/schemas/contracts.py (frozen): ensemble sizes, tier caps, 10-compound cap.
const ENSEMBLE_SIZES = [3, 5, 10, 20], TIER_MAX_N = { free: 10, edu: 20, paid: 20 }, MAX_COMPOUNDS = 10, DEFAULT_N = 5;
const SEC_PER_UNIT = 40 / 75; // placeholder: 40 s for 3 compounds x 5 properties at N=5 (wireframe)
const PROPS = [
  { id: "g-sol", label: "Solubility (thermodynamic; kinetic pending data)", eps: ["solubility"], on: true },
  { id: "g-perm", label: "Permeability (Caco-2, PAMPA)", eps: ["permeability"], on: true },
  { id: "g-tox", label: "hERG, AMES, LD50, liver injury", eps: ["herg", "ames", "ld50", "dili"], on: true },
  { id: "g-hl", label: "Half-life (human), microsomal and hepatocyte clearance", eps: ["half_life", "clearance"], on: false },
  { id: "g-logd", label: "logP and logD (logD at pH 7.4)", eps: ["logd"], on: false },
];

// ---- tabs (ARIA tab pattern) ----
const tabs = [...document.querySelectorAll('[role="tab"]')];
function select(t) {
  tabs.forEach((x) => { const on = x === t; x.setAttribute("aria-selected", on); x.tabIndex = on ? 0 : -1; document.getElementById(x.getAttribute("aria-controls")).hidden = !on; });
  t.focus();
  if (t.id === "t-draw") initDraw();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => select(t));
  t.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") select(tabs[(i + 1) % tabs.length]);
    if (e.key === "ArrowLeft") select(tabs[(i + tabs.length - 1) % tabs.length]);
  });
});

// ---- format check ----
export function checkSmiles(s) {
  if (!/^[A-Za-z0-9@+\-\[\]()\\/=#$:~&!*%.]+$/.test(s)) return { kind: "error", msg: "contains characters that are not valid in SMILES" };
  let paren = 0, inBr = false; const ring = {};
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "[") { if (inBr) return { kind: "error", msg: "nested [ ]" }; inBr = true; continue; }
    if (ch === "]") { if (!inBr) return { kind: "error", msg: "unmatched ]" }; inBr = false; continue; }
    if (inBr) continue;
    if (ch === "(") paren++;
    if (ch === ")") { paren--; if (paren < 0) return { kind: "error", msg: "unmatched )" }; }
    if (/\d/.test(ch)) ring[ch] = (ring[ch] || 0) + 1;
    if (ch === "%" && /\d\d/.test(s.slice(i + 1, i + 3))) { const k = s.slice(i, i + 3); ring[k] = (ring[k] || 0) + 1; i += 2; }
  }
  if (inBr) return { kind: "error", msg: "unclosed [" };
  if (paren !== 0) return { kind: "error", msg: "unbalanced parentheses" };
  const open = Object.keys(ring).filter((k) => ring[k] % 2);
  if (open.length) return { kind: "error", msg: `unclosed ring ${open[0]}` };
  if (s.includes(".")) return { kind: "note", msg: "multiple components (salt or mixture)" };
  return { kind: "ok", msg: "format ok" };
}

const ta = document.getElementById("smi"), check = document.getElementById("check");
let validSmiles = [];
function parseLines() {
  const lines = ta.value.split("\n").map((l) => l.trim()).filter(Boolean);
  const seen = new Set(); validSmiles = [];
  const items = lines.map((l, i) => {
    const [smi, ...nm] = l.split(/\s+/);
    let r = checkSmiles(smi);
    if (seen.has(smi)) r = { kind: "error", msg: "duplicate of an earlier line" };
    seen.add(smi);
    if (r.kind !== "error" && validSmiles.length < MAX_COMPOUNDS) validSmiles.push(smi);
    else if (r.kind !== "error") r = { kind: "error", msg: `over the ${MAX_COMPOUNDS}-compound limit, not submitted` };
    return { i: i + 1, r };
  });
  check.replaceChildren(el("b", { text: "Check (live)" }),
    ...items.map((x) => el("div", {}, el("span", { class: "tag", "data-kind": x.r.kind, text: x.r.kind }), `compound ${x.i}: ${x.r.msg}`)),
    el("div", { class: "muted", "data-testid": "slots", text: `${validSmiles.length} of ${MAX_COMPOUNDS} slots used` }));
  update();
}
ta.addEventListener("input", parseLines);

// ---- properties, ensemble size, time estimate ----
const props = document.getElementById("props");
PROPS.forEach((p) => props.append(el("label", { for: p.id }, el("input", { id: p.id, type: "checkbox", checked: p.on, onchange: update }), p.label)));
const ens = document.getElementById("ens"), tier = document.getElementById("tier");
function fillEns() {
  const cap = TIER_MAX_N[tier.value], cur = Number(ens.value) || DEFAULT_N;
  ens.replaceChildren(...ENSEMBLE_SIZES.map((n) => el("option", { value: n, disabled: n > cap, selected: n === Math.min(cur, cap), text: n > cap ? `${n} (verified .edu)` : String(n) })));
}
tier.addEventListener("change", () => { fillEns(); update(); });
ens.addEventListener("change", update);
function selectedEndpoints() { return PROPS.filter((p) => document.getElementById(p.id).checked).flatMap((p) => p.eps); }
function update() {
  const n = validSmiles.length, np = selectedEndpoints().length, N = Number(ens.value);
  const secs = Math.round(n * np * N * SEC_PER_UNIT);
  document.getElementById("eta").textContent = `Estimated time: ${secs} s for ${n} compound${n === 1 ? "" : "s"} and ${np} properties at N = ${N} (placeholder). More members give steadier uncertainty and take longer.`;
  document.getElementById("submit").disabled = n === 0 || np === 0;
}
document.getElementById("submit").addEventListener("click", () => {
  const pre = document.getElementById("payload");
  pre.hidden = false;
  pre.textContent = "Fixture mode: nothing was sent. The request would be\n" + JSON.stringify({ smiles: validSmiles, ensemble_n: Number(ens.value), endpoints: selectedEndpoints() }, null, 1);
});

// ---- upload ----
document.getElementById("file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  document.getElementById("file-note").textContent = f ? `${f.name} (${f.size} bytes). Parsing happens on the server in a sandbox; not parsed here.` : "";
});

// ---- draw: live 2D + 3D preview (fixture structures) ----
let viewer = null, project = null;
async function initDraw() {
  if (!project) {
    project = await loadJSON("fixtures/project.json");
    const tpl = document.getElementById("tpl");
    project.compounds.forEach((c) => tpl.append(el("option", { value: c.id, text: `${c.name}: ${c.smiles}` })));
    tpl.addEventListener("change", showTpl);
    viewer = new SharedViewer(document.getElementById("viewer"));
  }
  showTpl();
}
function showTpl() {
  const c = project.compounds.find((x) => x.id === Number(document.getElementById("tpl").value));
  document.getElementById("draw2d").replaceChildren(el("img", { class: "mol big", src: c.svg, alt: `2D structure of ${c.name}` }));
  viewer.showMolblock(c.conformer.molblock);
  document.getElementById("viewer-cap").textContent = CONFORMER_LABEL;
}

fillEns(); parseLines();
