// Projects list and the project table with the ranking panel. All data comes from the API (see docs/WEB_WIRING.md).
import { el, qsId, renderChrome, showEmpty, showError, showLoading, stateBox, say, confirmDialog, fmtDate } from "./chrome.js";
import { authed, del, depictionUrl, describeError, get, post, put, requireSession } from "./api.js";
import { SharedViewer, CONFORMER_LABEL } from "./viewer.js";
import { cellText, limitText, num } from "./format.js";

const $ = (id) => document.getElementById(id);
const user = await requireSession();
renderChrome({ current: "app.html", crumb: "Projects", user });
const pid = qsId("p");

if (!pid) projectsList(); else projectTable(pid);

// =============================================================== projects list
async function projectsList() {
  $("projects-view").hidden = false;
  const host = $("plist");
  async function load() {
    showLoading(host, "Loading projects");
    let data;
    try { data = await authed(() => get("/projects")); } catch (e) { return showError(host, e, load); }
    if (!data.projects.length) return showEmpty(host, "No projects yet. Name one above to start.");
    host.replaceChildren(el("ul", { class: "plain-list", "data-testid": "project-list" }, data.projects.map((p) =>
      el("li", { class: "inline", "data-project": p.id },
        el("a", { href: `app.html?p=${p.id}`, class: "proj-link", text: p.name }),
        el("span", { class: "chip", text: `${p.compound_count} of 10 compounds` }),
        el("span", { class: "legend", text: `created ${fmtDate(p.created_at)}` }),
        el("button", { class: "btn small", type: "button", "aria-label": `Delete project ${p.name}`, text: "Delete", onclick: async () => {
          const ok = await confirmDialog({ title: "Delete project", body: `Delete "${p.name}" with all its compounds, predictions and measurements? This cannot be undone.`, okLabel: "Delete project" });
          if (!ok) return;
          try { await authed(() => del(`/projects/${p.id}`)); await load(); } catch (e) { showError(host, e, load); }
        } })))));
  }
  $("new-project").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("np-msg"), name = $("np-name").value.trim();
    if (!name) { say(msg, "Enter a project name.", "error"); return; }
    try {
      const p = await authed(() => post("/projects", { name }));
      location.assign(`app.html?p=${p.id}`);
    } catch (e) { say(msg, describeError(e).message, "error"); }
  });
  await load();
}

// =============================================================== project table
async function projectTable(projectId) {
  const pageState = $("page-state");
  showLoading(pageState, "Loading project");
  let project, models, saved;
  try {
    [project, models] = await authed(() => Promise.all([get(`/projects/${projectId}`), get("/models")]));
    saved = await authed(() => get(`/projects/${projectId}/ranking`));
  } catch (e) {
    return showError(pageState, e, () => location.reload());
  }
  pageState.replaceChildren();
  $("project-view").hidden = false;
  $("pname").textContent = project.name;
  document.title = `${project.name} | Metis`;
  $("lnk-add").href = `add.html?p=${projectId}`;
  $("lnk-export").href = `report.html?p=${projectId}`;
  document.querySelector(".crumb").textContent = `Projects / ${project.name}`;
  $("del-project").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Delete project", body: `Delete "${project.name}" with all its compounds, predictions and measurements? This cannot be undone.`, okLabel: "Delete project" });
    if (!ok) return;
    try { await authed(() => del(`/projects/${projectId}`)); location.assign("app.html"); } catch (e) { showError(pageState, e); }
  });

  const cards = Object.fromEntries(models.models.map((m) => [m.endpoint, m]));
  const compounds = project.compounds;
  const stateHost = $("ptable-state");
  if (!compounds.length) {
    showEmpty(stateHost, "This project has no compounds yet.");
    stateHost.append(el("p", {}, el("a", { class: "btn primary", href: `add.html?p=${projectId}`, text: "Add compounds" })));
    $("chips").replaceChildren(el("span", { class: "chip", text: "0 of 10 compounds" }));
    return;
  }
  $("ptable-row").hidden = false;
  const eps = [...new Set(compounds.flatMap((c) => c.predictions.map((x) => x.prediction.endpoint)))].sort();
  const predOf = (c, e) => c.predictions.find((x) => x.prediction.endpoint === e);
  if (!eps.length) {
    stateHost.replaceChildren(stateBox("empty", "No predictions yet. Run predictions to fill the table and enable ranking."),
      el("p", {}, el("a", { class: "btn primary", href: `add.html?p=${projectId}#predict`, text: "Run predictions" })));
  }

  const viewer = new SharedViewer($("viewer"));
  let ranking = null;     // {compound_id -> ranked entry}
  let stale = false;
  let selected = compounds[0].id;
  const cfg = { lam: 0.25, rows: {} };  // rows[endpoint] = {weight, higher, tmin, tmax, lmin, lmax, unfav}

  // ---- ranking panel ----
  const saveCfg = saved.persisted ? saved.config : null;
  for (const e of eps) {
    const w = saveCfg?.weights.find((x) => x.endpoint === e), l = saveCfg?.limits.find((x) => x.endpoint === e);
    cfg.rows[e] = { weight: w ? w.weight : 1, higher: w ? w.higher_is_better : true,
      tmin: w?.target_min ?? null, tmax: w?.target_max ?? null, lmin: l?.min ?? null, lmax: l?.max ?? null, unfav: !!l?.use_unfavorable_bound };
  }
  if (saveCfg) cfg.lam = saveCfg.lam;
  const label = (e) => e;
  const defRange = (e) => cards[e]?.default_range ?? null;

  const lam = $("lam");
  lam.value = String(Math.round(cfg.lam * 100)); $("lamv").textContent = cfg.lam.toFixed(2);
  lam.addEventListener("input", () => { cfg.lam = Number(lam.value) / 100; $("lamv").textContent = cfg.lam.toFixed(2); dirty(); });

  const wbox = $("weights"), lbox = $("limits");
  for (const e of eps) {
    const r = cfg.rows[e], dr = defRange(e), unit = cards[e]?.units;
    const bind = (id, key, conv) => (ev) => { r[key] = conv(ev.target); dirty(); };
    wbox.append(el("fieldset", { class: "bare rank-row", "data-endpoint": e },
      el("legend", {}, el("a", { href: `model.html?e=${encodeURIComponent(e)}`, text: label(e) }), unit ? el("span", { class: "legend", text: ` (${unit})` }) : null),
      el("label", { for: `w-${e}`, text: "Weight" }),
      el("input", { id: `w-${e}`, class: "num", type: "number", min: 0, max: 10, step: 1, value: r.weight, "data-weight": e, oninput: bind("", "weight", (t) => Math.min(10, Math.max(0, Number(t.value) || 0))) }),
      el("label", { for: `dir-${e}`, text: "Better when" }),
      el("select", { id: `dir-${e}`, "data-direction": e, onchange: (ev) => { r.higher = ev.target.value === "higher"; dirty(); } },
        el("option", { value: "higher", text: "higher", selected: r.higher }), el("option", { value: "lower", text: "lower", selected: !r.higher })),
      el("label", { for: `tmin-${e}`, text: "Target min" }),
      el("input", { id: `tmin-${e}`, class: "num", type: "number", step: "any", "data-tmin": e, value: r.tmin ?? (dr ? dr[0] : ""), oninput: dirty }),
      el("label", { for: `tmax-${e}`, text: "Target max" }),
      el("input", { id: `tmax-${e}`, class: "num", type: "number", step: "any", "data-tmax": e, value: r.tmax ?? (dr ? dr[1] : ""), oninput: dirty }),
      el("span", { class: "legend rank-default", text: dr ? `model default: ${dr[0]} to ${dr[1]}` : "no model card: enter both values" }),
      dr ? el("button", { class: "btn small", type: "button", text: "Reset range", "aria-label": `Reset target range for ${e} to the model default`,
        onclick: () => { $(`tmin-${e}`).value = dr[0]; $(`tmax-${e}`).value = dr[1]; dirty(); } }) : null));
    lbox.append(el("fieldset", { class: "bare rank-row", "data-endpoint": e },
      el("legend", { text: label(e) }),
      el("label", { for: `lmin-${e}`, text: "Min" }),
      el("input", { id: `lmin-${e}`, class: "num", type: "number", step: "any", "data-limit-min": e, value: r.lmin ?? "", oninput: (ev) => { r.lmin = num(ev.target.value); dirty(); } }),
      el("label", { for: `lmax-${e}`, text: "Max" }),
      el("input", { id: `lmax-${e}`, class: "num", type: "number", step: "any", "data-limit-max": e, value: r.lmax ?? "", oninput: (ev) => { r.lmax = num(ev.target.value); dirty(); } }),
      el("label", { class: "legend span-all" }, el("input", { type: "checkbox", checked: r.unfav, "data-unfav": e, onchange: (ev) => { r.unfav = ev.target.checked; dirty(); } }),
        " use unfavorable interval bound (regression only)")));
  }
  if (!eps.length) { $("rank-save").disabled = true; say($("rank-msg"), "Ranking needs predictions.", "ok"); }

  // An untouched or default range is sent as null/null so the model's current default applies (decision 0002).
  function readRange(e) {
    const a = num($(`tmin-${e}`).value), b = num($(`tmax-${e}`).value), dr = defRange(e);
    if (a == null || b == null || (dr && a === dr[0] && b === dr[1])) return [null, null];
    return [a, b];
  }
  function buildConfig() {
    const weights = eps.map((e) => {
      const r = cfg.rows[e], [tmin, tmax] = readRange(e);
      const out = { endpoint: e, weight: r.weight, higher_is_better: r.higher };
      if (tmin != null) { out.target_min = tmin; out.target_max = tmax; }
      return out;
    });
    const limits = eps.filter((e) => cfg.rows[e].lmin != null || cfg.rows[e].lmax != null).map((e) => {
      const r = cfg.rows[e];
      return { endpoint: e, min: r.lmin, max: r.lmax, use_unfavorable_bound: r.unfav };
    });
    return { weights, limits, lam: cfg.lam };
  }

  function dirty() { say($("rank-msg"), "Changed. Press Save and rank to apply.", "ok"); }

  async function rank() {
    const msg = $("rank-msg");
    say(msg, "Ranking...", "ok");
    $("rank-save").disabled = true;
    try {
      const res = await authed(() => put(`/projects/${projectId}/ranking`, buildConfig()));
      ranking = Object.fromEntries(res.ranking.map((x) => [x.compound_id, x]));
      stale = !!res.stale_model_version;
      say(msg, "Saved. Ranking computed from the latest predictions.", "ok");
      $("formula-text").textContent = res.formula;
    } catch (e) {
      const d = describeError(e);
      say(msg, d.message, "error");
    } finally { $("rank-save").disabled = false; }
    renderRows();
  }
  $("rank-save").addEventListener("click", rank);

  // ---- table ----
  const thead = $("thead");
  thead.append(el("tr", {},
    el("th", { scope: "col" }, "Rank", el("small", { text: "blended (property-only)" })),
    el("th", { scope: "col", text: "Structure" }),
    eps.map((e) => el("th", { scope: "col" }, el("a", { href: `model.html?e=${encodeURIComponent(e)}`, text: label(e) }), el("small", { text: cards[e]?.units || "" }))),
    el("th", { scope: "col" }, "Measured", el("small", { text: "your assay values" }))));

  const name = (c) => c.label || `Compound ${compounds.indexOf(c) + 1}`;

  async function showCompound(id, with3d) {
    selected = id;
    const c = compounds.find((x) => x.id === id);
    $("dock2d").replaceChildren(el("img", { class: "mol big", src: depictionUrl(projectId, c.id), alt: `2D structure of ${name(c)}` }),
      el("p", { class: "legend mono", text: `${name(c)}. ${c.canonical_smiles}` }));
    if (with3d) {
      const cap = $("viewer-cap");
      cap.textContent = "Loading the computed 3D conformer...";
      try {
        const conf = await authed(() => get(`/projects/${projectId}/compounds/${c.id}/conformer`));
        viewer.showMolblock(conf.molblock);
        cap.textContent = `${name(c)}: ${CONFORMER_LABEL}. (${conf.method})`;
      } catch (e) { cap.textContent = `3D view unavailable for ${name(c)}: ${describeError(e).message}`; }
    }
    renderRows(false);
    const row = document.querySelector(`tr[data-compound="${id}"]`);
    (with3d ? row?.querySelector("button") : row)?.focus({ preventScroll: true });
  }

  function renderRows(rebuild = true) {
    const order = compounds.slice();
    if (ranking) order.sort((a, b) => (ranking[a.id]?.blended_rank ?? 1e9) - (ranking[b.id]?.blended_rank ?? 1e9));
    const tbody = $("tbody");
    tbody.replaceChildren();
    for (const c of order) {
      const r = ranking?.[c.id];
      const tr = el("tr", { class: "sel-row", tabindex: 0, "aria-selected": String(c.id === selected), "data-compound": c.id,
        onclick: (ev) => { if (!ev.target.closest("a,button,input,select")) showCompound(c.id, false); },
        onkeydown: (ev) => { if ((ev.key === "Enter" || ev.key === " ") && ev.target === tr) { ev.preventDefault(); showCompound(c.id, false); } } });
      tr.append(el("td", { class: "nowrap", "data-testid": "rank" },
        r ? `${r.blended_rank} (${r.property_rank})` : "-",
        r && !r.passes_limits ? el("span", { class: "limit-note", "data-testid": "fail", text: "fail" }) : null,
        r && !r.passes_limits ? r.limit_failures.map((f) => el("span", { class: "limit-note", text: f.note })) : null));
      tr.append(el("td", {}, el("div", { class: "molcell nowrap-row" },
        el("img", { class: "mol", src: depictionUrl(projectId, c.id), alt: `2D structure of ${name(c)}` }),
        el("div", { class: "stackcol" },
          el("span", { class: "chip", text: name(c) }),
          el("button", { class: "btn small", type: "button", "aria-label": `Show 3D structure of ${name(c)}`, onclick: () => showCompound(c.id, true), text: "3D" })))));
      for (const e of eps) {
        const entry = predOf(c, e);
        if (!entry) { tr.append(el("td", { "data-missing": e, class: "muted", text: "not predicted" })); continue; }
        const p = entry.prediction, t = cellText(p);
        const lim = cfg.rows[e] && (cfg.rows[e].lmin != null || cfg.rows[e].lmax != null) ? { min: cfg.rows[e].lmin, max: cfg.rows[e].lmax } : null;
        tr.append(el("td", { "data-pred": e, "data-n": p.ensemble_n, "data-coverage": p.coverage, "data-sample-size": p.sample_size },
          el("div", { class: "val" },
            el("a", { href: `compound.html?p=${projectId}&c=${c.id}&e=${encodeURIComponent(e)}`, class: "mono", "aria-label": `${label(e)} for ${name(c)}: ${t.main}. Open uncertainty panel`, text: t.main }),
            el("span", { class: "conf", text: t.sub })),
          r && r.limit_failures.some((f) => f.endpoint === e) && lim ? el("span", { class: "limit-note", text: limitText(lim) }) : null,
          r && r.stale_endpoints.includes(e) ? el("span", { class: "limit-note", "data-testid": "stale-cell", text: "older model version" }) : null));
      }
      tr.append(measuredCell(c));
      tbody.append(tr);
    }
    const nfail = ranking ? Object.values(ranking).filter((r) => !r.passes_limits).length : 0;
    chips(nfail);
    renderBreakdown();
  }

  function measuredCell(c) {
    const endpoints = Object.keys(cards);
    return el("td", {},
      c.measurements.map((m) => el("div", { class: "legend mono", "data-measured": m.endpoint, text: `${m.endpoint}: ${m.value}${m.unit ? " " + m.unit : ""} (measured)` })),
      endpoints.length ? el("form", { class: "inline meas-form", method: "post", action: "#js-required", onsubmit: async (ev) => {
        ev.preventDefault();
        const f = ev.currentTarget, v = num(f.elements.value.value);
        if (v == null) { f.elements.value.focus(); return; }
        try {
          await authed(() => post(`/projects/${projectId}/measurements`, { compound_id: c.id, endpoint: f.elements.endpoint.value, value: v }));
          location.reload();
        } catch (e) { f.querySelector(".field-error")?.remove(); f.append(el("span", { class: "field-error", role: "alert", text: describeError(e).message })); }
      } },
        el("label", { class: "sr", for: `me-${c.id}`, text: `Measured endpoint for ${name(c)}` }),
        el("select", { id: `me-${c.id}`, name: "endpoint" }, endpoints.map((e) => el("option", { value: e, text: e }))),
        el("label", { class: "sr", for: `mv-${c.id}`, text: `Measured value for ${name(c)}` }),
        el("input", { id: `mv-${c.id}`, name: "value", class: "num", type: "text", inputmode: "decimal", placeholder: "value" }),
        el("button", { class: "btn small", type: "submit", text: "Add" })) : null);
  }

  function chips(nfail = 0) {
    const nodes = [el("span", { class: "chip", text: `${compounds.length} of 10 compounds` })];
    if (ranking) nodes.push(el("span", { class: "chip", text: `Uncertainty weight: ${cfg.lam.toFixed(2)}` }));
    const ns = [...new Set(compounds.flatMap((c) => c.predictions.map((x) => x.prediction.ensemble_n)))].sort((a, b) => a - b);
    if (ns.length) nodes.push(el("span", { class: "chip", text: `Ensemble N = ${ns.join(", ")}` }));
    if (ranking) nodes.push(el("span", { class: "chip", "data-testid": "nfail", text: `${nfail} fail a hard limit` }));
    if (stale) nodes.push(el("span", { class: "chip stale-flag", "data-testid": "stale", role: "status", text: "Some predictions come from an older model version. Re-run predictions before relying on this ranking." }));
    $("chips").replaceChildren(...nodes);
  }

  function renderBreakdown() {
    const r = ranking?.[selected];
    const c = compounds.find((x) => x.id === selected);
    const tb = document.querySelector("#bd-table tbody");
    if (!r) {
      $("bd-title").textContent = "Save a ranking to see the score breakdown.";
      $("stack").replaceChildren(); tb.replaceChildren(); $("formula-num").textContent = "";
      return;
    }
    $("bd-title").textContent = `${name(c)}: blended score R = ${r.blended_score.toFixed(3)} (property-only S = ${r.property_score.toFixed(3)}, confidence C = ${r.confidence_score.toFixed(3)})`;
    const parts = r.breakdown.map((b) => ({ ...b, share: b.blended_property_part + b.blended_confidence_part }));
    $("stack").replaceChildren(...parts.map((p, i) => el("i", { class: `seg-${i}`, pct: (p.share * 100).toFixed(2), title: `${p.endpoint}: ${p.share.toFixed(3)}` })));
    tb.replaceChildren(...parts.map((p, i) => el("tr", {},
      el("td", {}, el("span", { class: `swatch seg-${i}` }), p.endpoint),
      el("td", { class: "mono", text: p.desirability.toFixed(3) }), el("td", { class: "mono", text: p.confidence.toFixed(3) }), el("td", { class: "mono", text: p.share.toFixed(3) }))));
    $("formula-num").textContent = `R = (1 - ${cfg.lam.toFixed(2)}) x ${r.property_score.toFixed(3)} + ${cfg.lam.toFixed(2)} x ${r.confidence_score.toFixed(3)} = ${r.blended_score.toFixed(3)}`;
  }

  $("formula-text").textContent = "R_j = (1 - lam) S_j + lam C_j";
  renderRows();
  await showCompound(selected, false);
  if (saved.persisted && eps.length) await rank();
}
