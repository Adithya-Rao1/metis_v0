import { el, loadJSON, renderChrome } from "./chrome.js";
import { DISCLAIMER } from "./legal_text.js";
import { cellText } from "./format.js";

renderChrome({ current: "app.html", crumb: "Project / Export and report" });
const project = await loadJSON("fixtures/project.json");
const eps = Object.keys(project.endpoints);
const c0 = project.compounds.find((x) => x.id === 3) || project.compounds[0];
const method = c0.conformer.method;

document.getElementById("csv-note").textContent =
  "One row per compound. Columns per property: value or calibrated probability, interval low, interval high, coverage, ensemble size N, sample size. Plus blended and property-only rank. UTF-8, comma separated, one header row.";
document.getElementById("sdf-note").textContent =
  `One record per compound with the same values as SD properties. Coordinates are a computed conformer (${method}), not an experimental structure; the method and its seed are written into each record.`;

const head = ["compound", ...eps.map((e) => `${e}: value`), ...eps.map((e) => `${e}: interval and N`)];
document.querySelector("#csv-prev thead").append(el("tr", {}, head.map((h) => el("th", { scope: "col", text: h }))));
document.querySelector("#csv-prev tbody").append(...project.compounds.slice(0, 3).map((c) => el("tr", {},
  el("td", { text: c.name }),
  eps.map((e) => el("td", { class: "mono", text: cellText(c.predictions[e]).main })),
  eps.map((e) => el("td", { class: "mono", text: cellText(c.predictions[e]).sub })))));

document.getElementById("rp-title").textContent = c0.name;
document.getElementById("rp-list").replaceChildren(...eps.map((e) => {
  const t = cellText(c0.predictions[e]);
  return el("li", {}, `${project.endpoints[e].label} (${project.endpoints[e].unit}): ${t.main}; ${t.sub}`);
}));
document.getElementById("rp-conf").textContent = `3D image: computed conformer, not an experimental structure (${method}).`;
document.getElementById("rp-foot").textContent = DISCLAIMER;
