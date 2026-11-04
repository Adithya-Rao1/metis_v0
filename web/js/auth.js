// Auth screen: register, verify, log in, forgot/reset password, resend verification, re-accept changed terms.
import { el, qs, say, showFieldErrors } from "./chrome.js";
import { describeError, get, post, safeNext } from "./api.js";

const $ = (id) => document.getElementById(id);
const PANELS = ["p-signup", "p-login", "p-forgot", "p-resend", "p-reset", "p-verify", "p-terms"];
let termsVersion = null;

function showPanel(id, heading) {
  for (const p of PANELS) $(p).hidden = p !== id;
  $("tabs").hidden = id !== "p-signup" && id !== "p-login";
  if (heading) $("auth-h").textContent = heading;
}
function banner(msg) { $("auth-banner").textContent = msg; $("auth-banner").hidden = !msg; }

// ---- tabs ----
const tabs = [...document.querySelectorAll('[role="tab"]')];
function select(t, focus = true) {
  tabs.forEach((x) => {
    const on = x === t;
    x.setAttribute("aria-selected", on); x.tabIndex = on ? 0 : -1;
    $(x.getAttribute("aria-controls")).hidden = !on;
  });
  if (focus) t.focus();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => select(t));
  t.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") select(tabs[(i + 1) % tabs.length]);
    if (e.key === "ArrowLeft") select(tabs[(i + tabs.length - 1) % tabs.length]);
  });
});

// ---- helpers ----
async function submitting(form, msgNode, fn) {
  const btn = form.querySelector('button[type="submit"]');
  const was = btn.disabled;
  btn.disabled = true; showFieldErrors(form, {}); say(msgNode, "Working...", "ok");
  try { await fn(); } catch (e) {
    const d = describeError(e);
    say(msgNode, d.message, "error"); showFieldErrors(form, d.fields);
    return false;
  } finally { btn.disabled = was; }
  return true;
}
// ---- sign up ----
const accept = $("accept"), submit = $("signup-submit");
const sync = () => { submit.disabled = !accept.checked || !termsVersion; };
accept.checked = false; sync();
accept.addEventListener("change", sync);
$("signup").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget, msg = $("signup-msg");
  if (!accept.checked) { say(msg, "You must accept the Terms, Privacy Policy and Disclaimer to continue.", "error"); return; }
  const email = form.elements.email.value.trim();
  const ok = await submitting(form, msg, () => post("/auth/register", { email, password: form.elements.password.value, accepted_terms_version: termsVersion }));
  if (ok) {
    form.elements.password.value = "";
    say(msg, "Check your email. We sent a verification link; your account works once you open it. The link expires after 24 hours.", "ok");
  }
});

// ---- log in ----
$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget, msg = $("login-msg");
  let me = null;
  const ok = await submitting(form, msg, async () => {
    await post("/auth/login", { email: form.elements.email.value.trim(), password: form.elements.password.value });
    form.elements.password.value = "";
    me = await get("/auth/me");
  });
  if (!ok) return;
  if (!me.terms_current) { await showReaccept(); return; }
  location.assign(safeNext(qs("next")));
});

// ---- forgot / resend ----
$("forgot").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget, msg = $("forgot-msg");
  const ok = await submitting(form, msg, () => post("/auth/forgot", { email: form.elements.email.value.trim() }));
  if (ok) say(msg, "If that address belongs to a verified account, a reset link is on its way. It expires after one hour.", "ok");
});
$("resend").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget, msg = $("resend-msg");
  const ok = await submitting(form, msg, () => post("/auth/resend-verification", { email: form.elements.email.value.trim() }));
  if (ok) say(msg, "If that address has an unverified account, a new verification link is on its way.", "ok");
});
$("to-forgot").addEventListener("click", (e) => { e.preventDefault(); showPanel("p-forgot", "Reset your password"); $("fg-email").focus(); });
$("to-resend").addEventListener("click", (e) => { e.preventDefault(); showPanel("p-resend", "Verify your email"); $("rs-email").focus(); });

// ---- reset (token from the emailed link) ----
function startReset(token) {
  showPanel("p-reset", "Reset your password");
  $("reset").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget, msg = $("reset-msg");
    const ok = await submitting(form, msg, () => post("/auth/reset", { token, password: form.elements.password.value }));
    if (ok) {
      form.elements.password.value = "";
      say(msg, "Password changed. You were signed out everywhere. Log in with the new password.", "ok");
      form.querySelector('button[type="submit"]').disabled = true;
      msg.after(el("a", { class: "btn", href: "auth.html", text: "Go to log in" }));
    }
  });
}

// ---- verify (token from the emailed link) ----
async function startVerify(token) {
  showPanel("p-verify", "Verifying your email");
  try {
    await post("/auth/verify", { token });
    $("verify-msg").textContent = "Your email is verified. You can log in now.";
    $("auth-h").textContent = "Email verified";
  } catch (e) {
    $("verify-msg").textContent = describeError(e).message;
    $("auth-h").textContent = "Email not verified";
    $("verify-msg").after(el("a", { class: "btn", href: "#resend", id: "verify-resend", text: "Send a new verification email",
      onclick: (ev) => { ev.preventDefault(); showPanel("p-resend", "Verify your email"); } }));
  }
}

// ---- re-accept changed terms ----
async function showReaccept() {
  showPanel("p-terms", "The terms have changed");
  const t = await get("/auth/terms");
  termsVersion = t.version;
  $("reaccept-version").textContent = t.version;
  const box = $("reaccept-box"), btn = $("reaccept-submit");
  box.checked = false; btn.disabled = true;
  box.onchange = () => { btn.disabled = !box.checked; };
  $("reaccept").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.currentTarget, msg = $("reaccept-msg");
    const ok = await submitting(form, msg, () => post("/terms/accept", { version: termsVersion }));
    if (ok) location.assign(safeNext(qs("next")));
  };
}

// ---- boot ----
async function boot() {
  const token = qs("verify"), resetToken = qs("reset");
  if (token || resetToken) history.replaceState(null, "", "auth.html"); // keep one-time tokens out of history and referrers
  try {
    const t = await get("/auth/terms");
    termsVersion = t.version;
    $("terms-version").textContent = `, version ${t.version}`;
  } catch (e) {
    say($("signup-msg"), describeError(e).message, "error");
  }
  sync();
  if (token) return startVerify(token);
  if (resetToken) return startReset(resetToken);
  if (qs("why") === "terms") {
    banner("The Terms of Service, Privacy Policy or Disclaimer changed. Sign in to accept the new version.");
    try { await get("/auth/me"); return showReaccept(); } catch (e) { select($("tab-login"), false); return; }
  }
  if (location.hash === "#forgot") return showPanel("p-forgot", "Reset your password");
  if (location.hash === "#resend") return showPanel("p-resend", "Verify your email");
  if (qs("next")) { banner("Sign in to continue."); select($("tab-login"), false); }
}
boot().catch(() => say($("signup-msg"), "Something went wrong loading this page. Reload and try again.", "error"));
