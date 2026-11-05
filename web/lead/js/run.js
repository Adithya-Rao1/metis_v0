// Run ingestion: turns SSE events into store updates + bus events. The stream itself is opened in api.js (openRun).
import { bus } from "./bus.js";
import { store } from "./state.js";

/** Apply one schema Event {type, run_id, seq, payload}. Exported so tests / wave-2 code can feed events. */
export function ingest(ev) {
  const p = ev.payload;
  switch (ev.type) {
    case "run_start":
      store.reset();
      store.runId = ev.run_id;
      store.source = p.source;
      store.config = p.config;
      store.status = "running";
      store.seed = { smiles: p.config.seed_smiles, molblock: p.seed_molblock, score: p.seed_score };
      store.remember(p.config.seed_smiles, { score: p.seed_score, alerts: p.seed_score.alerts, composite: p.seed_score.composite });
      store.locked = new Set(p.config.locked_atoms || []);
      bus.emit("run:start", { event: ev, payload: p });
      break;
    case "candidate":
      store.candidates.push(p);
      store.remember(p.smiles, {
        score: p.score, alerts: p.score.alerts, composite: p.score.composite,
        contributions: p.explanation?.atom_contributions || undefined,
      });
      bus.emit("run:candidate", { event: ev, candidate: p, index: store.candidates.length - 1 });
      break;
    case "generation":
      store.generations.push(p);
      bus.emit("run:generation", { event: ev, payload: p });
      break;
    case "policy_metrics":
      store.policy.push(p);
      bus.emit("run:policy", { event: ev, payload: p });
      break;
    case "run_end":
      store.end = p;
      store.status = "done";
      bus.emit("run:end", { event: ev, payload: p });
      break;
    case "error":
      store.status = p.recoverable ? store.status : "error";
      bus.emit("run:error", { event: ev, payload: p });
      break;
    default:
      console.warn("unknown event type");
  }
}
