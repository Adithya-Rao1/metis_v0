// Account: tier and caps, consents (GET/POST /consents), data export (GET /account/data), account deletion (DELETE /account/data).
import { el, renderChrome, say, showError, showLoading, confirmDialog } from "./chrome.js";
import { api, authed, describeError, get, post, del, requireSession, saveBlob } from "./api.js";

const $ = (id) => document.getElementById(id);
const user = await requireSession();
renderChrome({ current: "account.html", crumb: "Account / Data and consents", user });
const page = $("page-state");
const TIER_MAX_N = { free: 10, edu: 20, paid: 20 }; // mirrors backend/schemas/contracts.py
const CONSENTS = [
  ["train_models", "Use my compounds and measurements to train or improve Metis models", "Off means your data never enters training sets."],
  ["share_dataset", "Include my data, de-identified, in a shared open dataset", "Off means nothing you enter is shared with other users or published."],
  ["analytics", "Allow usage analytics", "Off means no analytics are collected."],
  ["contact", "Contact me about Metis (product news, studies)", "Off means no contact beyond account email."],
];

async function load() {
  showLoading(page, "Loading your account");
  let state;
  try { state = await authed(() => get("/consents")); } catch (e) { return showError(page, e, load); }
  page.replaceChildren();
  $("acct-frame").hidden = false;
  $("acct-email").textContent = user.email || "not available";
  $("acct-tier").textContent = `${user.tier}${user.email_verified ? ", email verified" : ""}`;
  $("acct-cap").textContent = `up to N = ${TIER_MAX_N[user.tier] ?? 10}`;
  const box = $("consents");
  box.replaceChildren(box.firstElementChild);
  for (const [scope, label, hint] of CONSENTS) {
    box.append(el("label", {}, el("input", { type: "checkbox", id: `consent-${scope}`, "data-consent": scope, checked: !!state.consents[scope]?.granted,
      onchange: async (ev) => {
        const t = ev.target, want = t.checked, msg = $("cons-msg");
        t.disabled = true;
        try {
          const res = await authed(() => post("/consents", { scope, granted: want }));
          t.checked = !!res.consents[scope].granted;
          say(msg, `"${label}" is now ${t.checked ? "on" : "off"}. The change is recorded with its time.`, "ok");
        } catch (e) { t.checked = !want; say(msg, describeError(e).message, "error"); }
        finally { t.disabled = false; }
      } }), el("span", {}, label, el("br"), el("span", { class: "legend", text: hint }))));
  }
}

$("export-data").addEventListener("click", async () => {
  const msg = $("export-msg");
  say(msg, "Preparing your export...", "ok");
  try {
    const r = await authed(() => api("GET", "/account/data", { blob: true }));
    saveBlob(r.blob, r.filename || "metis-export.json");
    say(msg, "Downloaded.", "ok");
  } catch (e) { say(msg, describeError(e).message, "error"); }
});

$("del-all").addEventListener("click", async () => {
  const pw = el("input", { id: "del-pass", type: "password", autocomplete: "current-password", "aria-label": "Your password" });
  const ok = await confirmDialog({ title: "Delete my account", typed: "DELETE", okLabel: "Delete everything",
    body: "This permanently removes your account, projects, compounds, predictions, measurements and notes. Backups are purged within 30 days. Enter your password and type DELETE.",
    extra: el("div", { class: "field" }, el("label", { for: "del-pass", text: "Password" }), pw) });
  if (!ok) return;
  const msg = $("del-msg");
  try {
    await authed(() => del("/account/data", { password: pw.value }));
    location.replace("index.html");
  } catch (e) { say(msg, describeError(e).message, "error"); }
});

load();
