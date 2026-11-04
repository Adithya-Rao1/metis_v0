// Export screen: GET /projects/{id}/export?format=csv|sdf|pdf (owner only; the server writes the disclaimer into every format).
import { el, qsId, renderChrome, say, showEmpty, showError, showLoading, stateBox } from "./chrome.js";
import { api, authed, describeError, get, requireSession, saveBlob } from "./api.js";

const $ = (id) => document.getElementById(id);
const user = await requireSession();
renderChrome({ current: "app.html", crumb: "Projects / Export", user });
const pid = qsId("p");
const page = $("page-state");

// RFC 4180 reader for the preview; the server writes the CSV.
function parseCsv(text) {
  const rows = []; let row = [], cur = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(cur); cur = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cur); rows.push(row); row = []; cur = ""; }
    else cur += c;
  }
  if (cur !== "" || row.length) { row.push(cur); rows.push(row); }
  return rows;
}

async function load() {
  if (!pid) {
    showEmpty(page, "Open a project first, then choose Export.");
    page.append(el("p", {}, el("a", { class: "btn primary", href: "app.html", text: "Go to projects" })));
    return;
  }
  showLoading(page, "Loading project");
  let project;
  try { project = await authed(() => get(`/projects/${pid}`)); } catch (e) { return showError(page, e, load); }
  page.replaceChildren();
  $("back").href = `app.html?p=${pid}`;
  $("export-frame").hidden = false;
  $("xname").textContent = project.name;
  document.querySelector(".crumb").textContent = `Projects / ${project.name} / Export`;
  for (const b of document.querySelectorAll("[data-dl]")) {
    b.addEventListener("click", async () => {
      const fmt = b.dataset.dl, msg = $("dl-msg");
      b.disabled = true; say(msg, `Preparing the ${fmt.toUpperCase()}...`, "ok");
      try {
        const r = await authed(() => api("GET", `/projects/${pid}/export`, { query: { format: fmt }, blob: true }));
        saveBlob(r.blob, r.filename || `metis_export.${fmt}`);
        say(msg, `Downloaded ${r.filename || fmt}.`, "ok");
      } catch (e) { say(msg, describeError(e).message, "error"); }
      finally { b.disabled = false; }
    });
  }
  if (!project.compounds.length) { showEmpty($("preview-state"), "This project has no compounds, so there is nothing to preview."); return; }
  showLoading($("preview-state"), "Loading the CSV preview");
  try {
    const text = await authed(() => api("GET", `/projects/${pid}/export`, { query: { format: "csv" }, text: true }));
    const lines = text.split("\n");
    const k = lines.findIndex((l) => !l.startsWith("#"));
    const preamble = lines.slice(0, k < 0 ? lines.length : k).map((l) => l.replace(/^#\s?/, ""));
    const rows = parseCsv(lines.slice(k < 0 ? lines.length : k).join("\n")).filter((r) => r.length > 1 || (r[0] || "") !== "");
    $("preview-state").replaceChildren(...(preamble.length ? [el("p", { class: "legend", "data-testid": "csv-preamble", text: preamble.join(" ") })] : []));
    $("preview-wrap").hidden = false;
    $("csv-prev").tHead.replaceChildren(el("tr", {}, rows[0].map((h) => el("th", { scope: "col", text: h }))));
    $("csv-prev").tBodies[0].replaceChildren(...rows.slice(1, 6).map((r) => el("tr", {}, r.map((c) => el("td", { class: "mono", text: c })))));
  } catch (e) { showError($("preview-state"), e, load); }
}
load();
