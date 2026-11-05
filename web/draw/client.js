// Page side of the Ketcher bridge. The editor runs in <iframe sandbox="allow-scripts"> (opaque origin: no cookies, no storage,
// no parent DOM, no credentialed API calls). The only channel is postMessage; this module is the only place that sends or
// receives it, and it accepts a message only from that iframe's window with the opaque origin "null" and a known shape.
const TIMEOUT_MS = 15000;

export function openDrawBridge(iframe, frameUrl) {
  const pageOrigin = location.origin;
  if (!pageOrigin || pageOrigin === "null") throw new Error("The structure editor needs the site to be served over http or https.");
  const pending = new Map();
  let nextId = 1, isReady = false, onReady = null;
  const ready = new Promise((res) => { onReady = res; });

  window.addEventListener("message", (ev) => {
    if (ev.source !== iframe.contentWindow || ev.origin !== "null") return;
    const d = ev.data;
    if (!d || typeof d !== "object" || d.metis_draw !== 1) return;
    if (d.ready === true) { isReady = true; onReady(); return; }
    const p = typeof d.id === "number" ? pending.get(d.id) : null;
    if (!p) return;
    pending.delete(d.id);
    clearTimeout(p.timer);
    if (typeof d.error === "string") p.reject(new Error(d.error));
    else p.resolve(typeof d.result === "string" ? d.result : "");
  });

  function call(cmd, arg) {
    if (!isReady) return Promise.reject(new Error("The structure editor is still loading. Wait a moment and try again."));
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error("The structure editor did not answer.")); }, TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
      // targetOrigin must be "*": a sandboxed frame has an opaque origin that cannot be named. The payload is one SMILES string,
      // and only iframe.contentWindow (the window we created) receives it.
      iframe.contentWindow.postMessage({ metis_draw: 1, id, cmd, arg }, "*");
    });
  }
  iframe.setAttribute("src", `${frameUrl}?o=${encodeURIComponent(pageOrigin)}`);
  return { ready, isReady: () => isReady, getSmiles: () => call("getSmiles"), setMolecule: (smiles) => call("setMolecule", String(smiles)) };
}
