import { el, loadJSON, renderChrome } from "./chrome.js";
import { SharedViewer } from "./viewer.js";

renderChrome({ current: "docking.html", crumb: "Docking prep" });
const dock = await loadJSON("fixtures/docking.json");
const [pdb, sdf] = await Promise.all([
  fetch("fixtures/structures/1m17_chainA_pocket.pdb").then((r) => r.text()),
  fetch("fixtures/structures/erlotinib_1m17.sdf").then((r) => r.text()),
]);
const viewer = new SharedViewer(document.getElementById("viewer"));
let site = dock.sites[1];
const draw = () => viewer.showDocking({ pdb, sdf, shell: dock.pocket_shell_residues, box: dock.box, siteResidues: site.residues });
draw();

document.getElementById("bc").textContent = dock.box.center.join(", ");
document.getElementById("bs").textContent = dock.box.size.join(", ");
document.getElementById("fixnote").textContent = dock.note;
document.getElementById("log").replaceChildren(...dock.change_log.map((t) => el("li", { text: t })));
document.getElementById("pdbinfo").replaceChildren(
  el("div", {}, el("span", { class: "chip", text: dock.method }), ` resolution ${dock.resolution}`),
  el("div", { class: "muted", text: `${dock.title}. ${dock.other_methods[0]}.` }));

const tb = document.querySelector("#sites tbody");
dock.sites.forEach((s, i) => tb.append(el("tr", {},
  el("td", {}, el("input", { type: "radio", name: "site", "aria-label": `Use the ${s.source.toLowerCase()} site`, checked: i === 1, onchange: () => { site = s; draw(); } })),
  el("td", { text: s.source }), el("td", { text: s.site }), el("td", { class: "mono", text: s.score }))));

const notes = { vina: "AutoDock Vina: protein and ligands as PDBQT; box parameters as a Vina config.", gnina: "GNINA: protein as PDBQT or PDB, ligands as SDF; box parameters as a GNINA config." };
const eng = () => document.querySelector('input[name="eng"]:checked').value;
const setNote = () => (document.getElementById("eng-note").textContent = notes[eng()]);
document.querySelectorAll('input[name="eng"]').forEach((r) => r.addEventListener("change", setNote)); setNote();

document.getElementById("pdbid").addEventListener("input", (e) => {
  const v = e.target.value.trim().toUpperCase();
  document.getElementById("pdbinfo").lastChild.textContent = v === "1M17" ? `${dock.title}. ${dock.other_methods[0]}.` : `Fixture mode offers 1M17 only; ${v || "this ID"} is not available.`;
});
function save(name, text) {
  const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: name });
  document.body.append(a); a.click(); a.remove();
}
document.getElementById("dl-box").addEventListener("click", () => {
  const [x, y, z] = dock.box.center, [sx, sy, sz] = dock.box.size;
  save(`${eng()}_box.txt`, `# FIXTURE box parameters (${eng()})\ncenter_x = ${x}\ncenter_y = ${y}\ncenter_z = ${z}\nsize_x = ${sx}\nsize_y = ${sy}\nsize_z = ${sz}\n`);
});
document.getElementById("dl-log").addEventListener("click", () => save("change_log.txt", "FIXTURE change log\n" + dock.change_log.join("\n") + "\n"));
