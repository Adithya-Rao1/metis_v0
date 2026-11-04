// Docking preparation: POST /dockprep/proteins, /dockprep/sites, /dockprep/ligands. Stateless on the server; downloads are built here.
import { el, qs, renderChrome, say, stateBox } from "./chrome.js";
import { api, authed, b64ToBlob, bytesToB64, depictionUrl, describeError, get, post, requireSession, saveBlob } from "./api.js";
import { SharedViewer } from "./viewer.js";
import { num } from "./format.js";

const $ = (id) => document.getElementById(id);
const user = await requireSession();
renderChrome({ current: "docking.html", crumb: "Docking prep", user });
$("dock-frame").hidden = false;
const viewer = new SharedViewer($("viewer"));

const NOTES = { vina: "AutoDock Vina: protein and ligands as PDBQT; box parameters as a Vina config.", gnina: "GNINA: protein as PDBQT or PDB, ligands as SDF; box parameters as a GNINA config." };
const eng = () => document.querySelector('input[name="eng"]:checked').value;
const setNote = () => ($("eng-note").textContent = NOTES[eng()]);
document.querySelectorAll('input[name="eng"]').forEach((r) => r.addEventListener("change", setNote)); setNote();
const src = () => document.querySelector('input[name="src"]:checked').value;
document.querySelectorAll('input[name="src"]').forEach((r) => r.addEventListener("change", () => { $("up").disabled = src() !== "upload"; $("pdbid").disabled = src() !== "pdb"; }));
if (qs("pdb")) $("pdbid").value = qs("pdb").slice(0, 8);

// ---- ligand picker: a project compound gets a 2D depiction ----
let projectDetail = null;
(async () => {
  try {
    const { projects } = await authed(() => get("/projects"));
    $("lig-project").append(...projects.map((p) => el("option", { value: p.id, text: `${p.name} (${p.compound_count})` })));
  } catch (e) { /* the ligand picker is optional */ }
})();
$("lig-project").addEventListener("change", async (e) => {
  const sel = $("lig-compound"), id = e.target.value;
  sel.replaceChildren(); $("lig2d").replaceChildren(); projectDetail = null;
  if (!id) { sel.disabled = true; return; }
  try {
    projectDetail = await authed(() => get(`/projects/${id}`));
    sel.replaceChildren(...projectDetail.compounds.map((c, i) => el("option", { value: c.id, text: c.label || `Compound ${i + 1}` })));
    sel.disabled = !projectDetail.compounds.length;
    show2d();
  } catch (err) { say($("prep-msg"), describeError(err).message, "error"); }
});
$("lig-compound").addEventListener("change", show2d);
function show2d() {
  const id = $("lig-compound").value;
  if (!projectDetail || !id) return;
  $("lig2d").replaceChildren(el("img", { class: "mol", src: depictionUrl(projectDetail.id, id), alt: "2D structure of the selected ligand" }));
}
const ligandSmiles = () => {
  const typed = $("lig-smiles").value.trim();
  if (typed) return typed;
  const c = projectDetail && projectDetail.compounds.find((x) => x.id === $("lig-compound").value);
  return c ? c.canonical_smiles : null;
};

// ---- run ----
const list = (v) => v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
let state = null; // {structure, protein, sites, ligand, bodies}

async function structureInput() {
  if (src() === "pdb") {
    const id = $("pdbid").value.trim().toUpperCase();
    if (!/^[0-9][A-Z0-9]{3}$/.test(id)) throw new Error("Enter a 4-character PDB ID such as 1M17, or upload a file.");
    return { pdb_id: id };
  }
  const f = $("up").files[0];
  if (!f) throw new Error("Choose a protein file to upload.");
  if (f.size > 15 * 1024 * 1024) throw new Error("That file is larger than the 15 MB limit.");
  return { file_b64: bytesToB64(await f.arrayBuffer()), filename: f.name };
}

function siteBody(structure, configSite) {
  const modes = [...document.querySelectorAll('input[name="mode"]:checked')].map((m) => m.value);
  if (!modes.length) throw new Error("Choose at least one way to define the binding site.");
  const body = { ...structure, modes, engine: eng(), padding: num($("padding").value) ?? 4, receptor_file: "receptor.pdbqt", ligand_file: eng() === "vina" ? "ligand.pdbqt" : "ligand.sdf" };
  if (modes.includes("ligand")) { if (!$("ligand-id").value.trim()) throw new Error("Enter the co-crystal ligand residue, for example A:999."); body.ligand_id = $("ligand-id").value.trim(); }
  if (modes.includes("residues")) { const r = list($("residues").value); if (!r.length) throw new Error("Enter residues such as A:790, A:793."); body.residues = r; }
  if (modes.includes("box")) {
    const c = ["bx", "by", "bz"].map((i) => num($(i).value)), s = ["sx", "sy", "sz"].map((i) => num($(i).value));
    if ([...c, ...s].some((x) => x == null)) throw new Error("Enter the box center and size (six numbers).");
    body.box = { center: c, size: s };
  }
  const ch = list($("chains").value);
  if (ch.length) body.chains = ch;
  if (configSite) body.config_site = configSite;
  return body;
}
function proteinBody(structure) {
  const body = { ...structure, remove_waters: $("waters").checked, remove_heteroatoms: $("hets").checked, keep_hets: list($("keep-hets").value),
    add_hydrogens: $("hyd").checked, ph: num($("ph").value) ?? 7.4 };
  const ch = list($("chains").value);
  if (ch.length) body.chains = ch;
  return body;
}

$("dock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("prep-msg"), btn = $("prep");
  btn.disabled = true; say(msg, "Preparing... this can take a little while.", "ok");
  $("result").replaceChildren(stateBox("loading", "Preparing the protein and binding site..."));
  try {
    const structure = await structureInput();
    const bodies = { protein: proteinBody(structure), sites: siteBody(structure, null) };
    const smi = ligandSmiles();
    if (smi) bodies.ligand = { smiles: smi, name: "ligand", n_conformers: 10 };
    const protein = await authed(() => post("/dockprep/proteins", bodies.protein));
    const sites = await authed(() => post("/dockprep/sites", bodies.sites));
    const ligand = bodies.ligand ? await authed(() => post("/dockprep/ligands", bodies.ligand)) : null;
    state = { structure, protein, sites, ligand, bodies, chosen: sites.report.config_site || (sites.sites[0] && sites.sites[0].id) || null };
    say(msg, "Done. Review the change log, then download.", "ok");
    render();
  } catch (err) {
    const text = err.status === undefined ? err.message : describeError(err).message;
    say(msg, text, "error");
    $("result").replaceChildren(stateBox("error", text));
  } finally { btn.disabled = false; }
});

async function chooseSite(id) {
  const msg = $("prep-msg");
  say(msg, "Updating the config for that site...", "ok");
  try {
    state.sites = await authed(() => post("/dockprep/sites", siteBody(state.structure, id)));
    state.chosen = id;
    say(msg, "Config updated.", "ok");
    render();
  } catch (err) { say(msg, err.status === undefined ? err.message : describeError(err).message, "error"); }
}

const decode = (f) => atob(f.content_b64);
function fileButtons(label, files) {
  return Object.entries(files || {}).map(([name, f]) => el("button", { class: "btn small", type: "button", "data-file": name, "aria-label": `Download ${name} (${label})`, text: name,
    onclick: () => saveBlob(b64ToBlob(f.content_b64, "text/plain"), name) }));
}
async function zip(kind) {
  const path = { protein: "/dockprep/proteins", sites: "/dockprep/sites", ligand: "/dockprep/ligands" }[kind];
  const body = kind === "sites" ? siteBody(state.structure, state.chosen) : state.bodies[kind];
  try {
    const r = await authed(() => api("POST", path, { json: body, query: { bundle: "zip" }, blob: true }));
    saveBlob(r.blob, r.filename || `metis_dockprep_${kind}.zip`);
  } catch (err) { say($("prep-msg"), describeError(err).message, "error"); }
}

function parseResidue(s) { const m = /^([A-Za-z0-9]):(-?\d+)/.exec(s); return m ? { chain: m[1], resi: Number(m[2]) } : null; }

function render() {
  const { protein, sites, ligand } = state;
  const rpt = protein.report, site = sites.sites.find((s) => s.id === state.chosen) || sites.sites[0] || null;
  // 3D: prepared protein (PDB), the chosen box and site residues, the prepared ligand if any
  const pdbFile = protein.files["receptor.pdb"];
  const sdfName = ligand && Object.keys(ligand.files).find((n) => n.endsWith(".sdf"));
  if (pdbFile && viewer.ok) {
    viewer.showDocking({ pdb: decode(pdbFile), sdf: sdfName ? decode(ligand.files[sdfName]) : null, box: site ? { center: site.center, size: site.size } : null,
      siteResidues: site ? site.residues.map(parseResidue).filter(Boolean) : [] });
    $("viewer-cap").textContent = `Prepared protein${rpt.pdb_id ? " " + rpt.pdb_id : ""} (experimental coordinates, changes listed below), search box${sdfName ? " and the prepared ligand (a computed conformer, not an experimental structure)" : ""}.`;
  }
  const log = [...protein.change_log, ...sites.change_log, ...(ligand ? ligand.change_log : [])];
  const out = [];
  out.push(el("h3", { text: "Protein" }),
    el("div", { class: "kv", "data-testid": "protein-report" },
      el("span", { text: "Entry" }), el("span", { class: "mono", text: rpt.pdb_id || "uploaded file" }),
      el("span", { text: "Method" }), el("span", { text: `${rpt.experimental_method || "unknown"}${rpt.resolution_angstrom ? `, ${rpt.resolution_angstrom} A resolution` : ""}` }),
      el("span", { text: "Chains" }), el("span", { class: "mono", text: `${rpt.chains_selected.join(", ")} of ${rpt.chains_available.join(", ")}` }),
      el("span", { text: "Waters removed" }), el("span", { class: "mono", text: String(rpt.n_waters_removed) }),
      el("span", { text: "Co-crystal ligands" }), el("span", { class: "mono", text: rpt.cocrystal_ligands.map((l) => `${l.resname} ${l.residue}`).join(", ") || "none" })));
  if (rpt.method_note) out.push(el("p", { class: "note-box", text: rpt.method_note }));
  out.push(el("h3", { text: "Binding site" }));
  if (!sites.sites.length) out.push(stateBox("empty", "No binding site was defined."));
  else {
    out.push(el("div", { class: "tablewrap" }, el("table", { id: "sites" },
      el("thead", {}, el("tr", {}, ["Use", "Source", "Site", "Score"].map((h) => el("th", { scope: "col", text: h })))),
      el("tbody", {}, sites.sites.map((s) => el("tr", { "data-site": s.id },
        el("td", {}, el("input", { type: "radio", name: "site", "aria-label": `Use ${s.label}`, checked: s.id === state.chosen, onchange: () => chooseSite(s.id) })),
        el("td", { text: s.method }), el("td", { text: s.label }), el("td", { class: "mono", text: s.score == null ? "none" : String(s.score) }))))),
      ),
    el("div", { class: "kv mt8" }, el("span", { text: "Box center (A)" }), el("span", { class: "mono", id: "bc", text: site.center.join(", ") }),
      el("span", { text: "Box size (A)" }), el("span", { class: "mono", id: "bs", text: site.size.join(", ") })),
    el("p", { class: "legend", text: "Site confidence is not scored; where several methods ran, agreement between them is in the report file." }));
  }
  for (const n of sites.notes || []) out.push(el("p", { class: "note-box", text: String(n) }));
  out.push(el("div", { class: "note-box mt8" }, el("b", { text: "Change log" }), " (downloaded with the files): ",
    el("ul", { id: "log", class: "loglist" }, log.map((x) => el("li", { text: `[${x.step}] ${x.action}${x.target ? " " + x.target : ""}${x.detail ? ": " + x.detail : ""}` })))));
  out.push(el("h3", { text: "Download" }),
    el("div", { class: "toolbar flush", "data-testid": "downloads" },
      fileButtons("protein", protein.files), fileButtons("binding site", sites.files), ligand ? fileButtons("ligand", ligand.files) : null,
      el("button", { class: "btn small", type: "button", id: "dl-log", text: "change_log.txt", onclick: () => saveBlob(new Blob([protein.change_log_text, "\n", sites.change_log_text, ligand ? "\n" + ligand.change_log_text : ""], { type: "text/plain" }), "change_log.txt") })),
    el("div", { class: "toolbar flush" }, ["protein", "sites", ligand ? "ligand" : null].filter(Boolean).map((k) =>
      el("button", { class: "btn small", type: "button", "data-zip": k, text: `${k === "sites" ? "Site config" : k[0].toUpperCase() + k.slice(1)} bundle (zip)`, onclick: () => zip(k) }))));
  $("result").replaceChildren(...out);
}
