// Home page: spinning 1M17 EGFR + erlotinib complex. Pauses when the tab is hidden; no spin under reduced motion.
import { webglAvailable } from "./viewer.js";

const box = document.getElementById("mol3d");
const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function fallback() {
  box.dataset.webgl = "off";
  box.replaceChildren();
  const f = document.createElement("div"); f.className = "fallback";
  const img = document.createElement("img"); img.src = "assets/erlotinib.svg"; img.alt = "2D structure of erlotinib";
  const p = document.createElement("span"); p.textContent = "3D view unavailable (WebGL not supported).";
  f.append(img, p); box.append(f);
}

async function main() {
  if (!webglAvailable() || typeof window.$3Dmol === "undefined") return fallback();
  const [pdb, sdf] = await Promise.all([
    fetch("fixtures/structures/1m17_chainA_pocket.pdb").then((r) => r.text()),
    fetch("fixtures/structures/erlotinib_1m17.sdf").then((r) => r.text()),
  ]);
  const meta = await fetch("fixtures/structures/1m17_meta.json").then((r) => r.json());
  box.dataset.webgl = "on";
  const v = window.$3Dmol.createViewer(box, { backgroundColor: "#14181c", antialias: true });
  v.addModel(pdb, "pdb");
  v.setStyle({}, { cartoon: { color: "#98a3ae", opacity: 0.85 } });
  v.setStyle({ resi: meta.pocket_shell_residues }, { cartoon: { color: "#98a3ae", opacity: 0.85 }, stick: { radius: 0.09, colorscheme: "Jmol" } });
  v.addModel(sdf, "sdf");
  v.setStyle({ model: 1 }, { stick: { radius: 0.22, colorscheme: "greenCarbon" }, sphere: { scale: 0.25, colorscheme: "greenCarbon" } });
  v.zoomTo({ model: 1 }); v.zoom(0.32); v.render();

  let raf = null, frames = 0, last = 0;
  const tick = (t) => {
    if (t - last > 33) { v.rotate(0.6, "y"); v.render(); frames++; last = t; box.dataset.frames = String(frames); }
    raf = requestAnimationFrame(tick);
  };
  const start = () => { if (!reduce && !raf && !document.hidden) { raf = requestAnimationFrame(tick); box.dataset.spin = "on"; } };
  const stop = () => { if (raf) cancelAnimationFrame(raf); raf = null; box.dataset.spin = "off"; };
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();
}
main().catch(fallback);
