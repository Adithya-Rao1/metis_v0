// Lead Studio's API client. Same-origin only (everything is under /lead/api/*), through net.js -> the Metis client.
// The backend either answers /lead/api/health ("live") or it does not ("offline": the page says so and offers a retry;
// there is no recorded or synthetic fallback data).
import { bus } from "./bus.js";
import { lead, eventsUrl, checkSession } from "./net.js";

const TYPES = ["run_start", "generation", "candidate", "policy_metrics", "run_end", "error"];

export const api = { mode: "live", health: null, error: null };

// ----------------------------------------------------------------------------- health
export async function probe() {
  try {
    api.health = await lead("GET", "/api/health");
    api.mode = "live"; api.error = null;
  } catch (e) {
    api.health = null; api.mode = "offline"; api.error = e.message || "unreachable";
  }
  bus.emit("api:mode", { mode: api.mode });
  return api.mode;
}

// -------------------------------------------------------------------------------- targets
export async function getTargets() {
  const d = await lead("GET", "/api/targets");
  return Array.isArray(d) ? d : d?.targets || [];
}

/** Protein PDB text, from the studio's own origin only. structure_url is a path under /api/data/ (or an absolute /api/... path). */
export async function getStructure(t) {
  const tries = [];
  const u = String(t.structure_url || "");
  if (u && !/^[a-z][a-z0-9+.-]*:|^\/\//i.test(u)) tries.push(u.startsWith("/api/") ? u : `/api/data/${u.replace(/^\/+/, "")}`);
  tries.push(`/api/data/structures/${encodeURIComponent(t.id)}.pdb`);
  let last = null;
  for (const p of tries) {
    try { return await lead("GET", p, { text: true }); } catch (e) { last = e; }
  }
  throw last || new Error("no structure URL");
}

// ----------------------------------------------------------------------------------- validate
export const validate = (smiles) => lead("POST", "/api/molecules/validate", { json: { smiles } });

// ------------------------------------------------------------------------------------- runs
export const createRun = (cfg) => lead("POST", "/api/runs", { json: cfg });
export const cancelRun = (runId) => lead("DELETE", `/api/runs/${encodeURIComponent(runId)}`);

/**
 * Open the SSE stream of a run (same-origin EventSource, cookie auth). Reconnects with backoff; the server replays from the
 * start on reconnect (no Last-Event-ID), so events with seq <= the last one seen are dropped.
 * Returns {close}. handlers: onEvent(ev), onDone(), onError(msg), onStatus(text)
 */
export function openRun(runId, { onEvent, onDone, onError, onStatus }) {
  let closed = false, es = null, timer = 0, retries = 0, lastSeq = -1, sawAny = false;
  const finish = () => { closed = true; clearTimeout(timer); es?.close(); };
  const url = eventsUrl(runId);
  const open = () => {
    if (closed) return;
    es = new EventSource(url);
    for (const t of TYPES) {
      es.addEventListener(t, (e) => {
        if (t === "error" && !e.data) return; // connection-level error is handled by onerror below
        let ev;
        try { ev = JSON.parse(e.data); } catch (err) { console.warn("malformed SSE payload"); return; }
        if (ev.seq <= lastSeq) return;
        lastSeq = ev.seq; sawAny = true; retries = 0;
        onEvent(ev);
        if (ev.type === "run_end" || (ev.type === "error" && !ev.payload?.recoverable)) { finish(); onDone?.(); }
      });
    }
    es.onerror = () => {
      if (closed) return;
      es.close();
      checkSession(); // a signed-out session cannot be told from a network error here: this leaves for sign-in if needed
      if (++retries > 6) { finish(); onError?.(sawAny ? "Lost connection to the run stream." : "Could not open the run event stream."); return; }
      onStatus?.(`Reconnecting to the run stream (attempt ${retries})…`);
      timer = setTimeout(open, Math.min(400 * 2 ** retries, 5000));
    };
  };
  open();
  return { close: finish };
}

// ------------------------------------------------------------------------------------- poses
export const createPose = (req) => lead("POST", "/api/poses", { json: req });
export const getPose = (jobId) => lead("GET", `/api/poses/${encodeURIComponent(jobId)}`);
