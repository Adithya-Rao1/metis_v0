// One shared 3Dmol viewer per page (browsers cap WebGL contexts). Falls back to a notice when WebGL is missing.
export const CONFORMER_LABEL = "computed conformer, not an experimental structure";

export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl") || c.getContext("experimental-webgl"));
  } catch (e) { return false; }
}

export class SharedViewer {
  constructor(container) {
    this.container = container;
    this.ok = webglAvailable() && typeof window.$3Dmol !== "undefined";
    this.v = null;
    if (!this.ok) {
      container.replaceChildren();
      const n = document.createElement("div");
      n.className = "gl-notice";
      n.setAttribute("role", "status");
      n.dataset.testid = "webgl-notice";
      n.textContent = "3D view unavailable: WebGL is not supported or is disabled in this browser. The 2D structure is still shown.";
      container.append(n);
      container.dataset.webgl = "off";
      return;
    }
    container.dataset.webgl = "on";
    this.v = window.$3Dmol.createViewer(container, { backgroundColor: "#1b2026", antialias: true });
  }
  _done() { this.v.zoomTo(); this.v.render(); this.container.dataset.shown = "1"; }
  showMolblock(molblock, highlight = []) {
    if (!this.ok) return;
    const v = this.v;
    v.removeAllModels(); v.removeAllShapes(); v.removeAllLabels();
    v.addModel(molblock, "sdf");
    v.setStyle({}, { stick: { radius: 0.14, colorscheme: "Jmol" }, sphere: { scale: 0.22, colorscheme: "Jmol" } });
    if (highlight.length) v.setStyle({ index: highlight }, { stick: { radius: 0.2, color: "#6aa5e6" }, sphere: { scale: 0.3, color: "#6aa5e6" } });
    this._done();
  }
  showDocking({ pdb, sdf, shell, box, siteResidues = [] }) {
    if (!this.ok) return;
    const v = this.v;
    v.removeAllModels(); v.removeAllShapes();
    const p = v.addModel(pdb, "pdb");
    v.setStyle({ model: 0 }, { cartoon: { color: "#98a3ae", opacity: 0.8 } });
    v.setStyle({ model: 0, resi: shell }, { stick: { radius: 0.1, colorscheme: "Jmol" } });
    if (siteResidues.length) v.setStyle({ model: 0, resi: siteResidues }, { stick: { radius: 0.18, color: "#d6b658" } });
    v.addModel(sdf, "sdf");
    v.setStyle({ model: 1 }, { stick: { radius: 0.2, colorscheme: "greenCarbon" } });
    if (box) { const [cx, cy, cz] = box.center, [w, h, d] = box.size;
    v.addBox({ center: { x: cx, y: cy, z: cz }, dimensions: { w, h, d }, color: "#6aa5e6", wireframe: true, alpha: 1 }); }
    v.zoomTo({ model: 1 }); v.zoom(0.55); v.render();
    this.container.dataset.shown = "1";
    return p;
  }
}
