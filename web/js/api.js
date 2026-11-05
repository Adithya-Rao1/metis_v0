// The one API client. Same-origin only: the API is served under /api on the site's own origin (see docs/WEB_WIRING.md).
// Session and CSRF tokens live in cookies (session is httponly, so this file never sees it). Every mutating request
// sends the CSRF header named by backend/accounts/config.py (CSRF_HEADER = "X-CSRF-Token", cookie "metis_csrf").
// Nothing here touches the DOM.
export const API_BASE = "/api";
export const CSRF_HEADER = "X-CSRF-Token";
export const COOKIE_CSRF = "metis_csrf";
const READ_ONLY = new Set(["GET", "HEAD", "OPTIONS"]);

export class ApiError extends Error {
  constructor(status, detail, headers) {
    super(typeof detail === "string" ? detail : `request failed (${status})`);
    this.status = status;
    this.detail = detail;
    this.code = typeof detail === "string" ? detail : (detail && typeof detail === "object" && !Array.isArray(detail) ? detail.code || null : null);
    const ra = headers && headers.get ? Number(headers.get("Retry-After")) : NaN;
    this.retryAfter = Number.isFinite(ra) && ra > 0 ? ra : null;
    this.problems = detail && typeof detail === "object" && !Array.isArray(detail) && Array.isArray(detail.problems) ? detail.problems : [];
  }
}

export function readCookie(name) {
  for (const part of document.cookie.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return decodeURIComponent(part.slice(i + 1).trim());
  }
  return null;
}

async function ensureCsrf(force = false) {
  if (!force && readCookie(COOKIE_CSRF)) return;
  await fetch(`${API_BASE}/auth/csrf`, { credentials: "same-origin" });
}

async function parse(resp) {
  const type = resp.headers.get("content-type") || "";
  if (type.includes("json")) {
    try { return await resp.json(); } catch (e) { return null; }
  }
  return null;
}

// `base` (default "/api") lets a sibling app on the same origin, Lead Studio under "/api/lead", reuse this client (CSRF, retry, errors).
// api("POST", "/projects", { json: {...} }) -> parsed JSON. { blob: true } returns the response body as a Blob
// (downloads); { text: true } returns text. Throws ApiError for every non-2xx answer.
export async function api(method, path, opts = {}) {
  const { json, query, blob, text, retry = true, base = API_BASE } = opts;
  let url = `${base}${path}`;
  if (query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v != null) q.set(k, String(v));
    const s = q.toString();
    if (s) url += `?${s}`;
  }
  const mutating = !READ_ONLY.has(method);
  if (mutating) await ensureCsrf();
  const init = { method, credentials: "same-origin", headers: {} };
  if (mutating) init.headers[CSRF_HEADER] = readCookie(COOKIE_CSRF) || "";
  if (json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(json); }
  let resp;
  try { resp = await fetch(url, init); } catch (e) { throw new ApiError(0, "network_error", null); }
  if (resp.status === 403 && mutating && retry) {
    const body = await parse(resp.clone());
    if (body && body.detail === "csrf_failed") { await ensureCsrf(true); return api(method, path, { ...opts, retry: false }); }
  }
  if (!resp.ok) {
    const body = await parse(resp);
    throw new ApiError(resp.status, body && body.detail !== undefined ? body.detail : null, resp.headers);
  }
  if (blob) return { blob: await resp.blob(), filename: filenameOf(resp), type: resp.headers.get("content-type") || "" };
  if (text) return resp.text();
  if (resp.status === 204) return null;
  return parse(resp);
}

function filenameOf(resp) {
  const m = /filename="?([^";]+)"?/.exec(resp.headers.get("content-disposition") || "");
  return m ? m[1] : null;
}

export const get = (path, opts) => api("GET", path, opts);
export const post = (path, json, opts) => api("POST", path, { ...opts, json });
export const put = (path, json, opts) => api("PUT", path, { ...opts, json });
export const patch = (path, json, opts) => api("PATCH", path, { ...opts, json });
export const del = (path, json, opts) => api("DELETE", path, { ...opts, json });

// ---- plain-language messages -------------------------------------------------------------------------------------
const CODES = {
  invalid_credentials: "Email or password is not correct.",
  email_not_verified: "This email address is not verified yet. Use the link in the verification email, or ask for a new one.",
  invalid_or_expired_token: "This link is invalid or has expired. Ask for a new one.",
  rate_limited: "Too many attempts.",
  csrf_failed: "Your session check failed. Reload the page and try again.",
  terms_acceptance_required: "You must accept the Terms of Service, Privacy Policy and Disclaimer.",
  terms_version_not_current: "The terms changed while this page was open. Reload and accept the current version.",
  terms_reacceptance_required: "The terms changed. Accept the current version to continue.",
  terms_reaccept_required: "The terms changed. Accept the current version to continue.",
  password_incorrect: "That password is not correct.",
  not_authenticated: "You are signed out.",
};

function pydanticLoc(e) {
  return Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body").join(".") : "";
}

// -> { message, fields: {fieldName: message}, problems: [{record, error}] }. Never includes server HTML.
export function describeError(err) {
  if (!(err instanceof ApiError)) return { message: "Something went wrong. Reload the page and try again.", fields: {}, problems: [] };
  const out = { message: "", fields: {}, problems: err.problems || [] };
  const d = err.detail;
  switch (err.status) {
    case 0: out.message = "Cannot reach the server. Check your connection and try again."; break;
    case 401: out.message = CODES[err.code] || "You are signed out. Sign in to continue."; break;
    case 403: out.message = CODES[err.code] || "You do not have permission to do that."; break;
    case 404: out.message = "Not found. It may not exist, or it may belong to another account."; break;
    case 409: out.message = (typeof d === "string" ? d : "") || "This conflicts with the current state. Reload and try again."; break;
    case 413: out.message = (d && typeof d === "object" && d.message) || "That upload is too large."; break;
    case 422:
      if (Array.isArray(d)) {
        for (const e of d) { const f = pydanticLoc(e); if (f) out.fields[f] = String(e.msg || "invalid"); }
        out.message = d.length ? `Please check the input: ${d.map((e) => String(e.msg || "invalid")).slice(0, 3).join("; ")}` : "Please check the input.";
      } else if (d && typeof d === "object") {
        out.message = String(d.message || "Please check the input.");
      } else out.message = typeof d === "string" ? d : "Please check the input.";
      break;
    case 429: out.message = `${CODES[err.code] || "Too many requests."} ${err.retryAfter ? `Try again in ${formatWait(err.retryAfter)}.` : "Try again later."}`; break;
    case 503: case 504:
      out.message = d && typeof d === "object" && d.message ? String(d.message)
        : `The service is busy right now. ${err.retryAfter ? `Try again in ${formatWait(err.retryAfter)}.` : "Try again in a moment."}`;
      break;
    default:
      out.message = d && typeof d === "object" && d.message ? String(d.message) : (typeof d === "string" && CODES[d]) || "The request could not be completed.";
  }
  return out;
}

export function formatWait(seconds) {
  if (seconds < 90) return `${Math.ceil(seconds)} seconds`;
  return `${Math.ceil(seconds / 60)} minutes`;
}

// Where an unauthenticated visitor goes. Only same-site page names are ever allowed as `next`.
const NEXT_OK = /^(lead\/)?[a-z_]+\.html(\?[A-Za-z0-9=&_.-]*)?$/;
export function safeNext(v) { return v && NEXT_OK.test(v) ? v : "app.html"; }

// Pages under /lead/ (Lead Studio) live one level down: `next` is "lead/index.html" and auth.html is "../auth.html".
const inLead = () => /\/lead\/[^/]*$/.test(location.pathname);
function signInUrl(next, reason) {
  const q = new URLSearchParams({ next });
  if (reason) q.set("why", reason);
  return `${inLead() ? "../" : ""}auth.html?${q}`;
}

export function redirectToSignIn(reason) {
  const file = location.pathname.split("/").pop();
  const here = (inLead() ? `lead/${file || "index.html"}` : file) + location.search;
  location.replace(signInUrl(safeNext(here), reason));
}

// Call on every signed-in screen. Resolves with the user, or redirects (and never resolves) when signed out.
export async function requireSession() {
  try {
    const me = await get("/auth/me");
    if (!me.terms_current) { redirectToSignIn("terms"); return new Promise(() => {}); }
    return me;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) { redirectToSignIn(); return new Promise(() => {}); }
    throw e;
  }
}

// Wrap calls that should send a signed-out or terms-changed user to the sign-in screen.
export async function authed(fn) {
  try { return await fn(); } catch (e) {
    if (e instanceof ApiError && e.status === 401) { redirectToSignIn(); return new Promise(() => {}); }
    if (e instanceof ApiError && e.status === 403 && TERMS_CODES.has(e.code)) { redirectToSignIn("terms"); return new Promise(() => {}); }
    throw e;
  }
}

// The server answers 403 with one of these on ANY authenticated route (GETs included) until the current terms are accepted.
const TERMS_CODES = new Set(["terms_reaccept_required", "terms_reacceptance_required"]);

// 2D picture for a SMILES that is not stored in a project (docking-prep ligand, nearest training compounds): POST /dockprep/depiction
// {smiles} -> image/svg+xml. Resolves with a blob: URL, or null when the server has no such route (404/405/501) or cannot draw it.
// A missing route is remembered so the page does not keep asking.
let depictionRouteMissing = false;
export async function smilesPicture(smiles) {
  if (depictionRouteMissing || !smiles) return null;
  try {
    const svg = await api("POST", "/dockprep/depiction", { json: { smiles: String(smiles) }, text: true });
    return typeof svg === "string" && svg.includes("<svg") ? URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" })) : null;
  } catch (e) {
    if (e instanceof ApiError && [404, 405, 501].includes(e.status)) depictionRouteMissing = true;
    if (e instanceof ApiError && e.status === 401) throw e;
    return null;
  }
}

export function saveBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

export function b64ToBlob(b64, type = "application/octet-stream") {
  const bin = atob(b64), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}

export function bytesToB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

export const depictionUrl = (pid, cid, highlight = []) =>
  `${API_BASE}/projects/${encodeURIComponent(pid)}/compounds/${encodeURIComponent(cid)}/depiction.svg` +
  (highlight.length ? `?${highlight.map((i) => `highlight=${Number(i)}`).join("&")}` : "");
