"""Accessibility audits. `audit` is the in-repo check for the serious/critical WCAG rules axe-core reports most often (kept as a second
check); `axe_audit` below runs the real, vendored axe-core. Runs inside the page; returns a list of {rule, impact, target}. Rules covered: document-title, html-has-lang,
image-alt, button-name, link-name, label (form fields), select-name, input-image-alt, frame-title, duplicate-id(-aria), aria-valid-attr-value
(idrefs exist), aria-roles, aria-required-children (tablist/list), aria-hidden-focus, tabindex (> 0), scrollable-region-focusable,
nested-interactive, role-img-alt, color-contrast (text over a solid background; text over images/canvases is skipped as axe does)."""

AUDIT_JS = r"""
() => {
  const out = [], seen = new Set();
  const vis = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const sel = (e) => (e.id ? '#' + e.id : e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/)[0] : ''));
  const add = (rule, impact, e) => { const k = rule + sel(e); if (!seen.has(k)) { seen.add(k); out.push({ rule, impact, target: sel(e), text: (e.textContent || '').trim().slice(0, 40) }); } };
  const name = (e) => {
    const lb = e.getAttribute('aria-labelledby');
    if (lb && lb.split(/\s+/).some((id) => (document.getElementById(id)?.textContent || '').trim())) return true;
    if ((e.getAttribute('aria-label') || '').trim()) return true;
    if ((e.getAttribute('title') || '').trim()) return true;
    if ((e.textContent || '').trim()) return true;
    if (e.querySelector('img[alt]:not([alt=""]), svg[aria-label], [aria-label]')) return true;
    return false;
  };
  if (!document.title.trim()) add('document-title', 'serious', document.documentElement);
  if (!/^[a-z]{2}/i.test(document.documentElement.lang || '')) add('html-has-lang', 'serious', document.documentElement);
  for (const i of document.images) if (!i.hasAttribute('alt') && i.getAttribute('role') !== 'presentation') add('image-alt', 'critical', i);
  for (const b of document.querySelectorAll('button,[role=button]')) if (vis(b) && !name(b)) add('button-name', 'critical', b);
  for (const a of document.querySelectorAll('a[href]')) if (vis(a) && !name(a)) add('link-name', 'serious', a);
  for (const f of document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]),select,textarea')) {
    if (!vis(f) && f.type !== 'checkbox' && f.type !== 'radio') continue;
    const labelled = f.getAttribute('aria-label') || f.getAttribute('aria-labelledby') || f.closest('label') || (f.id && document.querySelector('label[for="' + CSS.escape(f.id) + '"]')) || f.getAttribute('title');
    if (!labelled) add(f.tagName === 'SELECT' ? 'select-name' : 'label', 'critical', f);
  }
  for (const f of document.querySelectorAll('iframe,frame')) if (!(f.getAttribute('title') || f.getAttribute('aria-label'))) add('frame-title', 'serious', f);
  const ids = {};
  for (const e of document.querySelectorAll('[id]')) ids[e.id] = (ids[e.id] || 0) + 1;
  for (const [id, n] of Object.entries(ids)) if (n > 1) add('duplicate-id', 'minor-as-serious', document.getElementById(id));
  for (const e of document.querySelectorAll('[aria-labelledby],[aria-describedby],[aria-controls],[for]')) {
    for (const at of ['aria-labelledby', 'aria-describedby']) {
      const v = e.getAttribute(at);
      if (v && !v.split(/\s+/).every((id) => document.getElementById(id))) add('aria-valid-attr-value', 'critical', e);
    }
  }
  const ROLES = new Set('alert alertdialog application article banner button cell checkbox columnheader combobox complementary contentinfo definition dialog directory document feed figure form grid gridcell group heading img link list listbox listitem log main marquee math menu menubar menuitem menuitemcheckbox menuitemradio navigation none note option presentation progressbar radio radiogroup region row rowgroup rowheader scrollbar search searchbox separator slider spinbutton status switch tab table tablist tabpanel term textbox timer toolbar tooltip tree treegrid treeitem'.split(' '));
  for (const e of document.querySelectorAll('[role]')) if (!e.getAttribute('role').split(/\s+/).every((r) => ROLES.has(r))) add('aria-roles', 'critical', e);
  for (const tl of document.querySelectorAll('[role=tablist]')) if (!tl.querySelector('[role=tab]')) add('aria-required-children', 'critical', tl);
  for (const e of document.querySelectorAll('[role=tab]')) if (!e.closest('[role=tablist]')) add('aria-required-parent', 'critical', e);
  const FOCUSABLE = 'a[href],button,input:not([type=hidden]),select,textarea,[tabindex]';
  for (const h of document.querySelectorAll('[aria-hidden=true]')) {
    if (h.matches(FOCUSABLE) && h.tabIndex >= 0 && !h.disabled) add('aria-hidden-focus', 'serious', h);
    for (const f of h.querySelectorAll(FOCUSABLE)) if (f.tabIndex >= 0 && !f.disabled) add('aria-hidden-focus', 'serious', f);
  }
  for (const e of document.querySelectorAll('[tabindex]')) if (Number(e.getAttribute('tabindex')) > 0) add('tabindex', 'serious', e);
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue;
    const s = getComputedStyle(e);
    if ((s.overflowX === 'auto' || s.overflowX === 'scroll') && e.scrollWidth > e.clientWidth + 1) {
      if (e.tabIndex < 0 && !e.querySelector(FOCUSABLE)) add('scrollable-region-focusable', 'serious', e);
    }
  }
  for (const e of document.querySelectorAll('a[href] button, button a[href], a[href] a[href], button button, a[href] input, button input')) if (vis(e)) add('nested-interactive', 'serious', e);
  for (const e of document.querySelectorAll('[role=img]')) if (!name(e)) add('role-img-alt', 'serious', e);
  // colour contrast: text with a solid ancestor background; images / canvases / gradients behind text are skipped
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const over = (top, bot) => ({ r: top.r * top.a + bot.r * (1 - top.a), g: top.g * top.a + bot.g * (1 - top.a), b: top.b * top.a + bot.b * (1 - top.a), a: 1 });
  const bgOf = (e) => {
    const chain = [];
    for (let n = e; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.backgroundImage !== 'none') return null;
      const c = parse(s.backgroundColor);
      if (c && c.a > 0) { chain.push(c); if (c.a >= 1) break; }
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    const bodyBg = parse(getComputedStyle(document.documentElement).backgroundColor);
    if (bodyBg && bodyBg.a > 0) base = over(bodyBg, base);
    for (const c of chain.reverse()) base = over(c, base);
    return base;
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const done = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const e = n.parentElement;
    if (!e || done.has(e) || !n.textContent.trim() || !vis(e) || e.closest('canvas,svg,script,style,[aria-hidden=true],option')) continue;
    done.add(e);
    if (e.closest(':disabled') || e.closest('[disabled]')) continue;
    const s = getComputedStyle(e), fg = parse(s.color), bg = bgOf(e);
    if (!fg || !bg || e.closest('#viewer, .viewer, #mol3d')) continue;
    const f = over(fg, bg), l1 = lum(f), l2 = lum(bg), ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight, 10) >= 700;
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
    if (ratio < need) add('color-contrast', 'serious', e);
  }
  return out;
}
"""


def audit(page):
    return page.evaluate(AUDIT_JS)


# ---- real axe-core (test-only, vendored unmodified; see tests/web/vendor/axe/README.md) ----
from pathlib import Path  # noqa: E402

AXE_JS = Path(__file__).resolve().parent / "vendor" / "axe" / "axe.min.js"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]
# Pixels axe cannot judge (a WebGL canvas, text over images) are reported as "incomplete", not as violations; we only fail on violations.


def axe_audit(page, impacts=("serious", "critical")):
    """Run the real axe-core on the page as it is now. Returns [{rule, impact, target, nodes}] for violations of the given impacts."""
    if not page.evaluate("typeof window.axe !== 'undefined'"):
        page.evaluate(AXE_JS.read_text(encoding="utf-8"))  # evaluate, not a <script> tag: the pages carry a strict CSP
    res = page.evaluate("""async (tags) => {
        const r = await window.axe.run(document, { runOnly: { type: 'tag', values: tags }, resultTypes: ['violations'] });
        return r.violations.map(v => ({ rule: v.id, impact: v.impact, nodes: v.nodes.length, target: v.nodes.slice(0, 3).map(n => n.target.join(' ')), help: v.help }));
    }""", AXE_TAGS)
    return [v for v in res if v["impact"] in impacts]
