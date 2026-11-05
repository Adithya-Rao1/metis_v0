// HTTP client for manual mode: same-origin /lead/api/manual/* through the Metis client (net.js: session cookie, CSRF, sign-in redirect).
// `fetchFn` / `base` are gone; tests inject nothing. Errors keep the "<status> <detail>" message shape the panel shows.
import { lead } from "../net.js";

const wrap = async (call) => {
  try { return await call(); } catch (e) {
    throw new Error(`${e.status || ""} ${typeof e.detail === "string" ? e.detail : e.message}`.trim());
  }
};
export function makeApi() {
  const json = (method, url, body) => wrap(() => lead(method, url, body === undefined ? {} : { json: body }));
  return {
    applicable: (smiles, locked = []) => json("GET", `/api/manual/applicable?smiles=${encodeURIComponent(smiles)}&locked=${locked.map(Number).join(",")}`),
    preview: (req) => json("POST", "/api/manual/preview", req),
    validate: (smiles) => json("POST", "/api/molecules/validate", { smiles }),
    createSession: (body) => json("POST", "/api/manual/sessions", body),
    appendStep: (id, step) => json("POST", `/api/manual/sessions/${encodeURIComponent(id)}/steps`, step),
    exportText: (id) => wrap(() => lead("GET", `/api/manual/sessions/${encodeURIComponent(id)}/export`, { text: true })),
  };
}
