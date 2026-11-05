"""F-121: Ketcher runs in <iframe sandbox="allow-scripts"> (opaque origin) behind a postMessage bridge.

Browser tests use the header-applying site (hsite) so the real CSP, including its `sandbox` directive, is enforced.
"""
import hashlib
import re
from pathlib import Path

from mockapi import PID

WEB = Path(__file__).resolve().parents[2] / "web"


def _open_draw(pg):
    pg.goto(f"{pg.base}/add.html?p={PID}")
    pg.wait_for_selector("#add-frame:not([hidden])")
    pg.click("#t-draw")
    pg.wait_for_selector("#ketcher")
    pg.wait_for_function("() => document.getElementById('ketcher').contentWindow !== null")
    fr = next(f for f in pg.frames if "draw/frame.html" in f.url)
    fr.wait_for_function("() => !!window.ketcher", timeout=90000)
    return fr


def test_ketcher_frame_is_sandboxed_without_same_origin():
    html = (WEB / "add.html").read_text()
    tag = re.search(r"<iframe[^>]*id=\"ketcher\"[^>]*>", html).group(0)
    flags = re.search(r"\bsandbox=\"([^\"]*)\"", tag).group(1).split()
    assert flags == ["allow-scripts"]
    assert 'data-src="draw/frame.html"' in tag


def test_frame_page_loads_exactly_the_pinned_bundle_files():
    t = (WEB / "draw" / "frame.html").read_text()
    v = (WEB / "vendor" / "VERSIONS.txt").read_text()
    idx = (WEB / "vendor" / "ketcher" / "index.html").read_text()
    for ref in re.findall(r"(?:src|href)=\"(static/[^\"]+)\"", t):
        assert ref in idx, ref                                     # same files the vendored index.html loads
        assert hashlib.sha256((WEB / "vendor" / "ketcher" / ref).read_bytes()).hexdigest() in v, ref
    assert len(re.findall(r"static/(?:js|css)/main", t)) == 2
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", t) and not re.search(r"\son[a-z]+\s*=", t)
    assert not re.search(r"(?:src|href)=[\"']?(?:https?:)?//", t)


def test_every_message_handler_checks_source_and_origin():
    for name, needles in (("client.js", ["ev.source !== iframe.contentWindow", 'ev.origin !== "null"']),
                          ("bridge.js", ["ev.source !== window.parent", "ev.origin !== parentOrigin"])):
        t = (WEB / "draw" / name).read_text()
        assert all(n in t for n in needles), name
    # postMessage is used nowhere else in the site's own scripts
    for p in WEB.rglob("*.js"):
        if "vendor" in p.parts or "draw" in p.parts:
            continue
        assert not re.search(r"postMessage|addEventListener\(\s*[\"']message", p.read_text()), p


def test_draw_structure_adds_a_compound_through_the_bridge(make_hpage):
    pg = make_hpage()
    fr = _open_draw(pg)
    fr.evaluate("window.ketcher.setMolecule('CCCCCCO')")                     # a user drawing; the page then reads it through the bridge
    pg.wait_for_timeout(500)
    pg.wait_for_function("() => document.getElementById('ketcher').contentWindow !== null")
    pg.click("#add-submit")
    pg.wait_for_function("() => { const m = document.getElementById('add-msg'); return m.dataset.kind && !m.textContent.startsWith('Sending'); }", timeout=30000)
    assert "Added 1 compound" in pg.inner_text("#add-msg"), pg.inner_text("#add-msg")
    posts = [c for c in pg.api.calls if c[0] == "POST" and c[1].endswith("/compounds")]
    assert posts and posts[-1][2]["format"] == "smiles" and posts[-1][2]["smiles"] == ["CCCCCCO"]
    assert [e for e in pg.errors if re.search(r"Content Security Policy|Refused to", e)] == []


def test_page_can_set_and_get_a_molecule_over_the_bridge_only(make_hpage):
    pg = make_hpage()
    _open_draw(pg)
    got = pg.evaluate("""() => new Promise((res) => {
        const f = document.getElementById('ketcher'); let n = 0; const out = [];
        addEventListener('message', function h(e) {
          if (e.source !== f.contentWindow || !e.data || typeof e.data.id !== 'number') return;
          out.push(e.data); n++;
          if (n === 1) f.contentWindow.postMessage({ metis_draw: 1, id: 2, cmd: 'getSmiles' }, '*');
          if (n === 2) { removeEventListener('message', h); res(out); }
        });
        f.contentWindow.postMessage({ metis_draw: 1, id: 1, cmd: 'setMolecule', arg: 'CCCCO' }, '*');
    })""")
    assert got[0] == {"metis_draw": 1, "id": 1, "result": ""}
    assert got[1]["id"] == 2 and got[1]["result"] == "CCCCO"


def test_bridge_ignores_unknown_commands_and_foreign_senders(make_hpage):
    pg = make_hpage()
    fr = _open_draw(pg)
    # an unknown command and a malformed message get no answer
    heard = pg.evaluate("""() => new Promise((res) => {
        const f = document.getElementById('ketcher'); const out = [];
        addEventListener('message', (e) => { if (e.source === f.contentWindow) out.push(e.data); });
        for (const m of [{ metis_draw: 1, id: 5, cmd: 'eval', arg: 'alert(1)' }, { metis_draw: 1, id: 'x', cmd: 'getSmiles' }, { id: 6, cmd: 'getSmiles' }, 'getSmiles', null, { metis_draw: 1, id: 7, cmd: '__proto__' }])
          f.contentWindow.postMessage(m, '*');
        setTimeout(() => res(out), 600);
    })""")
    assert heard == []
    # a window that is not the frame's parent (here: the frame itself posting to itself) is ignored too: the parent hears nothing back
    pg.evaluate("() => { window.__heard = []; addEventListener('message', (e) => { if (e.data && e.data.id === 9) window.__heard.push(e.data); }); }")
    fr.evaluate("() => postMessage({ metis_draw: 1, id: 9, cmd: 'getSmiles' }, '*')")
    pg.wait_for_timeout(600)
    assert pg.evaluate("window.__heard") == []


def test_frame_stays_inert_when_opened_on_its_own_or_with_a_bad_origin(make_hpage):
    pg = make_hpage()
    for q in ("", "?o=https://evil.example/path", "?o=javascript:1", "?o=null"):
        pg.goto(f"{pg.base}/draw/frame.html{q}")
        pg.wait_for_function("() => !!window.ketcher", timeout=90000)
        assert pg.evaluate("window.origin") == "null"                      # CSP `sandbox` makes even a direct visit opaque
        heard = pg.evaluate("""() => new Promise((res) => { let n = 0; addEventListener('message', () => n++); postMessage({ metis_draw: 1, id: 1, cmd: 'getSmiles' }, '*'); setTimeout(() => res(n), 400); })""")
        assert heard == 1, q                                                # only our own probe: the bridge never answered


ESCAPES = """async () => {
  const r = {};
  const t = async (k, f) => { try { r[k] = await f(); } catch (e) { r[k] = 'blocked: ' + (e && e.name); } };
  r.origin = window.origin;
  await t('parent_dom', () => parent.document.title);
  await t('parent_cookie', () => parent.document.cookie);
  await t('own_cookie', () => document.cookie);
  await t('parent_storage', () => parent.localStorage.length);
  await t('top_nav', () => { top.location.href = 'https://attacker.invalid/?x=1'; return 'navigated'; });
  await t('fetch_api', async () => { const res = await fetch('/api/account/data', { credentials: 'include' }); return 'read ' + res.status + ' ' + (await res.text()); });
  await t('opener', () => window.opener && window.opener.document.title);
  return r;
}"""


def _assert_isolated(pg, fr, base, fetch_blocked=True):
    r = fr.evaluate(ESCAPES)
    assert r["origin"] == "null"
    for k in ("parent_dom", "parent_cookie", "own_cookie", "parent_storage", "top_nav") + (("fetch_api",) if fetch_blocked else ()):
        assert str(r[k]).startswith("blocked"), (k, r[k])
    assert not r["opener"] or str(r["opener"]).startswith("blocked") or r["opener"] is None
    pg.wait_for_timeout(300)
    assert pg.url.startswith(base) and "attacker" not in pg.url               # the top window did not move


def test_sandbox_attribute_alone_isolates_the_bundle(make_page):
    """Plain static server, NO CSP: the iframe `sandbox` attribute is what isolates (defence does not depend on the header)."""
    pg = make_page()
    seen = []
    pg.on("request", lambda r: seen.append(r.all_headers()) if "/api/account/data" in r.url else None)
    fr = _open_draw(pg)
    _assert_isolated(pg, fr, pg.base, fetch_blocked=False)   # without a CSP the request may leave, but as an anonymous cross-origin one
    pg.wait_for_timeout(300)
    assert seen, "the probe request should have been issued"
    for h in seen:
        names = {k.lower() for k in h}
        assert "cookie" not in names and "x-csrf-token" not in names, h
        assert h.get("origin") in (None, "null"), h


def test_bundle_cannot_reach_the_page_or_api_under_the_real_headers(make_hpage):
    pg = make_hpage()
    seen = []
    pg.on("request", lambda r: seen.append((r.url, dict(r.headers))) if "/api/account/data" in r.url else None)
    fr = _open_draw(pg)
    _assert_isolated(pg, fr, pg.base)
    assert seen == []                                                         # connect-src 'none' stops the request before it leaves
    assert pg.evaluate("document.cookie") == "metis_csrf=testtoken"           # and the page's own state is untouched
    assert pg.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0   # Ketcher wrote nothing into the app origin
