// The Metis CSP allows no inline styles, so markup built from template strings cannot carry style attributes.
// Those templates write data-style="prop:value;prop:value" instead, and this observer applies it through the CSSOM
// (element.style.setProperty), which style-src 'self' allows. Values are numbers and colours computed in this page, never server text.
function applyTo(el) {
  const s = el.getAttribute("data-style");
  if (!s) return;
  for (const decl of s.split(";")) {
    const i = decl.indexOf(":");
    if (i > 0) el.style.setProperty(decl.slice(0, i).trim(), decl.slice(i + 1).trim());
  }
}

function scan(node) {
  if (node.nodeType !== 1) return;
  if (node.hasAttribute("data-style")) applyTo(node);
  for (const el of node.querySelectorAll("[data-style]")) applyTo(el);
}

new MutationObserver((records) => {
  for (const r of records) {
    if (r.type === "attributes") applyTo(r.target);
    else for (const n of r.addedNodes) scan(n);
  }
}).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-style"] });
scan(document.documentElement);
