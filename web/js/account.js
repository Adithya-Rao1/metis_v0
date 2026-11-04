import { el, renderChrome } from "./chrome.js";
renderChrome({ current: "account.html", crumb: "Account / Data and consents" });
const CONSENTS = [
  ["train", "Use my compounds and measurements to train or improve Metis models", "Off means your data never enters training sets."],
  ["share", "Include my data, de-identified, in a shared open dataset", "Off means nothing you enter is shared with other users or published."],
  ["analytics", "Allow usage analytics", "Off means no analytics are collected."],
];
const box = document.getElementById("consents");
const store = (() => { try { return window.localStorage; } catch (e) { return null; } })();
CONSENTS.forEach(([k, label, hint]) => {
  let on = false;
  try { on = store?.getItem("metis.fixture.consent." + k) === "1"; } catch (e) { on = false; }
  box.append(el("label", {}, el("input", { type: "checkbox", id: `consent-${k}`, checked: on, "data-consent": k,
    onchange: (e) => {
      try { store?.setItem("metis.fixture.consent." + k, e.target.checked ? "1" : "0"); } catch (err) { /* storage unavailable */ }
      document.getElementById("cons-msg").textContent = `Fixture mode: "${label}" is now ${e.target.checked ? "on" : "off"} in this browser only; nothing was sent.`;
    } }), el("span", {}, label, el("br"), el("span", { class: "legend", text: hint }))));
});
const dlg = document.getElementById("confirm"), inp = document.getElementById("c-in"), ok = document.getElementById("c-ok");
inp.addEventListener("input", () => { ok.disabled = inp.value !== "DELETE"; });
document.getElementById("del-all").addEventListener("click", () => { inp.value = ""; ok.disabled = true; dlg.showModal(); });
document.getElementById("del-project").addEventListener("click", () => { document.getElementById("del-msg").textContent = "Fixture mode: no projects are stored, so nothing was deleted."; });
dlg.addEventListener("close", () => {
  if (dlg.returnValue === "ok") document.getElementById("del-msg").textContent = "Fixture mode: nothing is stored, so nothing was deleted. The real action deletes immediately.";
});
