// Add compounds (SMILES, molfile/SDF paste, CSV/SDF upload, Ketcher drawing) and run predictions for one project.
import { el, qsId, renderChrome, say, showEmpty, showError, showLoading, stateBox } from "./chrome.js";
import { authed, describeError, get, post, requireSession } from "./api.js";

const $ = (id) => document.getElementById(id);
const user = await requireSession();
renderChrome({ current: "app.html", crumb: "Projects / Add compounds", user });
// Mirrors backend/schemas/contracts.py (frozen): ensemble sizes, tier caps, 10-compound cap.
const ENSEMBLE_SIZES = [3, 5, 10, 20], TIER_MAX_N = { free: 10, edu: 20, paid: 20 }, MAX_COMPOUNDS = 10, DEFAULT_N = 5;
const pid = qsId("p");
const page = $("page-state");

if (!pid) {
  showEmpty(page, "Open a project first, then choose Add compounds.");
  page.append(el("p", {}, el("a", { class: "btn primary", href: "app.html", text: "Go to projects" })));
} else {
  $("back").href = `app.html?p=${pid}`;
  start();
}

async function start() {
  showLoading(page, "Loading project");
  let project, models;
  try { [project, models] = await authed(() => Promise.all([get(`/projects/${pid}`), get("/models")])); } catch (e) { return showError(page, e, start); }
  page.replaceChildren();
  document.querySelector(".crumb").textContent = `Projects / ${project.name} / Add compounds`;
  $("add-frame").hidden = false; $("predict").hidden = false;
  let existing = project.compounds.length;
  const slots = () => MAX_COMPOUNDS - existing;

  // ---- tabs (ARIA tab pattern) ----
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  function select(t) {
    tabs.forEach((x) => { const on = x === t; x.setAttribute("aria-selected", on); x.tabIndex = on ? 0 : -1; $(x.getAttribute("aria-controls")).hidden = !on; });
    t.focus();
    if (t.id === "t-draw") initDraw();
    if (t.id === "t-paste") parseLines();
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") select(tabs[(i + 1) % tabs.length]);
      if (e.key === "ArrowLeft") select(tabs[(i + tabs.length - 1) % tabs.length]);
    });
  });
  const activeTab = () => tabs.find((t) => t.getAttribute("aria-selected") === "true").id;

  // ---- format check (client side, SMILES tab) ----
  const ta = $("smi"), check = $("check");
  function lines() { return ta.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const [smi, ...nm] = l.split(/\s+/); return { smi, name: nm.join(" ") }; }); }
  function parseLines() {
    const seen = new Set();
    let used = 0;
    const items = lines().map((l, i) => {
      let r = checkSmiles(l.smi);
      if (seen.has(l.smi)) r = { kind: "error", msg: "duplicate of an earlier line" };
      seen.add(l.smi);
      if (r.kind !== "error") { used++; if (used > slots()) r = { kind: "error", msg: `over the ${MAX_COMPOUNDS}-compound limit, not submitted` }; }
      return { i: i + 1, r };
    });
    check.replaceChildren(el("b", { text: "Check (live)" }),
      ...items.map((x) => el("div", {}, el("span", { class: "tag", "data-kind": x.r.kind, text: x.r.kind }), `compound ${x.i}: ${x.r.msg}`)),
      el("div", { class: "muted", "data-testid": "slots", text: `${Math.min(existing + used, MAX_COMPOUNDS)} of ${MAX_COMPOUNDS} slots used (${existing} already in this project)` }));
  }
  ta.addEventListener("input", parseLines);
  parseLines();

  // ---- upload ----
  let uploaded = null;
  $("file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    uploaded = null;
    if (!f) { $("file-note").textContent = ""; return; }
    if (f.size > 900000) { $("file-note").textContent = `${f.name} is ${f.size} bytes; the limit is 900 KB.`; return; }
    const ext = f.name.toLowerCase().split(".").pop();
    uploaded = { format: ext === "csv" ? "csv" : "sdf", content: await f.text() };
    $("file-note").textContent = `${f.name} (${f.size} bytes) will be sent to the server as ${uploaded.format.toUpperCase()}; it is parsed there in a sandbox.`;
  });

  // ---- draw (Ketcher, same-origin iframe) ----
  function initDraw() {
    const fr = $("ketcher");
    if (!fr.getAttribute("src")) fr.setAttribute("src", fr.dataset.src);
  }
  async function drawnSmiles() {
    const k = $("ketcher").contentWindow && $("ketcher").contentWindow.ketcher;
    if (!k) throw new Error("The structure editor is still loading. Wait a moment and try again.");
    const smi = await k.getSmiles();
    if (!smi) throw new Error("Draw a structure first.");
    return smi;
  }

  // ---- submit ----
  function buildRequest() {
    const tab = activeTab();
    if (tab === "t-paste") {
      const ls = lines();
      if (!ls.length) throw new Error("Enter at least one SMILES.");
      if (ls.some((l) => l.name)) {
        const q = (s) => `"${String(s).replace(/"/g, '""')}"`;
        return { format: "csv", content: ["smiles,name", ...ls.map((l) => `${q(l.smi)},${q(l.name)}`)].join("\n") };
      }
      return { format: "smiles", smiles: ls.map((l) => l.smi) };
    }
    if (tab === "t-mol") {
      const v = $("molblock").value;
      if (!v.trim()) throw new Error("Paste a molfile or SDF first.");
      return { format: /\$\$\$\$/.test(v) ? "sdf" : "molblock", content: v };
    }
    if (tab === "t-upload") {
      if (!uploaded) throw new Error("Choose a CSV or SDF file first.");
      return uploaded;
    }
    return null;
  }
  $("add-submit").addEventListener("click", async () => {
    const msg = $("add-msg"), problems = $("add-problems");
    problems.replaceChildren();
    $("add-submit").disabled = true;
    try {
      let body;
      if (activeTab() === "t-draw") body = { format: "smiles", smiles: [await drawnSmiles()] };
      else body = buildRequest();
      say(msg, "Sending to the server...", "ok");
      const res = await authed(() => post(`/projects/${pid}/compounds`, body));
      existing += res.compounds.length;
      say(msg, `Added ${res.compounds.length} compound${res.compounds.length === 1 ? "" : "s"}. The project now has ${existing} of ${MAX_COMPOUNDS}.`, "ok");
      ta.value = ""; parseLines();
      renderEta();
    } catch (e) {
      const d = describeError(e);
      say(msg, e.status === undefined ? e.message : d.message, "error");
      problems.replaceChildren(...d.problems.map((p) => el("div", {}, el("span", { class: "tag", "data-kind": "error", text: "error" }), `record ${p.record}: ${p.error}`)));
    } finally { $("add-submit").disabled = false; }
  });

  // ---- predict ----
  const modelsHost = $("models-state");
  const cards = models.models;
  if (!cards.length) {
    modelsHost.replaceChildren(stateBox("empty", "No models are published yet, so no predictions can be made. Check the models page later.", { id: "no-models" }));
    $("predict-submit").disabled = true;
  } else {
    $("predict-form").hidden = false;
    const props = $("props");
    for (const m of cards) {
      props.append(el("label", { for: `ep-${m.endpoint}` },
        el("input", { id: `ep-${m.endpoint}`, type: "checkbox", "data-endpoint": m.endpoint, checked: true, onchange: () => { fillEns(); renderEta(); } }),
        el("span", {}, el("a", { href: `model.html?e=${encodeURIComponent(m.endpoint)}`, text: m.endpoint }), ` (${m.kind}${m.units ? ", " + m.units : ""}) `,
          el("span", { class: "legend", text: `calibrated at N = ${Object.keys(m.coverage_by_n).join(", ")}` }))));
    }
  }
  const selectedCards = () => cards.filter((m) => $(`ep-${m.endpoint}`)?.checked);
  const cap = TIER_MAX_N[user.tier] ?? 10;
  const ens = $("ens");
  function offered() {
    const sel = selectedCards();
    return ENSEMBLE_SIZES.map((n) => {
      if (n > cap) return { n, ok: false, why: "needs a verified .edu or paid account" };
      if (sel.some((m) => !(String(n) in m.coverage_by_n))) return { n, ok: false, why: "not calibrated for every selected property" };
      return { n, ok: true };
    });
  }
  function fillEns() {
    const cur = Number(ens.value) || DEFAULT_N, opts = offered();
    const pick = opts.find((o) => o.ok && o.n === cur) || opts.find((o) => o.ok && o.n === DEFAULT_N) || opts.find((o) => o.ok);
    ens.replaceChildren(...opts.map((o) => el("option", { value: o.n, disabled: !o.ok, selected: pick && o.n === pick.n, text: o.ok ? String(o.n) : `${o.n} (${o.why})` })));
    $("tier-note").textContent = `Your account type is ${user.tier}: ensemble sizes up to N = ${cap}. Only sizes with a published calibration are offered.`;
  }
  ens.addEventListener("change", () => renderEta());
  function renderEta() {
    const sel = selectedCards(), n = Number(ens.value), nc = Math.max(existing, 1);
    if (!sel.length || !n) { $("eta").textContent = ""; $("predict-submit").disabled = true; return; }
    const secs = sel.reduce((a, m) => a + (m.runtime_by_n_seconds[String(n)] || 0), 0) * nc;
    $("eta").textContent = `Estimated time: about ${secs < 10 ? secs.toFixed(1) : Math.round(secs)} s for ${nc} compound${nc === 1 ? "" : "s"} at N = ${n}, from the model cards (runtime per compound). The server confirms its own estimate when you start. More members give steadier uncertainty and take longer.`;
    $("predict-submit").disabled = existing === 0;
  }
  if (cards.length) { fillEns(); renderEta(); }

  $("predict-submit").addEventListener("click", async () => {
    const sel = selectedCards().map((m) => m.endpoint), n = Number(ens.value);
    const job = $("job");
    if (!sel.length) { job.replaceChildren(stateBox("error", "Choose at least one property.")); return; }
    $("predict-submit").disabled = true;
    job.replaceChildren(stateBox("loading", "Starting..."));
    try {
      const j = await authed(() => post(`/projects/${pid}/predict`, { endpoints: sel, ensemble_n: n }));
      await track(j.job_id, j.estimated_seconds, j.total);
    } catch (e) { job.replaceChildren(stateBox("error", describeError(e).message)); }
    finally { $("predict-submit").disabled = false; }
  });

  async function track(jobId, estimate, total) {
    const job = $("job");
    const bar = el("progress", { id: "job-bar", max: total || 1, value: 0, "aria-label": "Prediction progress" });
    const line = el("span", { id: "job-line", "data-testid": "job-line" });
    job.replaceChildren(el("div", { class: "inline" }, bar, line),
      el("p", { class: "legend", "data-testid": "job-estimate", text: `Server estimate: about ${Math.round(estimate)} s.` }));
    for (;;) {
      const st = await authed(() => get(`/jobs/${jobId}`));
      bar.max = st.total || 1; bar.value = st.done;
      line.textContent = `${st.state}: ${st.done} of ${st.total} predictions`;
      if (st.state === "done") {
        job.append(el("p", {}, el("a", { class: "btn primary", id: "to-table", href: `app.html?p=${pid}`, text: "View the project table" })));
        return;
      }
      if (st.state === "failed") { job.replaceChildren(stateBox("error", "The prediction job failed. Try again; if it keeps failing, report it.")); return; }
      await new Promise((r) => setTimeout(r, 700));
    }
  }
}

// ---- SMILES format check ----
export function checkSmiles(s) {
  if (!/^[A-Za-z0-9@+\-[\]()\\/=#$:~&!*%.]+$/.test(s)) return { kind: "error", msg: "contains characters that are not valid in SMILES" };
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
