"""Browser capture engine v3: loader tracking (ms), every asset, crawl, shadow DOM, screenshot, logs."""
import base64
import json
import os
import re
import time
from collections import deque
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import netutil
from pack import source_path

MAX_BODY = int(os.environ.get("MAX_BODY_MB", "25")) * 1024 * 1024
MAX_TOTAL = int(os.environ.get("MAX_TOTAL_MB", "150")) * 1024 * 1024
MAX_EXTRA = int(os.environ.get("MAX_EXTRA_URLS", "200"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_M = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")

STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
  try { window.chrome = window.chrome || {runtime: {}, loadTimes: () => ({}), csi: () => ({})}; } catch (e) {}
  try {
    const gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return gp.call(this, p);
    };
  } catch (e) {}
  try {
    const oq = navigator.permissions && navigator.permissions.query;
    if (oq) navigator.permissions.query = p => p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission}) : oq.call(navigator.permissions, p);
  } catch (e) {}
})();
"""

# Runs in every frame BEFORE site scripts: forces open shadow roots + ms-accurate loading-screen watcher.
INIT_JS = r"""
(() => {
  if (window.__u2s) return; window.__u2s = 1;
  try {
    const _as = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function (i) { return _as.call(this, Object.assign({}, i, {mode: 'open'})); };
  } catch (e) {}
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
  const ROOT_ID = /^(app|root|__next|__nuxt|__layout|___gatsby|page-root|site|main|wrapper|container)$/i;
  function detect() {
    if (!document.body) return null;
    let best = null;
    const consider = el => {
      if (el.id && ROOT_ID.test(el.id)) return;   // common SPA app-root: almost never a real loader
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
  quiet: performance.now() - (window.__lastMut || 0), segs: window.__segs || [], cur: window.__cur || null,
  imgs: Array.prototype.filter.call(document.images || [], i => !i.complete && i.loading !== 'lazy').length,
  fonts: document.fonts ? document.fonts.status : 'loaded'})"""

SCROLL_JS = """async (fast) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelectorAll('img[loading="lazy"],iframe[loading="lazy"]').forEach(e => { e.loading = 'eager'; });
  const vh = innerHeight;
  if (document.documentElement.scrollHeight <= vh * 1.1) return;
  let y = 0;
  for (let i = 0; i < 80; i++) {
    const h = document.documentElement.scrollHeight;
    if (y >= h) break;
    y += vh * (fast ? 1.2 : 0.9); scrollTo(0, y); await sleep(fast ? 40 : 120);
  }
  scrollTo(0, 0); await sleep(fast ? 30 : 150);
}"""

SCROLL_START_JS = """(fast) => {
  window.__u2sScroll = 0;
  (async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    document.querySelectorAll('img[loading="lazy"],iframe[loading="lazy"]').forEach(e => { e.loading = 'eager'; });
    const vh = innerHeight;
    if (document.documentElement.scrollHeight > vh * 1.1) {
      let y = 0;
      for (let i = 0; i < 80; i++) {
        const h = document.documentElement.scrollHeight;
        if (y >= h) break;
        y += vh * (fast ? 1.2 : 0.9); scrollTo(0, y); await sleep(fast ? 40 : 120);
      }
      scrollTo(0, 0); await sleep(fast ? 30 : 150);
    }
    window.__u2sScroll = 1;
  })();
  return 1;
}"""

READY_JS = """async (maxms) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try { await Promise.race([document.fonts.ready, sleep(maxms)]); } catch (e) {}
  try { await Promise.race([Promise.all(Array.prototype.map.call(document.images, i => i.decode ? i.decode().catch(() => {}) : 0)), sleep(maxms)]); } catch (e) {}
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

META_JS = """() => {
  const q = s => document.querySelector(s);
  const m = n => { const e = q('meta[name="' + n + '"],meta[property="' + n + '"]'); return e ? e.content : null; };
  const links = [];
  document.querySelectorAll('a[href]').forEach(a => { try {
    links.push({href: new URL(a.getAttribute('href'), document.baseURI).href, text: (a.textContent || '').trim().slice(0, 80), rel: a.rel || ''});
  } catch (e) {} });
  const heads = {};
  ['h1', 'h2', 'h3'].forEach(h => heads[h] = Array.prototype.map.call(document.querySelectorAll(h), e => e.textContent.trim().slice(0, 120)).slice(0, 30));
  const t = [], has = (c, n) => { if (c) t.push(n); };
  has(window.__NEXT_DATA__, 'Next.js'); has(window.__NUXT__ || window.$nuxt, 'Nuxt');
  has(window.React || window.__REACT_DEVTOOLS_GLOBAL_HOOK__ && q('#root,[data-reactroot]'), 'React');
  has(window.Vue || q('[data-v-app],[data-server-rendered]'), 'Vue'); has(q('[ng-version]'), 'Angular');
  has(window.angular, 'AngularJS'); has(q('[class*="svelte-"]'), 'Svelte'); has(q('#___gatsby'), 'Gatsby');
  has(window.jQuery, 'jQuery ' + (window.jQuery && jQuery.fn ? jQuery.fn.jquery : ''));
  has(window.Shopify, 'Shopify'); has(window.wixBiSession, 'Wix'); has(q('html[data-wf-page]'), 'Webflow');
  has(window.Static && window.Static.SQUARESPACE_CONTEXT, 'Squarespace');
  has(q('link[href*="wp-content"],script[src*="wp-content"]') || (m('generator') || '').indexOf('WordPress') > -1, 'WordPress');
  has(window.gsap, 'GSAP'); has(window.bootstrap || q('link[href*="bootstrap"]'), 'Bootstrap');
  has(window.THREE, 'three.js'); has(window.Alpine, 'Alpine.js'); has(window.htmx, 'htmx');
  return {meta: {title: document.title, description: m('description'), keywords: m('keywords'),
    canonical: (q('link[rel=canonical]') || {}).href || null, lang: document.documentElement.lang || null,
    generator: m('generator'), viewport: m('viewport'), charset: document.characterSet, theme_color: m('theme-color'),
    og: {title: m('og:title'), image: m('og:image'), description: m('og:description'), type: m('og:type')},
    twitter: {card: m('twitter:card')}},
    headings: heads, links: links.slice(0, 5000), tech: t,
    counts: {scripts: document.scripts.length, stylesheets: document.styleSheets.length, images: document.images.length,
      forms: document.forms.length, iframes: document.querySelectorAll('iframe').length,
      dom_nodes: document.getElementsByTagName('*').length}};
}"""

SHADOW_JS = """() => {
  const roots = [];
  const walk = n => (n.querySelectorAll ? n.querySelectorAll('*') : []).forEach(e => {
    if (e.shadowRoot) { roots.push(e.shadowRoot); walk(e.shadowRoot); } });
  walk(document);
  if (!roots.length) return null;
  try { return '<!DOCTYPE html>\\n' + document.documentElement.getHTML({serializableShadowRoots: true, shadowRoots: roots}); }
  catch (e) { return null; }
}"""

IGNORE_TYPES = ("eventsource", "websocket", "ping", "media", "texttrack")


def busy(inflight, now):
    """Requests that really block "network idle": long-lived/old ones (long-poll, beacons) are ignored."""
    return sum(1 for t in inflight.values() if now - t < 8000)


AUTO_UNLOCK_JS = r"""
(arg) => {
  const excluded = (arg && arg.excluded) || [], extra = (arg && arg.extra) || [];
  const WHITELIST = new Set(["continue","enter","enter site","get key","generate key","generate",
    "unlock","view content","reveal key","reveal","show key","proceed","i agree","agree",
    "agree and continue","watch now","start","get started","open","continue to site","click here",
    "tap to continue","tap here","unlock content","show content","continue reading","view key",
    "enter now","verify","next","ok","got it","close","dismiss","i understand"].concat(extra || []));
  const BLOCK = /subscribe|sign\s*up|register|login|log\s*in|buy|order|checkout|pay|donate|delete|remove|send\s*message|submit\s*order|confirm\s*payment/i;
  const norm = s => (s || '').replace(/[\u00a0]/g, ' ').trim().toLowerCase().replace(/[^\w\s>]/g, '').replace(/\s+/g, ' ');
  const exSet = new Set((excluded || []).map(norm));
  const cands = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button], div[onclick], span[onclick]')];
  let best = null, bestScore = -1;
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) < 0.1) continue;
    const raw = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (!raw || raw.length > 40) continue;
    const n = norm(raw);
    if (exSet.has(n) || BLOCK.test(n) || !WHITELIST.has(n)) continue;
    const form = el.closest('form');
    if (form) {
      const fields = form.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea');
      if (fields.length > 0) continue;   // real form with user-input fields: do not auto-submit it
    }
    const area = r.width * r.height, z = parseInt(s.zIndex) || 0, score = area + z * 500;
    if (score > bestScore) { bestScore = score; best = {el, text: raw}; }
  }
  if (!best) return null;
  try { best.el.scrollIntoView({block: "center"}); } catch (e) {}
  try { best.el.click(); } catch (e) { return null; }
  return {text: best.text.slice(0, 60)};
}
"""


SM_RE = re.compile(r"sourceMappingURL=([^\s'\"*]+)")
BLOCK_RE = re.compile(r"just a moment|attention required|access denied|captcha|verify you are human|"
                      r"are you a robot|unusual traffic|request blocked|enable javascript and cookies", re.I)
SKIP_EXT = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|ico|css|m?js|json|xml|pdf|zip|gz|rar|7z|mp3|mp4|webm|avi|mov|"
                      r"woff2?|ttf|otf|eot|map|txt|csv|xlsx?|docx?|pptx?|exe|dmg|apk)$", re.I)


# ------------------------------------------------------------------ collector (shared by all pages)
class Collector:
    def __init__(self, o):
        self.o = o
        self.res, self.skipped, self.total = {}, [], [0]
        self.pending, self.log, self.console, self.errors = [], [], [], []

    def attach(self, page, ms):
        inflight = {}

        def on_req(r):
            if r.resource_type not in IGNORE_TYPES:
                inflight[r] = ms()

        page.on("request", on_req)
        page.on("requestfinished", lambda r: inflight.pop(r, None))

        def failed(r):
            inflight.pop(r, None)
            if len(self.log) < 4000:
                self.log.append({"url": r.url[:300], "method": r.method, "type": r.resource_type,
                                 "failed": r.failure, "t_ms": ms()})

        page.on("requestfailed", failed)
        page.on("response", lambda r: self.pending.append((r, ms())))
        page.on("console", lambda m: len(self.console) < 300 and self.console.append(
            {"type": m.type, "text": m.text[:400]}))
        page.on("pageerror", lambda e: len(self.errors) < 100 and self.errors.append(str(e)[:400]))
        return inflight

    def store(self, u, status, ctype, body, rtype, t, smap=None):
        self.total[0] += len(body)
        self.res[u] = {"url": u, "status": status, "content_type": ctype, "body": body,
                       "resource_type": rtype, "t_ms": t, "sourcemap": smap}

    def drain(self):
        while self.pending:
            r, t = self.pending.pop(0)
            try:
                u, st, h = r.url.split("#")[0], r.status, r.headers
                if len(self.log) < 4000:
                    self.log.append({"url": u[:300], "method": r.request.method, "status": st,
                                     "type": r.request.resource_type, "t_ms": t,
                                     "content_type": (h.get("content-type") or "")[:60]})
                if (not self.o["assets"] or u in self.res or not u.startswith("http")
                        or r.request.method != "GET"):
                    continue
                if st >= 400:
                    self.skipped.append({"url": u, "status": st, "reason": "http_error"})
                    continue
                if st >= 300 or st in (204, 206):
                    continue
                if int(h.get("content-length") or 0) > MAX_BODY:
                    self.skipped.append({"url": u, "reason": "too_large"})
                    continue
                body = r.body()
                if len(body) > MAX_BODY or self.total[0] + len(body) > MAX_TOTAL:
                    self.skipped.append({"url": u, "reason": "size_limit"})
                    continue
                self.store(u, st, (h.get("content-type") or "").split(";")[0].strip().lower(), body,
                           r.request.resource_type, t, h.get("sourcemap") or h.get("x-sourcemap"))
            except Exception:
                pass


# ------------------------------------------------------------------ loading-screen watcher
class Watch:
    def __init__(self, ms, emit, label=None):
        self.ms, self.emit, self.label = ms, emit, label
        self.epochs, self.last_now = [], -1

    def _ev(self, **kw):
        if self.label:
            kw["page"] = self.label
        self.emit(kw)

    def observe(self, d):
        off = self.ms() - d["now"]
        if not self.epochs or d["now"] < self.last_now:      # new document (navigation / redirect)
            if self.epochs and self.epochs[-1]["cur"]:
                p = self.epochs[-1]
                p["segs"].append({**p["cur"], "end": p["last"], "dur": p["last"] - p["cur"]["start"], "nav": True})
                p["cur"] = None
            self.epochs.append({"off": off, "segs": [], "cur": None, "last": 0, "seen": 0, "open": False})
        e = self.epochs[-1]
        e["off"] = min(e["off"], off)
        e["last"] = self.last_now = d["now"]
        for s in d["segs"][e["seen"]:]:
            if not e["open"]:
                self._ev(type="loader_start", at_ms=int(e["off"] + s["start"]), selector=s["sel"])
            self._ev(type="loader_end", at_ms=int(e["off"] + s["end"]), selector=s["sel"], duration_ms=int(s["dur"]))
            e["seen"] += 1
            e["open"] = False
        e["segs"], e["cur"] = d["segs"], d["cur"]
        if d["cur"] and not e["open"]:
            self._ev(type="loader_start", at_ms=int(e["off"] + d["cur"]["start"]), selector=d["cur"]["sel"])
            e["open"] = True

    def total(self, d=None):
        t = sum(s["dur"] for e in self.epochs for s in e["segs"])
        if d and d["cur"]:
            t += d["now"] - d["cur"]["start"]
        return int(t)

    def segments(self):
        out = []
        for e in self.epochs:
            allseg = list(e["segs"])
            if e["cur"]:
                allseg.append({**e["cur"], "end": e["last"], "dur": e["last"] - e["cur"]["start"], "open": True})
            for s in allseg:
                out.append({"selector": s["sel"], "start_ms": int(e["off"] + s["start"]),
                            "end_ms": int(e["off"] + s["end"]), "duration_ms": int(s["dur"]),
                            "coverage": s.get("cover"), "still_visible_at_end": bool(s.get("open"))})
        return out


def _do_clicks(page, sels, emit, ms):
    for s in sels:
        try:
            el = page.query_selector(s)
            if el:
                el.click(timeout=1500)
                emit({"type": "click", "selector": s, "elapsed_ms": ms()})
        except Exception:
            pass
    time.sleep(0.2)


class Settler:
    """One poll step at a time, so many pages can be settled concurrently (cooperative, single thread)."""

    def __init__(self, page, watch, inflight, coll, o, emit, ms, deadline, label=None, click=True):
        self.page, self.watch, self.inflight, self.coll, self.o = page, watch, inflight, coll, o
        self.emit, self.ms, self.deadline, self.label, self.click = emit, ms, deadline, label, click
        self.start = self.last_tick = ms()
        self.load_seen_at = self.idle_since = self.loader_open_since = None
        self.clicked, self.reason, self.d = False, None, None
        self.stuck_warned = False

    def step(self):
        o, ms = self.o, self.ms
        if ms() >= self.deadline:
            self.reason = "max_wait"
            return True
        try:
            d = self.page.evaluate(STATE_JS)
        except Exception:
            return False
        self.d = d
        self.watch.observe(d)
        now = ms()
        if now - self.last_tick >= 1000:
            self.last_tick = now
            ev = {"type": "tick", "elapsed_s": round(now / 1000, 1), "elapsed_ms": now, "ready_state": d["ready"],
                  "loading_screen_visible": bool(d["cur"]), "loader": d["cur"]["sel"] if d["cur"] else None,
                  "loading_screen_total_ms": self.watch.total(d), "requests_in_flight": len(self.inflight),
                  "resources_captured": len(self.coll.res), "dom_quiet_ms": int(d["quiet"])}
            if self.label:
                ev["page"] = self.label
            self.emit(ev)
        if busy(self.inflight, now) == 0:
            if self.idle_since is None:
                self.idle_since = now
        else:
            self.idle_since = None
        if d["cur"]:
            self.loader_open_since = self.loader_open_since or now
        else:
            self.loader_open_since = None
        if d["ready"] == "complete" and self.load_seen_at is None:
            self.load_seen_at = now
        if d["ready"] == "complete" and self.click and o["click"] and not self.clicked:
            self.clicked = True
            _do_clicks(self.page, o["click"], self.emit, ms)
        settle_ms, idle_ms = o["settle_ms"], o.get("idle_ms", 350)
        # Safety valve: a "loading screen" that never goes away (false positive, e.g. an app-root we
        # didn't recognise) must not be allowed to stall the whole job until max_wait.
        stuck_ms = max(settle_ms * 8, 10000)
        stuck = (self.loader_open_since is not None and now - self.loader_open_since >= stuck_ms
                and d["ready"] == "complete" and busy(self.inflight, now) == 0)
        if stuck and not self.stuck_warned:
            self.stuck_warned = True
            ev = {"type": "loader_ignored", "selector": d["cur"]["sel"] if d["cur"] else None,
                  "reason": f"open for over {stuck_ms}ms without closing; treating as not a real loading "
                            "screen so the job can finish", "elapsed_ms": now}
            if self.label:
                ev["page"] = self.label
            self.emit(ev)
        base_ok = d["ready"] == "complete" and (not d["cur"] or stuck) and now - self.start >= o.get("min_ms", 200)
        idle_ok = self.idle_since is not None and now - self.idle_since >= idle_ms
        quiet_ok = (stuck or d["quiet"] >= settle_ms) and d["imgs"] == 0 and d["fonts"] != "loading"
        fallback = self.load_seen_at is not None and now - self.load_seen_at >= max(settle_ms * 3, 2000)
        ok = base_ok and ((idle_ok and quiet_ok) or fallback or stuck)
        if ok and o["wait_selector"] and self.label is None:
            try:
                ok = self.page.evaluate(f"!!document.querySelector({json.dumps(o['wait_selector'])})")
            except Exception:
                ok = False
        if ok:
            self.reason = "settled"
            return True
        return False


def settle(page, watch, inflight, coll, o, emit, ms, deadline, label=None, click=True):
    st = Settler(page, watch, inflight, coll, o, emit, ms, deadline, label, click)
    while True:
        coll.drain()
        if st.step():
            return st.reason, st.d
        page.wait_for_timeout(20)       # also lets Playwright deliver network events


def wait_net_idle(page, inflight, coll, ms, quiet_ms, max_ms):
    t0, idle_since = ms(), None
    while ms() - t0 < max_ms:
        coll.drain()
        now = ms()
        if busy(inflight, now) == 0:
            idle_since = idle_since if idle_since is not None else now
            if now - idle_since >= quiet_ms:
                return
        else:
            idle_since = None
        page.wait_for_timeout(20)


# ------------------------------------------------------------------ main entry
def capture(browser, url, o, emit):
    t0 = time.perf_counter()
    ms = lambda: int((time.perf_counter() - t0) * 1000)
    mobile = o["mobile"]
    vw = o.get("viewport_w") or (390 if mobile else 1366)
    vh = o.get("viewport_h") or (844 if mobile else 768)
    kw = dict(viewport={"width": int(vw), "height": int(vh)}, user_agent=o.get("user_agent") or (UA_M if mobile else UA),
              ignore_https_errors=True, bypass_csp=True, service_workers="block", is_mobile=mobile,
              has_touch=mobile, locale="en-US")
    if o.get("headers"):
        kw["extra_http_headers"] = o["headers"]
    ctx = browser.new_context(**kw)
    try:
        if o.get("cookies"):
            cks = []
            for c in o["cookies"]:
                c = dict(c)
                if "url" not in c and "domain" not in c:
                    c["url"] = url
                cks.append(c)
            ctx.add_cookies(cks)
        ctx.add_init_script(STEALTH_JS)
        ctx.add_init_script(INIT_JS)
        return _run(ctx, url, o, emit, ms)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _run(ctx, url, o, emit, ms):
    coll = Collector(o)
    page = ctx.new_page()
    page.on("dialog", lambda d: d.dismiss())
    marks, ph = {}, {}
    inflight = coll.attach(page, ms)
    page.on("domcontentloaded", lambda _: marks.setdefault("dom_content_loaded_ms", ms()))
    page.on("load", lambda _: marks.setdefault("load_ms", ms()))
    fast_scroll = o.get("fast_scroll", True)
    ready_ms = 700 if fast_scroll else 1500

    t = ms()
    resp, err = None, None
    for attempt in (1, 2):
        try:
            resp = page.goto(url, wait_until="commit", timeout=int(o["max_wait"] * 1000))
            break
        except Exception as e:
            err = e
            emit({"type": "retry", "attempt": attempt, "error": str(e)[:160]})
            time.sleep(0.5)
    else:
        raise RuntimeError(f"navigation failed: {err}")
    marks["first_byte_ms"] = ph["navigate_ms"] = ms()
    status = resp.status if resp else None

    watch = Watch(ms, emit)
    t = ms()
    reason, _ = settle(page, watch, inflight, coll, o, emit, ms, int(o["max_wait"] * 1000))
    settled_ms = ms()
    ph["settle_ms"] = settled_ms - t

    t = ms()
    unlocked = []
    if o.get("auto_unlock", True):
        extra = o.get("unlock_text") or []
        deadline_total = int(o["max_wait"] * 1000)
        for _ in range(int(o.get("unlock_rounds", 3))):
            remaining = deadline_total - ms()
            if remaining < 300:
                break
            try:
                res = page.evaluate(AUTO_UNLOCK_JS, {"excluded": unlocked, "extra": extra})
            except Exception:
                break
            if not res:
                break
            unlocked.append(res["text"])
            emit({"type": "auto_unlock_click", "text": res["text"], "elapsed_ms": ms()})
            time.sleep(0.05)
            reason, _ = settle(page, watch, inflight, coll, o, emit, ms, ms() + min(remaining, 12000))
    ph["unlock_ms"] = ms() - t
    settled_ms = ms()

    t = ms()
    if o["scroll"]:
        emit({"type": "phase", "phase": "scrolling", "elapsed_ms": ms()})
        try:
            page.evaluate(SCROLL_JS, fast_scroll)
        except Exception:
            pass
        wait_net_idle(page, inflight, coll, ms, min(o.get("idle_ms", 350), 150), 2500)
    try:
        page.evaluate(READY_JS, ready_ms)
    except Exception:
        pass
    try:
        watch.observe(page.evaluate(STATE_JS))
    except Exception:
        pass
    coll.drain()
    ph["scroll_ms"] = ms() - t

    t = ms()
    final_url, html, title = page.url, "", ""
    try:
        html, title = page.content(), page.title()
    except Exception as e:
        raise RuntimeError(f"could not read page: {e}")
    origin = "{0.scheme}://{0.netloc}".format(urlparse(final_url))

    fetcher = AssetFetcher(ctx, coll, o, final_url) if o["assets"] else None
    if fetcher:       # network threads start NOW and run while the main thread does the rest
        try:
            fetcher.submit(page.evaluate(GATHER_JS), True)
        except Exception:
            pass
        fetcher.submit([origin + p for p in ("/robots.txt", "/sitemap.xml", "/favicon.ico")], False)

    shadow_html = None
    if o["shadow"]:
        try:
            shadow_html = page.evaluate(SHADOW_JS)
        except Exception:
            pass
    try:
        info = page.evaluate(META_JS)
    except Exception:
        info = {"meta": {"title": title}, "headings": {}, "links": [], "tech": [], "counts": {}}
    storage = {}
    try:
        storage = page.evaluate("() => ({local: Object.fromEntries(Object.entries(localStorage)),"
                                " session: Object.fromEntries(Object.entries(sessionStorage))})")
    except Exception:
        pass
    try:
        storage["cookies"] = ctx.cookies()
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
    ph["extract_ms"] = ms() - t

    t = ms()
    screenshot = None
    if o["screenshot"]:
        emit({"type": "phase", "phase": "screenshot", "elapsed_ms": ms()})
        for full in (True, False):
            try:
                screenshot = page.screenshot(type="jpeg", quality=70, full_page=full, timeout=20000)
                break
            except Exception:
                continue
    ph["screenshot_ms"] = ms() - t

    blocked = bool(BLOCK_RE.search(title or "")) or (status in (401, 403, 429, 503) and bool(BLOCK_RE.search(html[:20000])))
    main = {"url": final_url, "requested": url, "html": html, "title": title, "status": status,
            "links": info.get("links", []),
            "timings": {"total_ms": settled_ms, "loading_screens": watch.segments()}}
    pages = [main]

    t = ms()
    if o["crawl"] and o["max_pages"] > 1:
        seeds = []
        if fetcher:
            fetcher.collect(3)
            seeds = _sitemap_urls(coll, origin)
        emit({"type": "phase", "phase": "crawling", "elapsed_ms": ms(), "seeds": len(seeds)})
        pages = _crawl(ctx, coll, o, emit, ms, main, seeds, fetcher)
    ph["crawl_ms"] = ms() - t

    t = ms()
    sources = {}
    if fetcher:
        emit({"type": "phase", "phase": "collecting_assets", "elapsed_ms": ms(), "captured": len(coll.res)})
        fetcher.collect(8)
        coll.drain()
        if o["sourcemaps"]:
            sources = collect_sources(coll, fetcher, 6)
    ph["assets_ms"] = ms() - t

    segs = watch.segments()
    return {
        "html": html, "title": title, "status": status, "final_url": final_url,
        "resources": list(coll.res.values()), "skipped": coll.skipped, "sources": sources, "frames": frames,
        "finish_reason": reason, "pages": pages, "screenshot": screenshot, "shadow_html": shadow_html,
        "info": info, "storage": storage, "network": coll.log, "console": coll.console, "errors": coll.errors,
        "blocked": blocked,
        "timings": {**marks, "settled_ms": settled_ms, "total_ms": ms(), "phases": ph, "engine": "browser",
                    "loading_screen_detected": bool(segs),
                    "loading_screen_total_ms": sum(s["duration_ms"] for s in segs),
                    "loading_screens": segs, "navigation_timing": nav},
        "auto_unlock_clicks": unlocked,
    }


# ------------------------------------------------------------------ parallel asset fetching (threads, no browser)
class AssetFetcher:
    """Fetches DOM-referenced assets the browser never requested, in parallel (12-32 threads)."""

    def __init__(self, ctx, coll, o, final_url):
        self.ctx, self.coll, self.o, self.final_url = ctx, coll, o, final_url
        self.futs, self.seen, self.cookie_cache = {}, set(), {}
        self.ua = o.get("user_agent") or (UA_M if o["mobile"] else UA)

    def _headers(self, u):
        origin = "{0.scheme}://{0.netloc}".format(urlparse(u))
        if origin not in self.cookie_cache:
            try:
                self.cookie_cache[origin] = "; ".join(f"{c['name']}={c['value']}" for c in self.ctx.cookies(origin))
            except Exception:
                self.cookie_cache[origin] = ""
        h = {"User-Agent": self.ua, "Referer": self.final_url}
        if self.cookie_cache[origin]:
            h["Cookie"] = self.cookie_cache[origin]
        return h

    def submit(self, urls, log_err=True):
        for u in urls:
            u = (u or "").split("#")[0]
            if (not u.startswith("http") or u in self.coll.res or u in self.seen or len(self.seen) >= MAX_EXTRA):
                continue
            self.seen.add(u)
            self.futs[u] = (netutil.POOL.submit(netutil.fetch, u, self._headers(u), 6, MAX_BODY), log_err)

    def collect(self, budget_s):
        end = time.time() + budget_s
        for u, (f, log_err) in list(self.futs.items()):
            try:
                r = f.result(timeout=max(0.01, end - time.time()))
            except Exception:
                f.cancel()
                continue
            del self.futs[u]
            if r is None:
                continue
            if r["status"] != 200:
                if log_err and r["status"] >= 400:
                    self.coll.skipped.append({"url": u, "status": r["status"], "reason": "http_error"})
                continue
            body = r["body"]
            if body is None or self.coll.total[0] + len(body) > MAX_TOTAL:
                continue
            self.coll.store(u, 200, r["ctype"], body, "extra", None)


def parse_sourcemap(mu, raw, sources):
    smap = json.loads(raw)
    cont = smap.get("sourcesContent") or []
    for i, name in enumerate(smap.get("sources") or []):
        if i < len(cont) and cont[i] is not None:
            sources[source_path(mu, name)] = cont[i]


def collect_sources(coll, fetcher, budget_s):
    """Follow sourceMappingURL -> recover original TS/JSX/SCSS sources (sourcesContent). Maps load in parallel."""
    sources, urls, cands = {}, [], []
    for r in list(coll.res.values()):
        ct, path = r["content_type"], urlparse(r["url"]).path.lower()
        if not ("javascript" in ct or "css" in ct or re.search(r"\.(m?js|css)$", path)):
            continue
        m = SM_RE.search(r["body"][-600:].decode("utf-8", "ignore"))
        ref = m.group(1) if m else r.get("sourcemap")
        if ref:
            cands.append((r["url"], ref))
            if not ref.startswith("data:"):
                urls.append(urljoin(r["url"], ref).split("#")[0])
    if fetcher is not None and urls:
        fetcher.submit(urls, False)
        fetcher.collect(budget_s)
    for u, ref in cands:
        try:
            if ref.startswith("data:"):
                head, _, payload = ref.partition(",")
                raw = base64.b64decode(payload) if "base64" in head else unquote(payload).encode()
                mu = u + ".inline.map"
            else:
                mu = urljoin(u, ref).split("#")[0]
                if mu not in coll.res:
                    continue
                coll.res[mu]["resource_type"] = "sourcemap"
                raw = coll.res[mu]["body"]
            parse_sourcemap(mu, raw, sources)
        except Exception:
            continue
    return sources


def _sitemap_urls(coll, origin, limit=300):
    maps = []
    rb = coll.res.get(origin + "/robots.txt")
    if rb:
        maps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb["body"].decode("utf-8", "ignore"))
    maps.append(origin + "/sitemap.xml")
    out, seen, queue = [], set(), deque((urljoin(origin + "/", m), 0) for m in maps)
    while queue and len(out) < limit and len(seen) < 6:
        m, depth = queue.popleft()
        if m in seen:
            continue
        seen.add(m)
        try:
            r = coll.res.get(m)
            body = r["body"] if r else None
            if body is None:
                rr = netutil.fetch(m, {"Referer": origin}, 6, MAX_BODY)
                if not rr or rr["status"] != 200 or rr["body"] is None:
                    continue
                body = rr["body"]
            for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body.decode("utf-8", "ignore")):
                if loc.lower().split("?")[0].endswith(".xml") and depth < 1:
                    queue.append((loc, depth + 1))
                else:
                    out.append(loc)
        except Exception:
            continue
    return out[:limit]


# ------------------------------------------------------------------ parallel crawl (cooperative, one thread)
def _norm(u):
    p = urlparse(urldefrag(u)[0])
    path = re.sub(r"/index\.(?:html?|php)$", "/", p.path or "/", flags=re.I)
    return p._replace(netloc=p.netloc.lower(), path=path).geturl()


def _crawl(ctx, coll, o, emit, ms, first, seeds, fetcher):
    pages, q, slots = [first], deque(), []
    origin = urlparse(first["url"]).netloc.lower()
    seen = {_norm(first["url"])}
    conc = max(1, min(int(o.get("crawl_concurrency", 4)), 8))
    started = 0

    def enqueue(urls, depth):
        for u in urls:
            if len(q) + started >= o["max_pages"] * 3:
                break
            try:
                p = urlparse(u)
            except ValueError:
                continue
            if p.scheme not in ("http", "https") or p.netloc.lower() != origin or SKIP_EXT.search(p.path):
                continue
            n = _norm(u)
            if n in seen:
                continue
            if o["include"] and not re.search(o["include"], n):
                continue
            if o["exclude"] and re.search(o["exclude"], n):
                continue
            seen.add(n)
            q.append((urldefrag(u)[0], depth))

    def open_slot(u, depth, idx):
        page = None
        try:
            page = ctx.new_page()
            inflight = coll.attach(page, ms)
            watch = Watch(ms, emit, label=u)
            resp = page.goto(u, wait_until="commit", timeout=int(min(o["max_wait"], 25) * 1000))
            st = Settler(page, watch, inflight, coll, o, emit, ms, ms() + int(min(o["max_wait"], 20) * 1000),
                         label=u, click=False)
            return {"page": page, "watch": watch, "st": st, "url": u, "depth": depth, "idx": idx, "t0": ms(),
                    "status": resp.status if resp else None, "phase": "settle", "inflight": inflight,
                    "idle_since": None, "scroll_t0": 0}
        except Exception as e:
            emit({"type": "page_error", "url": u, "error": str(e)[:200]})
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            return None

    def finish(s):
        page = s["page"]
        try:
            try:
                page.evaluate(READY_JS, 700)
            except Exception:
                pass
            coll.drain()
            html, title, final = page.content(), page.title(), page.url
            try:
                links = page.evaluate(META_JS).get("links", [])
            except Exception:
                links = []
            if fetcher:
                try:
                    fetcher.submit(page.evaluate(GATHER_JS), True)
                except Exception:
                    pass
            segs = s["watch"].segments()
            took = ms() - s["t0"]
            pg = {"url": final, "requested": s["url"], "html": html, "title": title, "status": s["status"],
                  "links": links, "idx": s["idx"],
                  "timings": {"total_ms": took, "loading_screen_total_ms": sum(x["duration_ms"] for x in segs),
                              "loading_screens": segs}}
            emit({"type": "page", "url": final, "status": s["status"], "title": title, "total_ms": took,
                  "loader_ms": pg["timings"]["loading_screen_total_ms"]})
            return pg
        except Exception as e:
            emit({"type": "page_error", "url": s["url"], "error": str(e)[:200]})
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    enqueue([l["href"] for l in first["links"]], 1)
    enqueue(seeds, 1)
    end = time.time() + o["crawl_budget"]
    while time.time() < end and (q or slots):
        while q and len(slots) < conc and started < o["max_pages"] - 1:
            u, depth = q.popleft()
            started += 1
            s = open_slot(u, depth, started)
            if s:
                slots.append(s)
        if not slots:
            if not q or started >= o["max_pages"] - 1:
                break
            continue
        coll.drain()
        for s in list(slots):
            if s["phase"] == "settle":
                if not s["st"].step():
                    continue
                if o["scroll"]:
                    try:
                        s["page"].evaluate(SCROLL_START_JS, o.get("fast_scroll", True))
                        s["phase"], s["scroll_t0"] = "scroll", ms()
                        continue
                    except Exception:
                        pass
                s["phase"] = "final"
            elif s["phase"] == "scroll":
                now = ms()
                try:
                    done = s["page"].evaluate("() => window.__u2sScroll === 1")
                except Exception:
                    done = True
                if busy(s["inflight"], now) == 0:
                    s["idle_since"] = s["idle_since"] if s["idle_since"] is not None else now
                else:
                    s["idle_since"] = None
                idle = s["idle_since"] is not None and now - s["idle_since"] >= 150
                if not ((done and idle) or now - s["scroll_t0"] > 5000):
                    continue
                s["phase"] = "final"
            if s["phase"] == "final":
                slots.remove(s)
                pg = finish(s)
                if pg:
                    pages.append(pg)
                    if s["depth"] < o["depth"]:
                        enqueue([l["href"] for l in pg["links"]], s["depth"] + 1)
        if slots:
            slots[0]["page"].wait_for_timeout(20)
    for s in slots:
        try:
            s["page"].close()
        except Exception:
            pass
    pages.sort(key=lambda p: p.get("idx", 0))
    return pages
