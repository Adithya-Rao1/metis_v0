// Upload box: SMILES / MOL / SDF -> POST /lead/api/molecules/validate. (Creating and streaming the run is stages.js startRun.)
import { bus } from "./bus.js";
import { store } from "./state.js";
import { firstMolRecord } from "./molparse.js";
import { lead } from "./net.js";

const MAX_FILE_BYTES = 1_000_000;

export function mountUpload({ onPreview, onStatus }) {
  const $ = (id) => document.getElementById(id);
  const smiles = $("smiles"), msg = $("validate-msg"), info = $("valid-info"), runBtn = $("btn-run"), checkBtn = $("btn-validate");
  const textOf = (tag, cls, s) => { const e = document.createElement(tag); if (cls) e.className = cls; e.textContent = s; return e; };
  const drop = $("drop"), file = $("file");
  let validated = null; // {canonical_smiles, molblock, ...}
  let request = null; // what was validated: {smiles} | {molfile}

  const showMsg = (text, kind = "") => {
    msg.hidden = !text;
    msg.textContent = text || "";
    msg.className = "msg" + (kind ? " " + kind : "");
  };

  function invalidate() {
    validated = null;
    runBtn.disabled = true;
    info.hidden = true;
  }
  smiles.addEventListener("input", () => { request = null; invalidate(); showMsg(""); });
  smiles.addEventListener("keydown", (e) => { if (e.key === "Enter") check(); });

  async function check() {
    const text = smiles.value.trim();
    if (!request && !text) return showMsg("Enter a SMILES string or drop a MOL / SDF file.");
    const body = request || { smiles: text };
    invalidate();
    showMsg("Checking molecule…", "info");
    checkBtn.disabled = true;
    store.status = "validating";
    try {
      const data = await lead("POST", "/api/molecules/validate", { json: body });
      if (!data?.ok) throw new Error(data?.error || "Molecule rejected by the server.");
      validated = data;
      showMsg("");
      const parts = [document.createTextNode("Canonical SMILES"), document.createElement("br"), textOf("code", "", data.canonical_smiles), document.createElement("br"),
        document.createTextNode("Heavy atoms "), textOf("b", "", String(data.heavy_atoms ?? "?"))];
      if (data.score) parts.push(document.createTextNode(" · QED "), textOf("b", "", data.score.descriptors.qed.toFixed(2)), document.createTextNode(" · alerts "), textOf("b", "", String(data.score.descriptors.alerts)));
      info.replaceChildren(...parts);
      info.hidden = false;
      runBtn.disabled = false;
      store.status = "ready";
      onPreview(data, request);
    } catch (err) {
      store.status = "idle";
      showMsg(err.message || String(err));
      onStatus?.("Molecule not accepted");
    } finally {
      checkBtn.disabled = false;
    }
  }
  checkBtn.onclick = check;

  runBtn.disabled = true; // main.js wires the run button (stages.js startRun)
  $("cfg-guid").oninput = (e) => { $("cfg-guid-out").textContent = Number(e.target.value).toFixed(2); };

  // ------------------------------------------------------------------ file drop / pick
  async function takeFile(f) {
    if (!f) return;
    if (f.size > MAX_FILE_BYTES) return showMsg(`File is too large (${(f.size / 1e6).toFixed(1)} MB, limit 1 MB).`);
    if (!/\.(mol|sdf|sd|mdl)$/i.test(f.name)) return showMsg("Only .mol and .sdf files are supported (or paste a SMILES).");
    const text = await f.text();
    const { molfile, multiple } = firstMolRecord(text);
    if (!/M {2}END/.test(molfile)) return showMsg("This file does not look like a MOL/SDF record (no “M  END” line).");
    request = { molfile };
    smiles.value = "";
    smiles.placeholder = `${f.name}${multiple ? " (first record)" : ""}`;
    await check();
    if (multiple) showMsg("The file has several records; only the first one is used.", "info");
  }
  drop.onclick = () => file.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") file.click(); };
  file.onchange = () => takeFile(file.files[0]);
  for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", (e) => takeFile(e.dataTransfer.files[0]));
  // dropping anywhere on the page works too
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => { e.preventDefault(); if (e.target !== drop && e.dataTransfer?.files?.length) takeFile(e.dataTransfer.files[0]); });

  return { check, setSmiles: (s) => { smiles.value = s; request = null; invalidate(); }, showMsg };
}
