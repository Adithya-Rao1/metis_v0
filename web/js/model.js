// Model and data page, from GET /models (public). Nothing is invented: if no model is published the page says so.
import { el, qs, renderChrome, showEmpty, showError, showLoading } from "./chrome.js";
import { get } from "./api.js";

const $ = (id) => document.getElementById(id);
let user = null;
try { user = await get("/auth/me"); } catch (e) { user = null; }
renderChrome({ current: "model.html", crumb: "Models", user });
const rep = $("report");

async function load() {
  showLoading(rep, "Loading models");
  let data;
  try { data = await get("/models"); } catch (e) { return showError(rep, e, load); }
  const models = data.models;
  if (!models.length) {
    $("picker").replaceChildren();
    showEmpty(rep, "No models have been published yet. Model pages appear here once a trained, benchmarked and calibrated model is released. Nothing on this page is a placeholder.");
    rep.firstChild.id = "no-models";
    return;
  }
  const want = qs("e");
  const m = models.find((x) => x.endpoint === want) || models[0];
  document.title = `${m.endpoint} model | Metis`;
  $("h").textContent = `Model and data page (${m.endpoint})`;
  document.querySelector(".crumb").textContent = `Models / ${m.endpoint}`;
  $("picker").replaceChildren(...models.map((x) =>
    el("a", { class: "chip", href: `model.html?e=${encodeURIComponent(x.endpoint)}`, ...(x.endpoint === m.endpoint ? { "aria-current": "page" } : {}), text: x.endpoint })));
  render(m);
}

const fmt = (v) => (typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toPrecision(4)) : typeof v === "boolean" ? (v ? "yes" : "no") : v == null ? "n/a" : String(v));
const safeUrl = (u) => { try { const x = new URL(u); return x.protocol === "https:" || x.protocol === "http:" ? x.href : null; } catch (e) { return null; } };

const isRecord = (v) => v && typeof v === "object" && !Array.isArray(v);
// An array of objects (reliability bins, coverage by similarity bin) becomes a small table: one row per element, one column per key.
// Cells are text only (textContent), so nothing from the card can become markup.
function miniTable(path, rows) {
  const keys = [...new Set(rows.flatMap((r) => (isRecord(r) ? Object.keys(r) : [])))];
  return el("table", { class: "subtable" }, el("caption", { class: "legend", text: `${path.replace(/_/g, " ")} (${rows.length} rows)` }),
    el("thead", {}, el("tr", {}, keys.map((k) => el("th", { scope: "col", text: k.replace(/_/g, " ") })))),
    el("tbody", {}, rows.map((r) => el("tr", {}, keys.map((k) => el("td", { class: "mono", text: r[k] != null && typeof r[k] === "object" ? JSON.stringify(r[k]) : fmt(r[k]) }))))));
}
// Flatten nested JSON (benchmark metrics) into [path, value] rows, preserving every key. Value is text, or a table node for arrays of objects.
function flatten(obj, prefix = "") {
  const out = [];
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (isRecord(v)) out.push(...flatten(v, path));
    else if (Array.isArray(v) && v.length && v.every(isRecord)) out.push([path, miniTable(path, v)]);
    else out.push([path, Array.isArray(v) ? v.map((x) => (isRecord(x) ? JSON.stringify(x) : fmt(x))).join(", ") : fmt(v)]);
  }
  return out;
}
const tbl = (cap, heads, rows, id) => el("div", { class: "tablewrap" }, el("table", { id }, el("caption", { text: cap }),
  el("thead", {}, el("tr", {}, heads.map((x) => el("th", { scope: "col", text: x })))),
  el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => (c && c.nodeType ? el("td", {}, c) : el("td", { class: "mono", text: String(c) }))))))));

function render(m) {
  const ns = Object.keys(m.coverage_by_n).sort((a, b) => Number(a) - Number(b));
  const keys = [...new Set(m.datasets.flatMap((d) => Object.keys(d)))];
  rep.replaceChildren(
    el("div", { class: "rgrid" },
      el("div", { class: "rcard" }, el("span", { class: "t", text: "Endpoint and version" }), el("span", { class: "n", text: m.endpoint }), el("span", { class: "legend", text: `version ${m.version} | ${m.kind}${m.units ? " | " + m.units : ""}` })),
      typeof m.higher_is_better === "boolean" ? el("div", { class: "rcard", "data-testid": "direction" }, el("span", { class: "t", text: "Better when the value is" }), el("span", { class: "n", text: m.higher_is_better ? "higher" : "lower" }), el("span", { class: "legend", text: "Used as the default direction when ranking; you can change it per project." })) : null,
      el("div", { class: "rcard", "data-testid": "split" }, el("span", { class: "t", text: "Split used for evaluation" }), el("span", { class: "n", text: m.split_type })),
      el("div", { class: "rcard", "data-testid": "default-range" }, el("span", { class: "t", text: "Default ranking range (training data min to max)" }), el("span", { class: "n mono", text: `${fmt(m.default_range[0])} to ${fmt(m.default_range[1])}` }))),
    el("h2", { id: "datasets", text: "Training datasets and licences" }),
    tbl("Each dataset with its stated licence, size and split.", keys, m.datasets.map((d) => keys.map((k) => {
      const v = d[k];
      if (k === "url" || k === "source_url") { const h = safeUrl(v); return h ? el("a", { href: h, rel: "noopener noreferrer", target: "_blank", text: String(v) }) : fmt(v); }
      return fmt(v);
    })), "datasets-table"),
    el("h2", { id: "how-computed", text: "Benchmark against baselines" }),
    flatten(m.benchmark).length ? tbl(`Held-out ${m.split_type} split. Every metric the publisher recorded is listed; if the model does not beat its baseline, this table shows that.`, ["Metric", "Value"], flatten(m.benchmark), "benchmark-table") : el("p", { class: "legend", text: "No benchmark metrics were recorded for this model." }),
    el("h2", { id: "coverage", text: "Coverage and runtime at each ensemble size" }),
    tbl("Empirical interval coverage on held-out data, median calibration width where applicable, and time per compound at each N offered.", ["Ensemble size N", "Held-out coverage", "Median calibration width", "Runtime (s)"],
      ns.map((n) => [n, fmt(m.coverage_by_n[n]), m.median_calibration_width_by_n?.[n] != null ? fmt(m.median_calibration_width_by_n[n]) : "n/a", m.runtime_by_n_seconds[n] != null ? fmt(m.runtime_by_n_seconds[n]) : "n/a"]), "coverage-table"),
    el("div", { class: "note-box", id: "limitations" }, el("b", { text: "Known limitations" }), el("br"), m.limitations),
    el("p", { class: "legend", "data-testid": "weights-hash", text: `Model weights sha256: ${m.weights_sha256}` }));
}
load();
