// Lead Studio draws a whole protein in WebGL on every frame; on a 3x display that is 9 pixels per CSS pixel. This page therefore reports a
// devicePixelRatio of at most 1.5 to everything running in it (3Dmol sizes its canvas from it, and so do our overlay canvases, which keeps
// them aligned). Import this module before the viewer is created. Layout is unaffected: only the canvas backing resolution is.
export const DPR_CAP = 1.5;

const d = Object.getOwnPropertyDescriptor(window, "devicePixelRatio");
if (d && typeof d.get === "function" && d.configurable) {
  Object.defineProperty(window, "devicePixelRatio", {
    configurable: true, enumerable: d.enumerable,
    get() { return Math.min(d.get.call(window) || 1, DPR_CAP); },
  });
}
