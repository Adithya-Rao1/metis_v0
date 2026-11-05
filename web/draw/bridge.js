// Runs INSIDE the sandboxed Ketcher frame (opaque origin). It exposes exactly two operations to the embedding Metis page
// over postMessage: getSmiles() and setMolecule(smiles). Every incoming message is checked: it must come from the parent window,
// from the origin the parent announced in the frame URL (?o=), and be a well-formed request from a fixed allow-list.
(() => {
  // A sandboxed document without allow-same-origin has no web storage: touching window.localStorage throws, and Ketcher reads it at
  // start-up. This script is loaded BEFORE the bundle and gives it in-memory stand-ins (nothing persists, nothing is shared).
  for (const name of ["localStorage", "sessionStorage"]) {
    try { void window[name]; continue; } catch (e) { /* blocked by the sandbox: install a stand-in */ }
    const data = new Map();
    const mem = {
      get length() { return data.size; },
      key: (i) => [...data.keys()][i] ?? null,
      getItem: (k) => (data.has(String(k)) ? data.get(String(k)) : null),
      setItem: (k, v) => { data.set(String(k), String(v)); },
      removeItem: (k) => { data.delete(String(k)); },
      clear: () => data.clear(),
    };
    Object.defineProperty(window, name, { configurable: true, get: () => mem });
  }
  const MAX = 100000;
  let parentOrigin = null;
  try {
    const o = new URLSearchParams(location.search).get("o");
    const u = new URL(o);
    if (u.origin === o && (u.protocol === "https:" || u.protocol === "http:")) parentOrigin = o;
  } catch (e) { parentOrigin = null; }
  if (!parentOrigin || window.parent === window) return;           // opened on its own: stay inert

  const send = (m) => window.parent.postMessage({ metis_draw: 1, ...m }, parentOrigin);
  const HANDLERS = {
    getSmiles: async (k) => String(await k.getSmiles()),
    setMolecule: async (k, arg) => { await k.setMolecule(arg); return ""; },
  };

  window.addEventListener("message", async (ev) => {
    if (ev.source !== window.parent || ev.origin !== parentOrigin) return;
    const d = ev.data;
    if (!d || typeof d !== "object" || d.metis_draw !== 1 || typeof d.id !== "number" || !Object.hasOwn(HANDLERS, d.cmd)) return;
    if (d.cmd === "setMolecule" && (typeof d.arg !== "string" || d.arg.length > MAX)) return send({ id: d.id, error: "invalid argument" });
    const k = window.ketcher;
    if (!k) return send({ id: d.id, error: "not ready" });
    try { send({ id: d.id, result: await HANDLERS[d.cmd](k, d.arg) }); } catch (e) { send({ id: d.id, error: String((e && e.message) || e).slice(0, 300) }); }
  });

  const timer = setInterval(() => { if (window.ketcher) { clearInterval(timer); send({ ready: true }); } }, 100);
})();
