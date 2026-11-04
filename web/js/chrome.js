// Shared page chrome: app bar, FIXTURE banner, disclaimer footer. Builds DOM with textContent only.
import { DISCLAIMER } from "./legal_text.js";

const NAV = [
  ["app.html", "Project"], ["add.html", "Add compounds"], ["model.html", "Models"],
  ["docking.html", "Docking prep"], ["account.html", "Account"],
];

export function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}

export function renderChrome({ current, crumb }) {
  const skip = el("a", { class: "skip", href: "#main", text: "Skip to content" });
  const nav = el("nav", { class: "nav", "aria-label": "Main" },
    NAV.map(([href, label]) => el("a", { href, text: label, ...(href === current ? { "aria-current": "page" } : {}) })));
  const bar = el("header", { class: "appbar" },
    el("a", { class: "brand", href: "index.html", text: "Metis" }),
    el("span", { class: "crumb", text: crumb || "" }), el("span", { class: "sp" }), nav);
  const banner = el("div", { class: "fixture-banner", role: "note", "data-testid": "fixture-banner" },
    el("strong", { text: "FIXTURE data. " }),
    "Every number, structure label and percentage on this page is illustrative layout content, not a model result. No backend is connected.");
  document.body.prepend(skip, bar, banner);
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

export async function loadJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`failed to load ${path}`);
  return r.json();
}

export const qs = (k, d = null) => new URLSearchParams(location.search).get(k) ?? d;
