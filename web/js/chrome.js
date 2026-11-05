// Shared page chrome: app bar, disclaimer footer, loading/empty/error states. Builds DOM with textContent only:
// server data never goes through innerHTML (tests/web/test_static.py enforces it).
import { DISCLAIMER } from "./legal_text.js";
import { api, describeError } from "./api.js";

const NAV = [
  ["app.html", "Projects"], ["model.html", "Models"], ["docking.html", "Docking prep"], ["lead/index.html", "Lead Studio (Beta)"], ["account.html", "Account"],
];

export function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "text") e.textContent = v;
    else if (k === "pct") e.style.width = `${Number(v)}%`; // CSSOM, allowed under a strict style-src
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}

export function renderChrome({ current, crumb, user = null }) {
  const skip = el("a", { class: "skip", href: "#main", text: "Skip to content" });
  const nav = el("nav", { class: "nav", "aria-label": "Main" },
    NAV.map(([href, label]) => el("a", { href, text: label, ...(href === current ? { "aria-current": "page" } : {}) })));
  const bar = el("header", { class: "appbar" },
    el("a", { class: "brand", href: "index.html", text: "Metis" }),
    el("span", { class: "crumb", text: crumb || "" }), el("span", { class: "sp" }), nav);
  if (user) {
    bar.append(el("span", { class: "chip", "data-testid": "tier", text: `${user.tier} account` }),
      el("button", { class: "btn small", type: "button", id: "logout", text: "Log out", onclick: async () => {
        try { await api("POST", "/auth/logout"); } catch (e) { /* idempotent */ }
        location.replace("auth.html");
      } }));
  }
  document.body.prepend(skip, bar);
  const foot = el("footer", { class: "site-foot" },
    el("p", { class: "disclaimer", "data-testid": "disclaimer", text: DISCLAIMER }),
    el("span", {}, "Predictions are model estimates, not measurements. ",
      el("a", { href: "legal/disclaimer.html", text: "Disclaimer (draft)" }), " | ",
      el("a", { href: "legal/terms.html", text: "Terms (draft)" }), " | ",
      el("a", { href: "legal/privacy.html", text: "Privacy (draft)" })));
  document.body.append(foot);
  const main = document.querySelector("main");
  if (main) main.id = "main";
}

export const qs = (k, d = null) => new URLSearchParams(location.search).get(k) ?? d;
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const qsId = (k) => { const v = qs(k); return v && UUID_RE.test(v) ? v.toLowerCase() : null; };

// ---- states: every data region has loading, empty and error (with retry) ----
export function stateBox(kind, message, { retry = null, id = null } = {}) {
  const box = el("div", { class: `state state-${kind}`, role: kind === "error" ? "alert" : "status", "data-state": kind, id },
    el("span", { text: message }));
  if (retry) box.append(" ", el("button", { class: "btn small", type: "button", text: "Try again", onclick: retry }));
  return box;
}
export const showLoading = (host, what = "Loading") => host.replaceChildren(stateBox("loading", `${what}...`));
export const showEmpty = (host, message) => host.replaceChildren(stateBox("empty", message));
export function showError(host, err, retry) {
  host.replaceChildren(stateBox("error", describeError(err).message, { retry }));
}

// A message line beside a form. kind: "error" | "ok".
export function say(node, message, kind = "ok") {
  node.textContent = message;
  node.dataset.kind = kind;
}

export function showFieldErrors(form, fields) {
  form.querySelectorAll(".field-error").forEach((n) => n.remove());
  for (const [name, msg] of Object.entries(fields || {})) {
    const input = form.querySelector(`[name="${CSS.escape(name)}"]`);
    if (input) input.after(el("span", { class: "field-error", role: "alert", text: msg }));
  }
}

// A modal confirmation (native <dialog>). Resolves true on confirm. `typed`: the user must type this word first.
export function confirmDialog({ title, body, okLabel = "Delete", typed = null, extra = null }) {
  return new Promise((resolve) => {
    const input = typed ? el("input", { id: "cd-typed", autocomplete: "off", "aria-label": `Type ${typed} to confirm` }) : null;
    const ok = el("button", { class: "btn primary", type: "button", id: "cd-ok", text: okLabel, disabled: !!typed });
    const cancel = el("button", { class: "btn", type: "button", id: "cd-cancel", text: "Cancel" });
    const dlg = el("dialog", { "aria-labelledby": "cd-h" },
      el("h2", { id: "cd-h", text: title }), el("p", { text: body }),
      typed ? el("div", { class: "field" }, el("label", { for: "cd-typed", text: `Type ${typed} to confirm` }), input) : null,
      extra, el("div", { class: "inline mt8" }, cancel, ok));
    if (input) input.addEventListener("input", () => { ok.disabled = input.value !== typed; });
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    ok.addEventListener("click", () => done(true));
    cancel.addEventListener("click", () => done(false));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

export const fmtDate = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10); };
