import { el } from "./chrome.js";
const tabs = [...document.querySelectorAll('[role="tab"]')];
function select(t) {
  tabs.forEach((x) => {
    const on = x === t;
    x.setAttribute("aria-selected", on); x.tabIndex = on ? 0 : -1;
    document.getElementById(x.getAttribute("aria-controls")).hidden = !on;
  });
  t.focus();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => select(t));
  t.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") select(tabs[(i + 1) % tabs.length]);
    if (e.key === "ArrowLeft") select(tabs[(i + tabs.length - 1) % tabs.length]);
  });
});
const accept = document.getElementById("accept"), submit = document.getElementById("signup-submit");
const sync = () => { submit.disabled = !accept.checked; };
accept.checked = false; sync();
accept.addEventListener("change", sync);
document.getElementById("signup").addEventListener("submit", (e) => {
  e.preventDefault();
  const msg = document.getElementById("signup-msg");
  if (!accept.checked) { msg.textContent = "You must accept the Terms, Privacy Policy and Disclaimer to continue."; return; }
  msg.textContent = "Fixture mode: no account was created. The real flow sends a verification email.";
});
document.getElementById("login").addEventListener("submit", (e) => {
  e.preventDefault();
  document.getElementById("login-msg").textContent = "Fixture mode: not connected to a backend.";
});
