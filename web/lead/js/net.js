// Lead Studio's network layer: a thin wrapper over the Metis client (web/js/api.js), so Lead Studio shares the Metis
// session cookie, the CSRF header/cookie, the sign-in redirect on 401 and the terms re-acceptance redirect.
// Everything is same-origin under /lead (/lead/api/*, /lead/library/*, /lead/profiles/*). Nothing here touches the DOM.
import { api, authed, ApiError, describeError, requireSession } from "../../js/api.js";

export const BASE = "/api/lead";
export { requireSession };

/**
 * lead("POST", "/api/runs", { json }) -> parsed JSON ({ text: true } -> string, { blob: true } -> {blob, filename}).
 * A 401 (or a terms change) leaves for the Metis sign-in page and never resolves. Other failures throw an Error whose
 * `message` is plain language (never server HTML) and which carries `status` and `detail`.
 */
export async function lead(method, path, opts = {}) {
  try {
    return await authed(() => api(method, path, { ...opts, base: BASE }));
  } catch (e) {
    if (e instanceof ApiError) {
      const err = new Error(describeError(e).message);
      err.status = e.status; err.detail = e.detail; err.retryAfter = e.retryAfter;
      throw err;
    }
    throw e;
  }
}

export const eventsUrl = (runId) => `${BASE}/api/runs/${encodeURIComponent(runId)}/events`;

/** Called when an event stream breaks: if the cause is an expired session this leaves for sign-in. */
export async function checkSession() {
  try { await requireSession(); } catch (e) { /* offline: the stream's own retry logic reports it */ }
}
