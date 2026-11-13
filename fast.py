"""HTTP engine: no browser. Static sites are mirrored in milliseconds (parallel assets, parallel crawl)."""
import os
import re
import time
from html import unescape
from urllib.parse import urldefrag, urljoin, urlparse

import netutil
from capture import (BLOCK_RE, MAX_BODY, MAX_TOTAL, SKIP_EXT, UA, UA_M, _norm, _sitemap_urls,
                     collect_sources)
from pack import _CSSURL, _IMPORT, _decode

MAX_ASSETS = int(os.environ.get("MAX_ASSETS", "1500"))


class NeedBrowser(Exception):
    """This page needs a real browser (JS-rendered, loading screen, blocked...)."""


TAG = re.compile(r"<(img|script|source|video|audio|track|embed|input|link)\b([^>]*)>", re.I)
ATTR = re.compile(r"""\b([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
A_HREF = re.compile(r"""<a\b[^>]*?\shref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
LINK_SKIP = re.compile(r"(?:^|\s)(?:canonical|alternate|dns-prefetch|preconnect|prev|next|author|search|me)(?:\s|$)", re.I)
SPA_ROOT = re.compile(r"""<(?:div|main)[^>]*\sid=["'](?:root|app|__next|__nuxt|svelte)["'][^>]*>\s*</(?:div|main)>"""
                      r"""|<app-root[^>]*>\s*</app-root>""", re.I)
LOADER_MARK = re.compile(r"""(?:id|class)\s*=\s*["'][^"']*(?:preloader|page-loader|loader|loading-screen|splash|spinner)[^"']*["']""", re.I)


def _abs(base, v):
    v = unescape((v or "").strip())
    if not v or v.startswith(("data:", "javascript:", "#", "mailto:", "tel:", "blob:")):
        return None
    try:
        u = urljoin(base, v)
    except ValueError:
        return None
    return u.split("#")[0] if u.startswith("http") else None


def css_urls(text, base):
    out = []
    for m in _IMPORT.finditer(text):
        out.append(_abs(base, m.group(1) or m.group(2)))
    for m in _CSSURL.finditer(text):
        out.append(_abs(base, m.group(2)))
    return [u for u in out if u]


def extract_assets(html, base):
    out = []
    for m in TAG.finditer(html):
        tag, attrs = m.group(1).lower(), {}
        for am in ATTR.finditer(m.group(2)):
            attrs[am.group(1).lower()] = am.group(2) or am.group(3) or am.group(4) or ""
        if tag == "link":
            if not LINK_SKIP.search(attrs.get("rel", "")):
                out.append(_abs(base, attrs.get("href")))
            continue
        for k in ("src", "poster", "data-src", "data-poster"):
            out.append(_abs(base, attrs.get(k)))
        for k in ("srcset", "data-srcset"):
            for part in (attrs.get(k) or "").split(","):
                part = part.strip()
                if part:
                    out.append(_abs(base, part.split()[0]))
    for css in re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.I | re.S):
        out += css_urls(css, base)
    for m in re.finditer(r"""\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", html, re.I):
        out += css_urls(m.group(1) or m.group(2) or "", base)
    return [u for u in out if u]


def extract_links(html, base):
    links = []
    for m in A_HREF.finditer(html):
        u = _abs(base, m.group(1) or m.group(2) or m.group(3))
        if u:
            links.append({"href": u, "text": "", "rel": ""})
    return links


def _rtype(ctype, url):
    p = urlparse(url).path.lower()
    if "css" in ctype or p.endswith(".css"):
        return "stylesheet"
    if "javascript" in ctype or p.endswith((".js", ".mjs")):
        return "script"
    if ctype.startswith("image/"):
        return "image"
    if "font" in ctype or p.endswith((".woff", ".woff2", ".ttf", ".otf")):
        return "font"
    if ctype.startswith(("audio/", "video/")):
        return "media"
    return "other"


def _dec(r):
    body = r["body"] or b""
    m = re.search(r"charset=([\w-]+)", r["headers"].get("Content-Type", "") or "", re.I) \
        or re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", body[:4096], re.I)
    enc = "utf-8"
    if m:
        enc = m.group(1) if isinstance(m.group(1), str) else m.group(1).decode("ascii", "ignore")
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def looks_dynamic(html, status):
    """Why this page needs a real browser (None = static HTML is enough)."""
    if status is None or status in (401, 403, 429, 503):
        return f"status {status}"
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if t and BLOCK_RE.search(t.group(1)):
        return "bot-protection page"
    if SPA_ROOT.search(html):
        return "empty app root (client-side rendered)"
    if LOADER_MARK.search(html):
        return "loading-screen markup (a browser is needed to measure it)"
    text = re.sub(r"<(script|style|noscript)\b.*?</\1>", "", html, flags=re.I | re.S)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    if len(text) < 250:
        return "very little server-rendered text"
    if re.search(r"enable javascript|requires javascript", text, re.I):
        return "page asks for JavaScript"
    return None


class Mirror:
    """Parallel fetcher with the same submit()/collect()/res interface as capture.AssetFetcher."""

    def __init__(self, hdr, ms):
        self.hdr, self.ms = hdr, ms
        self.res, self.skipped, self.tried, self.total, self._pending = {}, [], set(), 0, []

    def submit(self, urls, log_err=True):
        self._pending.extend(urls)

    def collect(self, budget_s):
        urls, self._pending = self._pending, []
        self.fetch(urls, budget_s, False)

    def fetch(self, urls, budget=10, log_err=True):
        todo = [u for u in dict.fromkeys((x or "").split("#")[0] for x in urls)
                if u.startswith("http") and u not in self.tried]
        todo = todo[:max(0, MAX_ASSETS - len(self.tried))]
        self.tried.update(todo)
        futs = {u: netutil.POOL.submit(netutil.fetch, u, self.hdr, 6, MAX_BODY) for u in todo}
        end, new = time.time() + budget, []
        for u, f in futs.items():
            try:
                r = f.result(timeout=max(0.01, end - time.time()))
            except Exception:
                f.cancel()
                continue
            if r is None:
                continue
            if r["status"] != 200:
                if log_err and r["status"] >= 400:
                    self.skipped.append({"url": u, "status": r["status"], "reason": "http_error"})
                continue
            body = r["body"]
            if body is None or self.total + len(body) > MAX_TOTAL:
                continue
            self.total += len(body)
            rec = {"url": u, "status": 200, "content_type": r["ctype"], "body": body,
                   "resource_type": _rtype(r["ctype"], u), "t_ms": self.ms(),
                   "sourcemap": r["headers"].get("SourceMap") or r["headers"].get("X-SourceMap")}
            self.res[u] = rec
            new.append(rec)
        return new

    def assets(self, urls, depth=3):
        wave = urls
        for _ in range(depth):
            nxt = []
            for r in self.fetch(wave):
                if r["resource_type"] == "stylesheet":
                    nxt += css_urls(_decode(r["body"]), r["url"])
            if not nxt:
                break
            wave = nxt


def _info(html, final, links):
    def meta(n):
        for pat in (r"""<meta[^>]+(?:name|property)=["']%s["'][^>]*content=["']([^"']*)""",
                    r"""<meta[^>]+content=["']([^"']*)["'][^>]*(?:name|property)=["']%s["']"""):
            m = re.search(pat % re.escape(n), html, re.I)
            if m:
                return unescape(m.group(1))
        return None

    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = unescape(re.sub(r"\s+", " ", t.group(1)).strip()) if t else ""
    heads = {f"h{n}": [] for n in (1, 2, 3)}
    for n, txt in re.findall(r"<h([1-3])[^>]*>(.*?)</h\1>", html, re.I | re.S)[:90]:
        heads[f"h{n}"].append(unescape(re.sub(r"<[^>]+>|\s+", " ", txt)).strip()[:120])
    low, tech = html.lower(), []
    for needle, name in (("wp-content", "WordPress"), ("__next_data__", "Next.js"), ("__nuxt", "Nuxt"),
                         ("ng-version", "Angular"), ("data-reactroot", "React"), ("cdn.shopify", "Shopify"),
                         ("wixstatic", "Wix"), ("data-wf-page", "Webflow"), ("jquery", "jQuery"),
                         ("bootstrap", "Bootstrap"), ("___gatsby", "Gatsby"), ("alpinejs", "Alpine.js")):
        if needle in low:
            tech.append(name)
    lang = re.search(r"<html[^>]*\slang=[\"']?([\w-]+)", html, re.I)
    return {"meta": {"title": title, "description": meta("description"), "keywords": meta("keywords"),
                     "generator": meta("generator"), "lang": lang.group(1) if lang else None,
                     "og": {"title": meta("og:title"), "image": meta("og:image")}},
            "headings": heads, "links": links, "tech": tech,
            "counts": {"scripts": low.count("<script"), "images": low.count("<img"), "forms": low.count("<form"),
                       "iframes": low.count("<iframe")}}


def run_http(url, o, emit, auto=False):
    t0 = time.perf_counter()
    ms = lambda: int((time.perf_counter() - t0) * 1000)
    ph = {}
    if auto and (o.get("screenshot") or o.get("click") or o.get("wait_selector")):
        raise NeedBrowser("options need a real browser (screenshot / click / wait_selector)")
    ua = o.get("user_agent") or (UA_M if o.get("mobile") else UA)
    hdr = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
    hdr.update(o.get("headers") or {})
    if o.get("cookies"):
        hdr["Cookie"] = "; ".join(f"{c.get('name')}={c.get('value')}" for c in o["cookies"] if c.get("name"))
    emit({"type": "phase", "phase": "http_fetch", "elapsed_ms": ms()})

    r = netutil.fetch(url, hdr, timeout=min(o["max_wait"], 20), max_bytes=MAX_BODY)
    if r is None:
        raise RuntimeError("http fetch failed (blocked address, DNS, TLS or timeout)")
    if r["body"] is None:
        raise RuntimeError("page is larger than MAX_BODY_MB")
    ph["fetch_ms"] = ms()
    html, final, status = _dec(r), r["url"].split("#")[0], r["status"]
    why = looks_dynamic(html, status)
    if auto and why:
        raise NeedBrowser(why)

    origin = "{0.scheme}://{0.netloc}".format(urlparse(final))
    ahdr = {"User-Agent": ua, "Referer": final}
    if hdr.get("Cookie"):
        ahdr["Cookie"] = hdr["Cookie"]
    mirror = Mirror(ahdr, ms)
    links = extract_links(html, final)
    title = _info(html, final, links)["meta"]["title"]
    main = {"url": final, "requested": url, "html": html, "title": title, "status": status, "links": links,
            "idx": 0, "timings": {"total_ms": ph["fetch_ms"], "loading_screens": [], "loading_screen_total_ms": 0}}
    pages = [main]

    t = ms()
    if o["assets"]:
        mirror.fetch([origin + p for p in ("/robots.txt", "/sitemap.xml", "/favicon.ico")], 4, False)
    if o["crawl"] and o["max_pages"] > 1:
        emit({"type": "phase", "phase": "crawling", "elapsed_ms": ms()})
        pages = _crawl(mirror, hdr, main, o, emit, ms, _sitemap_urls(mirror, origin) if o["assets"] else [])
    ph["crawl_ms"] = ms() - t

    t = ms()
    sources = {}
    if o["assets"]:
        emit({"type": "phase", "phase": "collecting_assets", "elapsed_ms": ms()})
        urls = []
        for p in pages:
            urls += extract_assets(p["html"], p["url"])
        mirror.assets(urls)
        if o["sourcemaps"]:
            sources = collect_sources(mirror, mirror, 6)
    ph["assets_ms"] = ms() - t

    info = _info(html, final, links)
    blocked = bool(BLOCK_RE.search(title or "")) or (status in (401, 403, 429, 503) and bool(BLOCK_RE.search(html[:20000])))
    return {
        "html": html, "title": title, "status": status, "final_url": final, "resources": list(mirror.res.values()),
        "skipped": mirror.skipped, "sources": sources, "frames": [], "finish_reason": "http", "pages": pages,
        "screenshot": None, "shadow_html": None, "info": info, "storage": {}, "network": [], "console": [],
        "errors": [], "blocked": blocked,
        "timings": {"first_byte_ms": ph["fetch_ms"], "settled_ms": ph["fetch_ms"], "total_ms": ms(), "phases": ph,
                    "engine": "http", "dynamic_hint": why, "loading_screen_detected": False,
                    "loading_screen_measured": False, "loading_screen_total_ms": 0, "loading_screens": [],
                    "navigation_timing": None}}


def _crawl(mirror, hdr, first, o, emit, ms, seeds):
    from collections import deque
    pages, origin = [first], urlparse(first["url"]).netloc.lower()
    seen = {_norm(first["url"])}
    end = time.time() + o["crawl_budget"]

    def pick(urls):
        out = []
        for u in urls:
            p = urlparse(u)
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
            out.append(urldefrag(u)[0])
        return out

    frontier = pick([l["href"] for l in first["links"]] + seeds)
    depth = 1
    while frontier and len(pages) < o["max_pages"] and time.time() < end and depth <= o["depth"]:
        batch = frontier[:o["max_pages"] - len(pages)]
        t0 = ms()
        futs = [netutil.POOL.submit(netutil.fetch, u, hdr, 10, MAX_BODY) for u in batch]
        nxt = []
        for u, f in zip(batch, futs):
            try:
                r = f.result(timeout=max(0.01, end - time.time()))
            except Exception:
                continue
            if not r or r["status"] != 200 or r["body"] is None or "html" not in (r["ctype"] or "html"):
                continue
            html, final = _dec(r), r["url"].split("#")[0]
            links = extract_links(html, final)
            tt = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = unescape(re.sub(r"\s+", " ", tt.group(1)).strip()) if tt else ""
            took = ms() - t0
            pages.append({"url": final, "requested": u, "html": html, "title": title, "status": 200,
                          "links": links, "idx": len(pages),
                          "timings": {"total_ms": took, "loading_screens": [], "loading_screen_total_ms": 0}})
            emit({"type": "page", "url": final, "status": 200, "title": title, "total_ms": took, "loader_ms": 0})
            nxt += [l["href"] for l in links]
        frontier = pick(nxt)
        depth += 1
    return pages
