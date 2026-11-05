// Metis Lead Studio: saved runs, manual sessions and domain profiles ("My library"). Self-contained ES module; injects its own DOM into `.topbar`.
// Identity is the Metis session (cookie + CSRF header, handled by net.js -> web/js/api.js): there is no sign-in UI here.
// All DOM is built with textContent / createTextNode (the h() helper below); server strings never go through innerHTML.
//
// Integration points
//   in : bus events run:start / run:candidate / run:generation / run:policy / run:end / run:error (window.metis.bus)
//        window event "metis:manual-export" {detail: {steps?: object[], jsonl?: string, seed_smiles?, target_id?}}
//   out: window event "metis:replay" {detail: {events}} and window.MetisStudio.replayEvents(events) if present
//        window.MetisAccount = {saveManual(detail), openLibrary(), openProfiles()}
import { lead } from "../net.js";

const state = { events: [], ended: false, profiles: [] };
let root;

// ------------------------------------------------------------------ helpers
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k in el && k !== "list") el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

// Same call shape as before (api("/library/runs", { method, body })), now through the Metis client under /lead.
const api = (path, { method = "GET", body } = {}) => lead(method, path, body === undefined ? {} : { json: body });

function toast(msg) {
  const t = h("div", { class: "acct-toast", role: "status" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}

function modal(content, { wide = false, onClose } = {}) {
  const close = () => { back.remove(); document.removeEventListener("keydown", esc); onClose?.(); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  const box = h("div", { class: "acct-modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true" }, content);
  const back = h("div", { class: "acct-backdrop", on: { mousedown: (e) => { if (e.target === back) close(); } } }, box);
  document.addEventListener("keydown", esc);
  document.body.append(back);
  box.querySelector("input")?.focus();
  return { close, box };
}

// ----------------------------------------------------------------- top bar UI
let saveBtn;
function render() {
  root.replaceChildren();
  saveBtn = h("button", { class: "abtn save-run", type: "button", hidden: !state.ended, title: "Save this run to your library", on: { click: saveRun } }, "Save this run");
  root.append(saveBtn, h("button", { class: "abtn", type: "button", title: "Saved runs, manual sessions and domain profiles", on: { click: () => openDrawer("runs") } }, "My library"));
}

// -------------------------------------------------------------- save / replay
function replay(events) {
  window.dispatchEvent(new CustomEvent("metis:replay", { detail: { events } }));
  window.MetisStudio?.replayEvents?.(events);
}

async function saveRun() {
  if (!state.events.length) return toast("Nothing to save yet");
  const title = prompt("Name this run", "") ?? null;
  if (title === null) return;
  try {
    await api("/library/runs", { method: "POST", body: { title: title.trim() || undefined, events: state.events } });
    toast("Saved to My library");
  } catch (ex) { toast(ex.message); }
}

async function saveManual(detail) {
  try {
    await api("/library/manual", { method: "POST", body: { steps: detail.steps, jsonl: detail.jsonl, seed_smiles: detail.seed_smiles, target_id: detail.target_id } });
    toast("Manual session saved"); return true;
  } catch (ex) { toast(ex.message); return false; }
}

// --------------------------------------------------------------------- drawer
let drawer;
function closeDrawer() { drawer?.remove(); drawer = null; }

async function openDrawer(tab = "runs") {
  closeDrawer();
  const body = h("div", { class: "body" });
  const tabs = ["runs", "manual", "profiles"].map((t) => h("button", {
    role: "tab", "aria-selected": t === tab, on: { click: () => openDrawer(t) },
  }, { runs: "Saved runs", manual: "Manual", profiles: "Profiles" }[t]));
  drawer = h("aside", { class: "acct-drawer", "aria-label": "My library" },
    h("header", {}, h("h2", {}, "My library"), h("button", { class: "abtn", on: { click: closeDrawer }, "aria-label": "Close" }, "✕")),
    h("div", { class: "acct-tabs", role: "tablist" }, tabs), body);
  document.body.append(drawer);
  body.append(h("div", { class: "acct-empty" }, "Loading…"));
  try {
    if (tab === "runs") renderRuns(body, (await api("/library/runs")).runs);
    else if (tab === "manual") renderManual(body, (await api("/library/manual")).sessions);
    else renderProfiles(body, (await api("/profiles")).profiles);
  } catch (ex) { body.replaceChildren(h("div", { class: "acct-empty" }, ex.message)); }
}

function when(iso) { try { return new Date(iso).toLocaleString(); } catch { return ""; } }

function renderRuns(body, runs) {
  body.replaceChildren(...(runs.length ? runs.map((r) => h("div", { class: "acct-item" },
    h("div", { class: "t" }, r.title),
    h("div", { class: "m" }, h("span", { class: "badge" }, r.stage || "run"), r.target_id ? h("span", { class: "badge" }, r.target_id) : null, r.headline || r.seed_smiles || ""),
    h("div", { class: "m" }, when(r.created_at)),
    h("div", { class: "acts" },
      h("button", { class: "abtn primary", on: { click: async () => {
        try { const full = await api(`/library/runs/${encodeURIComponent(r.id)}`); closeDrawer(); replay(full.events); } catch (ex) { toast(ex.message); }
      } } }, "Replay"),
      h("button", { class: "abtn destructive", on: { click: async () => { if (confirm("Delete this run?")) { await api(`/library/runs/${encodeURIComponent(r.id)}`, { method: "DELETE" }); openDrawer("runs"); } } } }, "Delete"))))
    : [h("div", { class: "acct-empty" }, "No saved runs yet. Finish a run and press “Save this run”.")]));
}

function renderManual(body, sessions) {
  body.replaceChildren(...(sessions.length ? sessions.map((s) => h("div", { class: "acct-item" },
    h("div", { class: "t" }, s.seed_smiles || "Manual session"),
    h("div", { class: "m" }, h("span", { class: "badge" }, "manual"), s.target_id || "", " ", when(s.created_at)),
    h("div", { class: "acts" },
      h("button", { class: "abtn", on: { click: async () => {
        const full = await api(`/library/manual/${encodeURIComponent(s.id)}`);
        const blob = new Blob([full.steps.map((x) => JSON.stringify(x)).join("\n") + "\n"], { type: "application/x-ndjson" });
        const a = h("a", { href: URL.createObjectURL(blob), download: `manual_session_${encodeURIComponent(s.id)}.jsonl` }); a.click(); URL.revokeObjectURL(a.href);
      } } }, "Export JSONL"),
      h("button", { class: "abtn destructive", on: { click: async () => { if (confirm("Delete this session?")) { await api(`/library/manual/${encodeURIComponent(s.id)}`, { method: "DELETE" }); openDrawer("manual"); } } } }, "Delete"))))
    : [h("div", { class: "acct-empty" }, "No manual sessions saved yet.")]));
}

function renderProfiles(body, profiles) {
  state.profiles = profiles;
  body.replaceChildren(h("button", { class: "abtn primary", on: { click: () => openProfileEditor(null) } }, "+ New profile"),
    h("div", { class: "acct-gap" }),
    ...(profiles.length ? profiles.map((p) => h("div", { class: "acct-item" },
      h("div", { class: "t" }, p.name),
      h("div", { class: "m" }, `${p.avoid_smarts.length} excluded pattern(s) · ${Object.keys(p.objectives).length} objective(s)${p.target_id ? " · " + p.target_id : ""} · id ${p.id}`),
      h("div", { class: "acts" },
        h("button", { class: "abtn", on: { click: () => openProfileEditor(p) } }, "Edit"),
        h("button", { class: "abtn destructive", on: { click: async () => { if (confirm("Delete this profile?")) { await api(`/profiles/${encodeURIComponent(p.id)}`, { method: "DELETE" }); openDrawer("profiles"); } } } }, "Delete"))))
      : [h("div", { class: "acct-empty" }, "No profiles yet. A profile stores objective weights and structures to exclude.")]));
}

// ------------------------------------------------------------- profile editor
const OBJECTIVES = ["qed", "logp", "tpsa", "alerts"];

function openProfileEditor(p) {
  const name = h("input", { type: "text", maxLength: 80, value: p?.name || "" });
  const err = h("div", { class: "acct-err", role: "alert" });
  const sliders = OBJECTIVES.map((k) => {
    const w = p?.objectives?.[k]?.weight ?? 1;
    const out = h("output", {}, Number(w).toFixed(1));
    const input = h("input", { type: "range", min: 0, max: 5, step: 0.1, value: w, "aria-label": `${k} weight`, on: { input: () => { out.textContent = Number(input.value).toFixed(1); } } });
    return { k, input, row: h("div", { class: "acct-slider" }, h("span", {}, k), input, out) };
  });
  const list = h("div");
  const addBtn = h("button", { type: "button", class: "abtn", on: { click: () => addRow("") } }, "+ Add pattern");
  const rows = [];
  let timer;
  function addRow(val) {
    if (rows.length >= 20) return;
    const input = h("input", { type: "text", maxLength: 200, value: val, spellcheck: false, placeholder: "SMARTS, e.g. [N+](=O)[O-]", "aria-label": "Excluded SMARTS" });
    const why = h("span", { class: "why" });
    const row = h("div", { class: "smarts-row" }, input, why, h("button", { type: "button", class: "abtn", "aria-label": "Remove", on: { click: () => { row.remove(); rows.splice(rows.findIndex((r) => r.input === input), 1); } } }, "✕"));
    rows.push({ input, why }); list.append(row);
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(validate, 300); });
    if (val) validate();
  }
  async function validate() {
    const live = rows.filter((r) => r.input.value.trim());
    rows.forEach((r) => { r.input.className = ""; r.why.textContent = ""; });
    if (!live.length) return true;
    try {
      const { results } = await api("/profiles/check-smarts", { method: "POST", body: { patterns: live.map((r) => r.input.value) } });
      let ok = true;
      results.forEach((res, i) => { live[i].input.className = res.ok ? "fav" : "unfav"; live[i].why.textContent = res.ok ? "" : res.error; ok = ok && res.ok; });
      return ok;
    } catch { return true; } // server will have the final say
  }
  (p?.avoid_smarts || []).forEach(addRow);
  const save = h("button", { class: "abtn primary grow", type: "submit" }, "Save profile");
  const form = h("form", { on: { submit: async (e) => {
    e.preventDefault(); err.textContent = ""; save.disabled = true;
    const body = {
      name: name.value, target_id: p?.target_id || null,
      objectives: Object.fromEntries(sliders.map((s) => [s.k, { weight: Number(s.input.value), direction: "range" }])),
      avoid_smarts: rows.map((r) => r.input.value.trim()).filter(Boolean), edit_allowlist: p?.edit_allowlist || [],
    };
    try {
      if (!(await validate())) throw new Error("Fix the highlighted SMARTS first");
      await api(p ? `/profiles/${encodeURIComponent(p.id)}` : "/profiles", { method: p ? "PUT" : "POST", body });
      m.close(); toast("Profile saved"); if (drawer) openDrawer("profiles");
    } catch (ex) { err.textContent = ex.message; } finally { save.disabled = false; }
  } } },
  h("label", {}, "Profile name"), name,
  h("label", {}, "Objective weights"), ...sliders.map((s) => s.row),
  h("label", {}, "Exclude these substructures (SMARTS, checked live)"), list, h("div", { class: "acct-gap-s" }, addBtn),
  err, h("div", { class: "acct-row" }, save, h("button", { type: "button", class: "abtn", on: { click: () => m.close() } }, "Cancel")),
  h("div", { class: "acct-hint" }, "Profile id " + (p ? p.id : "(assigned on save)") + ". Select it on a run via RunConfig.profile_id."));
  const m = modal([h("h2", {}, p ? "Edit profile" : "New profile"), h("p", { class: "sub" }, "Matching an exclusion pattern adds a USER alert and lowers the composite."), form], { wide: true });
}

// ----------------------------------------------------------------------- boot
function wireBus() {
  const bus = window.metis?.bus;
  if (!bus) return false;
  bus.on("run:start", (d) => { state.events = [d.event]; state.ended = false; saveBtn && (saveBtn.hidden = true); });
  for (const n of ["run:candidate", "run:generation", "run:policy"]) bus.on(n, (d) => { if (d?.event) state.events.push(d.event); });
  bus.on("run:end", (d) => { if (d?.event) state.events.push(d.event); state.ended = true; saveBtn && (saveBtn.hidden = false); });
  bus.on("run:error", () => { state.ended = false; saveBtn && (saveBtn.hidden = true); });
  return true;
}

function boot() {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  root = h("div", { class: "acct" });
  const anchor = bar.querySelector("#btn-export");
  anchor ? bar.insertBefore(root, anchor) : bar.append(root);
  render();
  if (!wireBus()) { let n = 0; const t = setInterval(() => { if (wireBus() || ++n > 40) clearInterval(t); }, 250); }
  window.addEventListener("metis:manual-export", (e) => saveManual(e.detail || {}));
  window.MetisAccount = { saveManual, openLibrary: () => openDrawer("runs"), openProfiles: () => openDrawer("profiles") };
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
