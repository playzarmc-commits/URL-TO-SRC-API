"""Browser capture: loads a page, tracks loading screens (ms precision), collects every resource."""
import base64
import json
import os
import re
import time
from urllib.parse import unquote, urljoin, urlparse

from pack import source_path

MAX_BODY = int(os.environ.get("MAX_BODY_MB", "25")) * 1024 * 1024
MAX_TOTAL = int(os.environ.get("MAX_TOTAL_MB", "150")) * 1024 * 1024
MAX_EXTRA = int(os.environ.get("MAX_EXTRA_URLS", "200"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_M = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")

# Runs inside every page BEFORE site scripts. Watches for loading screens every ~40ms
# and stores start/end with performance.now() -> millisecond accuracy, no polling jitter.
INIT_JS = r"""
(() => {
  if (window.__u2s) return; window.__u2s = 1;
  window.__segs = []; window.__cur = null; window.__lastMut = performance.now();
  try { new MutationObserver(() => { window.__lastMut = performance.now(); })
        .observe(document, {subtree: true, childList: true, characterData: true}); } catch (e) {}
  const KEY = /load|splash|spinner|progress|curtain|intro|boot|wait|veil|cover/i;
  const clean = s => s.replace(/download|upload|lazyload|lazy-load|loaded|overload|unload/ig, '');
  const attrs = el => clean((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : ''));
  const info = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect(), vw = innerWidth, vh = innerHeight;
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.05) return null;
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (w < 2 || h < 2) return null;
    return {cover: (w * h) / (vw * vh), pos: s.position, z: parseInt(s.zIndex) || 0};
  };
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
  function detect() {
    if (!document.body) return null;
    let best = null;
    const consider = el => {
      const v = info(el); if (!v) return;
      const keyed = KEY.test(attrs(el));
      const fixedTop = v.pos === 'fixed' && v.z > 0 && v.cover >= 0.6;
      const keyedBig = keyed && (v.pos === 'fixed' || v.pos === 'absolute' || v.cover >= 0.25);
      if ((fixedTop || keyedBig) && (!best || v.cover > best.cover)) best = {sel: name(el), cover: +v.cover.toFixed(2)};
    };
    for (const [x, y] of [[.5,.5],[.1,.1],[.9,.1],[.1,.9],[.9,.9]]) {
      let el = document.elementFromPoint(innerWidth * x, innerHeight * y), d = 0;
      while (el && el !== document.body && el !== document.documentElement && d < 8) { consider(el); el = el.parentElement; d++; }
    }
    document.querySelectorAll('[id*="load" i],[class*="load" i],[class*="splash" i],[id*="splash" i],[class*="spinner" i],[class*="intro" i],[class*="curtain" i],[role="progressbar"]').forEach(consider);
    return best;
  }
  let last = -100;
  function tick() {
    const n = performance.now(); if (n - last < 35) return; last = n;
    try {
      const d = detect();
      if (d && !window.__cur) window.__cur = {sel: d.sel, start: n, cover: d.cover};
      else if (!d && window.__cur) { const c = window.__cur; c.end = n; c.dur = n - c.start; window.__segs.push(c); window.__cur = null; }
    } catch (e) {}
  }
  (function raf() { tick(); requestAnimationFrame(raf); })();
  setInterval(tick, 50);
})();
"""

STATE_JS = """() => ({now: performance.now(), ready: document.readyState,
  quiet: performance.now() - (window.__lastMut || 0), segs: window.__segs || [], cur: window.__cur || null})"""

SCROLL_JS = """async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let y = 0;
  for (let i = 0; i < 60; i++) {
    const h = document.documentElement.scrollHeight;
    if (y >= h) break;
    y += innerHeight * 0.9; scrollTo(0, y); await sleep(120);
  }
  scrollTo(0, 0); await sleep(150);
}"""

GATHER_JS = """() => {
  const u = new Set(), add = v => { if (!v) return; try { u.add(new URL(v, document.baseURI).href); } catch (e) {} };
  const skip = /canonical|alternate|dns-prefetch|preconnect|prev|next|author|me|search/i;
  document.querySelectorAll('img,source,video,audio,track,script,embed,object,input[type=image],link').forEach(e => {
    if (e.tagName === 'LINK' && skip.test(e.getAttribute('rel') || '')) return;
    ['src', 'href', 'poster', 'data-src', 'data-poster'].forEach(a => add(e.getAttribute(a)));
    ['srcset', 'data-srcset'].forEach(a => (e.getAttribute(a) || '').split(',').forEach(s => add(s.trim().split(/\\s+/)[0])));
  });
  document.querySelectorAll('[style*="url("]').forEach(e => {
    (e.getAttribute('style').match(/url\\(([^)]+)\\)/g) || []).forEach(m => add(m.slice(4, -1).replace(/["']/g, '')));
  });
  return [...u].filter(x => /^https?:/.test(x));
}"""

SM_RE = re.compile(r"sourceMappingURL=([^\s'\"*]+)")


def capture(browser, url, o, emit):
    t0 = time.perf_counter()
    ms = lambda: int((time.perf_counter() - t0) * 1000)
    mobile = o["mobile"]
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844} if mobile else {"width": 1366, "height": 768},
        user_agent=UA_M if mobile else UA, ignore_https_errors=True, bypass_csp=True,
        service_workers="block", is_mobile=mobile, has_touch=mobile)
    try:
        return _run(ctx, url, o, emit, ms)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _store(res, total, u, status, ctype, body, rtype, t, smap=None):
    total[0] += len(body)
    res[u] = {"url": u, "status": status, "content_type": ctype, "body": body,
              "resource_type": rtype, "t_ms": t, "sourcemap": smap}


def _run(ctx, url, o, emit, ms):
    page = ctx.new_page()
    page.add_init_script(INIT_JS)
    inflight, pending, res, skipped, marks, total = set(), [], {}, [], {}, [0]
    page.on("request", lambda r: inflight.add(r))
    page.on("requestfinished", lambda r: inflight.discard(r))
    page.on("requestfailed", lambda r: inflight.discard(r))
    page.on("domcontentloaded", lambda _: marks.setdefault("dom_content_loaded_ms", ms()))
    page.on("load", lambda _: marks.setdefault("load_ms", ms()))
    if o["assets"]:
        page.on("response", lambda r: pending.append((r, ms())))

    def drain():
        while pending:
            r, t = pending.pop(0)
            try:
                u = r.url.split("#")[0]
                if u in res or not u.startswith("http") or r.request.method != "GET":
                    continue
                st = r.status
                if st >= 400:
                    skipped.append({"url": u, "status": st, "reason": "http_error"})
                    continue
                if st >= 300 or st in (204, 206):
                    continue
                h = r.headers
                if int(h.get("content-length") or 0) > MAX_BODY:
                    skipped.append({"url": u, "reason": "too_large"})
                    continue
                body = r.body()
                if len(body) > MAX_BODY or total[0] + len(body) > MAX_TOTAL:
                    skipped.append({"url": u, "reason": "size_limit"})
                    continue
                _store(res, total, u, st, (h.get("content-type") or "").split(";")[0].strip().lower(),
                       body, r.request.resource_type, t, h.get("sourcemap") or h.get("x-sourcemap"))
            except Exception:
                pass

    try:
        resp = page.goto(url, wait_until="commit", timeout=int(o["max_wait"] * 1000))
    except Exception as e:
        raise RuntimeError(f"navigation failed: {e}")
    marks["first_byte_ms"] = ms()
    status = resp.status if resp else None

    epochs, last_now = [], -1

    def loader_total(d=None):
        t = 0
        for e in epochs:
            t += sum(s["dur"] for s in e["segs"])
        if d and d["cur"]:
            t += d["now"] - d["cur"]["start"]
        return int(t)

    def observe(d):
        nonlocal last_now
        off = ms() - d["now"]
        if not epochs or d["now"] < last_now:          # new document (navigation / redirect)
            if epochs and epochs[-1]["cur"]:
                p = epochs[-1]
                p["segs"].append({**p["cur"], "end": p["last"], "dur": p["last"] - p["cur"]["start"], "nav": True})
                p["cur"] = None
            epochs.append({"off": off, "segs": [], "cur": None, "last": 0, "seen": 0, "open": False})
        e = epochs[-1]
        e["off"] = min(e["off"], off)
        e["last"] = d["now"]
        last_now = d["now"]
        for s in d["segs"][e["seen"]:]:
            if not e["open"]:
                emit({"type": "loader_start", "at_ms": int(e["off"] + s["start"]), "selector": s["sel"]})
            emit({"type": "loader_end", "at_ms": int(e["off"] + s["end"]), "selector": s["sel"],
                  "duration_ms": int(s["dur"])})
            e["seen"] += 1
            e["open"] = False
        e["segs"], e["cur"] = d["segs"], d["cur"]
        if d["cur"] and not e["open"]:
            emit({"type": "loader_start", "at_ms": int(e["off"] + d["cur"]["start"]), "selector": d["cur"]["sel"]})
            e["open"] = True

    deadline = int(o["max_wait"] * 1000)
    last_tick, load_seen_at, reason, d = 0, None, "max_wait", None
    while ms() < deadline:
        drain()
        try:
            d = page.evaluate(STATE_JS)
        except Exception:
            time.sleep(0.05)
            continue
        observe(d)
        now = ms()
        if now - last_tick >= 1000:
            last_tick = now
            emit({"type": "tick", "elapsed_s": round(now / 1000, 1), "elapsed_ms": now,
                  "ready_state": d["ready"], "loading_screen_visible": bool(d["cur"]),
                  "loader": d["cur"]["sel"] if d["cur"] else None,
                  "loading_screen_total_ms": loader_total(d), "requests_in_flight": len(inflight),
                  "resources_captured": len(res), "dom_quiet_ms": int(d["quiet"])})
        if d["ready"] == "complete" and load_seen_at is None:
            load_seen_at = now
        ok = (d["ready"] == "complete" and not d["cur"] and now >= 300 and len(inflight) <= 2
              and (d["quiet"] >= o["settle_ms"] or now - load_seen_at >= max(o["settle_ms"] * 3, 2500)))
        if ok and o["wait_selector"]:
            try:
                ok = page.evaluate(f"!!document.querySelector({json.dumps(o['wait_selector'])})")
            except Exception:
                ok = False
        if ok:
            reason = "settled"
            break
        time.sleep(0.05)
    settled_ms = ms()

    if o["scroll"]:
        emit({"type": "phase", "phase": "scrolling", "elapsed_ms": ms()})
        try:
            page.evaluate(SCROLL_JS)
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        try:
            observe(page.evaluate(STATE_JS))
        except Exception:
            pass
    drain()

    if o["assets"]:
        emit({"type": "phase", "phase": "collecting_assets", "elapsed_ms": ms(), "captured": len(res)})
        final_url = page.url
        _fetch_extra(ctx, page, res, total, skipped, final_url, 8)
        if o["sourcemaps"]:
            sources = _fetch_maps(ctx, res, total, final_url, 6)
        else:
            sources = {}
    else:
        sources, final_url = {}, page.url

    emit({"type": "phase", "phase": "packing", "elapsed_ms": ms()})
    html = page.content()
    title = ""
    try:
        title = page.title()
    except Exception:
        pass
    frames = []
    try:
        for f in page.frames[1:]:
            if f.url.startswith("http"):
                frames.append((f.url, f.content()))
    except Exception:
        pass
    nav = None
    try:
        nav = page.evaluate("() => { const n = performance.getEntriesByType('navigation')[0]; return n ? n.toJSON() : null; }")
    except Exception:
        pass

    segs = []
    for e in epochs:
        allseg = list(e["segs"])
        if e["cur"]:
            allseg.append({**e["cur"], "end": e["last"], "dur": e["last"] - e["cur"]["start"], "open": True})
        for s in allseg:
            segs.append({"selector": s["sel"], "start_ms": int(e["off"] + s["start"]),
                         "end_ms": int(e["off"] + s["end"]), "duration_ms": int(s["dur"]),
                         "coverage": s.get("cover"), "still_visible_at_end": bool(s.get("open"))})
    return {
        "html": html, "title": title, "status": status, "final_url": final_url,
        "resources": list(res.values()), "skipped": skipped, "sources": sources, "frames": frames,
        "finish_reason": reason,
        "timings": {**marks, "settled_ms": settled_ms, "total_ms": ms(),
                    "loading_screen_detected": bool(segs),
                    "loading_screen_total_ms": sum(s["duration_ms"] for s in segs),
                    "loading_screens": segs, "navigation_timing": nav},
    }


def _fetch_extra(ctx, page, res, total, skipped, final_url, budget_s):
    """Fetch URLs present in the DOM that the browser never requested (lazy / below-fold / srcset)."""
    try:
        urls = page.evaluate(GATHER_JS)
    except Exception:
        return
    end, n = time.time() + budget_s, 0
    for u in urls:
        u = u.split("#")[0]
        if u in res or n >= MAX_EXTRA or time.time() > end:
            continue
        try:
            r = ctx.request.get(u, timeout=6000, headers={"Referer": final_url})
            if r.status != 200:
                skipped.append({"url": u, "status": r.status, "reason": "http_error"})
                continue
            body = r.body()
            if len(body) > MAX_BODY or total[0] + len(body) > MAX_TOTAL:
                continue
            _store(res, total, u, 200, (r.headers.get("content-type") or "").split(";")[0].strip().lower(),
                   body, "extra", None)
            n += 1
        except Exception:
            pass


def _fetch_maps(ctx, res, total, final_url, budget_s):
    """Follow sourceMappingURL -> recover original TS/JSX/SCSS sources (sourcesContent)."""
    sources, end, cands = {}, time.time() + budget_s, []
    for r in list(res.values()):
        ct, path = r["content_type"], urlparse(r["url"]).path.lower()
        if not ("javascript" in ct or "css" in ct or re.search(r"\.(m?js|css)$", path)):
            continue
        m = SM_RE.search(r["body"][-600:].decode("utf-8", "ignore"))
        ref = m.group(1) if m else r.get("sourcemap")
        if ref:
            cands.append((r["url"], ref))
    for u, ref in cands:
        if time.time() > end:
            break
        try:
            if ref.startswith("data:"):
                head, _, payload = ref.partition(",")
                raw = base64.b64decode(payload) if "base64" in head else unquote(payload).encode()
                mu = u + ".inline.map"
            else:
                mu = urljoin(u, ref).split("#")[0]
                if mu in res:
                    raw = res[mu]["body"]
                else:
                    r = ctx.request.get(mu, timeout=6000, headers={"Referer": final_url})
                    if r.status != 200:
                        continue
                    raw = r.body()
                    if total[0] + len(raw) <= MAX_TOTAL:
                        _store(res, total, mu, 200, "application/json", raw, "sourcemap", None)
            smap = json.loads(raw)
            cont = smap.get("sourcesContent") or []
            for i, name in enumerate(smap.get("sources") or []):
                if i < len(cont) and cont[i] is not None:
                    sources[source_path(mu, name)] = cont[i]
        except Exception:
            continue
    return sources
