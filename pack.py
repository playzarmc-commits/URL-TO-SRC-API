"""Pure helpers (no browser): path mapping, URL rewriting, single-file snapshot, zip building."""
import base64
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import zipfile
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

_SAFE = re.compile(r"[^A-Za-z0-9._~@+=,-]")


def _safe(s):
    return _SAFE.sub("_", s)[:100] or "_"


class PathMap:
    """url -> unique local path inside the zip (<prefix>/<host>/<path>)."""

    def __init__(self):
        self.used, self.by_url = {}, {}

    def get(self, url, prefix="assets", host=True):
        if url in self.by_url:
            return self.by_url[url]
        u = urlparse(url)
        path = unquote(u.path) or "/"
        if path.endswith("/"):
            path += "index"
        parts = [_safe(p) for p in path.split("/") if p not in ("", ".", "..")] or ["index"]
        name = parts[-1]
        if u.query:
            stem, ext = os.path.splitext(name)
            name = f"{stem}__{hashlib.sha1(u.query.encode()).hexdigest()[:8]}{ext}"
        parts[-1] = name
        lp = "/".join([prefix] + ([_safe(u.netloc)] if host else []) + parts)
        base, n = lp, 1
        while lp in self.used and self.used[lp] != url:
            stem, ext = os.path.splitext(base)
            lp = f"{stem}_{n}{ext}"
            n += 1
        self.used[lp] = url
        self.by_url[url] = lp
        return lp


def source_path(map_url, src):
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", src)
    parts = [_safe(p) for p in re.split(r"[\\/]+", s) if p not in ("", ".", "..")]
    return "sources/" + _safe(urlparse(map_url).netloc) + "/" + "/".join(parts or ["unknown"])


def _forms(url, base_url, rel):
    """All textual ways `url` can appear inside a file that lives at base_url."""
    u, b = urlparse(url), urlparse(base_url)
    q = ("?" + u.query) if u.query else ""
    path = u.path or "/"
    f = {url: rel, "//" + u.netloc + path + q: rel}
    if u.netloc == b.netloc:
        f[path + q] = rel
        r = posixpath.relpath(path, posixpath.dirname(b.path or "/") or "/") + q
        f[r] = rel
        f["./" + r] = rel
    return f


_TOK = re.compile(r"""(["'(,=\s])([^"'()\s,<>]+)""")


def _lookup(forms, tok):
    r = forms.get(tok)
    if r is None and "&amp;" in tok:
        r = forms.get(tok.replace("&amp;", "&"))
    if r is None and "#" in tok:
        base, frag = tok.split("#", 1)
        r0 = forms.get(base) or (forms.get(base.replace("&amp;", "&")) if "&amp;" in base else None)
        if base and r0 is not None:
            r = r0 + "#" + frag
    return r


def rewrite(text, base_url, file_lp, items):
    """Single-pass rewrite of URLs in HTML/CSS to relative zip paths (anchors keep #fragment)."""
    fdir = posixpath.dirname(file_lp) or "."
    forms = {}
    for url, lp in items:
        forms.update(_forms(url, base_url, posixpath.relpath(lp, fdir)))

    def repl(m):
        r = _lookup(forms, m.group(2))
        return m.group(1) + r if r is not None else m.group(0)

    return _TOK.sub(repl, text)


def _is_css(r):
    return "css" in r["content_type"] or urlparse(r["url"]).path.lower().endswith(".css")


def _decode(body):
    for enc in ("utf-8", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            pass
    return body.decode("utf-8", "replace")


def _clean(html):
    """Remove things that break offline use; force utf-8."""
    h = re.sub(r"<base\b[^>]*>", "", html, flags=re.I)
    h = re.sub(r"""<meta[^>]+http-equiv=["']?content-security-policy[^>]*>""", "", h, flags=re.I)
    h = re.sub(r"""<meta[^>]+charset=[^>]*>""", "", h, flags=re.I)
    h = re.sub(r"""\s(?:integrity|nonce)=("[^"]*"|'[^']*')""", "", h, flags=re.I)
    if re.search(r"<head\b[^>]*>", h, flags=re.I):
        h = re.sub(r"(<head\b[^>]*>)", r'\1<meta charset="utf-8">', h, count=1, flags=re.I)
    else:
        h = '<meta charset="utf-8">' + h
    return h


# ------------------------------------------------------------------ single-file snapshot
_IMPORT = re.compile(r"""@import\s+(?:url\(\s*["']?([^)"']+)["']?\s*\)|["']([^"']+)["'])[^;]*;""", re.I)
_CSSURL = re.compile(r"""url\(\s*(["']?)([^)"']+)\1\s*\)""", re.I)


def _data_uri(r):
    ct = r["content_type"] or mimetypes.guess_type(r["url"].split("?")[0])[0] or "application/octet-stream"
    return f"data:{ct};base64," + base64.b64encode(r["body"]).decode()


def make_snapshot(html, page_url, res, max_res=2 * 1024 * 1024, max_total=40 * 1024 * 1024):
    """One self-contained HTML file: CSS inlined, images/fonts as data URIs, scripts removed."""
    budget = [max_total]

    def uri_for(abs_url):
        r = res.get(abs_url.split("#")[0])
        if not r or len(r["body"]) > max_res or budget[0] <= 0:
            return None
        d = _data_uri(r)
        budget[0] -= len(d)
        return d

    def css_inline(text, css_url, depth=0):
        def imp(m):
            target = urljoin(css_url, (m.group(1) or m.group(2)).strip())
            r = res.get(target)
            if r and depth < 3 and _is_css(r):
                return css_inline(_decode(r["body"]), target, depth + 1)
            return m.group(0)

        text = _IMPORT.sub(imp, text)

        def u(m):
            ref = m.group(2).strip()
            if ref.startswith(("data:", "#")):
                return m.group(0)
            ab = urljoin(css_url, ref)
            return 'url("%s")' % (uri_for(ab) or ab)

        return _CSSURL.sub(u, text)

    h = re.sub(r"<script\b.*?</script>", "", html, flags=re.I | re.S)
    h = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)",
               lambda m: m.group(1) + css_inline(m.group(2), page_url) + m.group(3), h, flags=re.I | re.S)

    def link(m):
        tag = m.group(0)
        if not re.search(r"""rel\s*=\s*["']?[^"'>]*stylesheet""", tag, re.I):
            return tag
        hm = re.search(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag, re.I)
        if not hm:
            return tag
        ab = urljoin(page_url, unescape(hm.group(1) or hm.group(2) or hm.group(3)))
        r = res.get(ab.split("#")[0])
        if r:
            return "<style>" + css_inline(_decode(r["body"]), r["url"]) + "</style>"
        return tag

    h = re.sub(r"<link\b[^>]*>", link, h, flags=re.I)

    forms = {}
    for url in res:
        forms.update(_forms(url, page_url, url))

    def repl(m):
        ab = _lookup(forms, m.group(2))
        if ab is None:
            return m.group(0)
        base, _, frag = ab.partition("#")
        r = res.get(base)
        if r and (r["content_type"].startswith(("image/", "audio/", "video/", "font/")) or "font" in r["content_type"]):
            d = uri_for(base)
            if d:
                return m.group(1) + d
        return m.group(1) + ab

    h = _TOK.sub(repl, h)
    base_tag = f'<base href="{page_url}">'
    if re.search(r"<head\b[^>]*>", h, flags=re.I):
        h = re.sub(r"(<head\b[^>]*>)", lambda m: m.group(1) + base_tag, h, count=1, flags=re.I)
    else:
        h = base_tag + h
    return h


# ------------------------------------------------------------------ zip
def _page_lp(pm, url):
    lp = pm.get(url, prefix="pages", host=False)
    return lp if lp.lower().endswith((".html", ".htm")) else lp + ".html"


def build_zip(path, pages, resources, sources, frames, report, extras, snapshot=True):
    main = pages[0]
    main_url = main["url"].split("#")[0]
    ppm, apm = PathMap(), PathMap()
    page_urls = {}
    for i, p in enumerate(pages):
        u = p["url"].split("#")[0]
        p["_lp"] = "index.html" if i == 0 else _page_lp(ppm, u)
        page_urls[u] = p["_lp"]
        req = (p.get("requested") or "").split("#")[0]
        if req and req not in page_urls:
            page_urls[req] = p["_lp"]
    res = [r for r in resources if r["url"] not in page_urls]
    items = [(r["url"], apm.get(r["url"])) for r in res]
    page_items = list(page_urls.items())
    for u, lp in list(page_urls.items()):
        if u.endswith("/index.html"):
            page_items.append((u[:-10], lp))
    items += page_items

    manifest, total = [], 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("original.html", main["html"].replace("<head>", f'<head><base href="{main_url}">', 1)
                   if "<head>" in main["html"] else main["html"])
        for p in pages:
            z.writestr(p["_lp"], rewrite(_clean(p["html"]), p["url"], p["_lp"], items))
            manifest.append({"url": p["url"], "path": p["_lp"], "resource_type": "page",
                             "title": p.get("title"), "status": p.get("status")})
        for r, (_, lp) in zip(res, items):
            body = r["body"]
            if _is_css(r):
                body = rewrite(_decode(body), r["url"], lp, items).encode("utf-8")
            z.writestr(lp, body)
            total += len(body)
            manifest.append({"url": r["url"], "path": lp, "status": r["status"],
                             "content_type": r["content_type"], "size": len(r["body"]),
                             "resource_type": r.get("resource_type"), "t_ms": r.get("t_ms")})
        snap = False
        if snapshot:
            try:
                z.writestr("snapshot.html", make_snapshot(_clean(main["html"]), main_url,
                                                          {r["url"]: r for r in resources}))
                snap = True
            except Exception as e:  # never fail the whole job because of the snapshot
                report["snapshot_error"] = str(e)
        for sp, content in sources.items():
            z.writestr(sp, content)
        for i, (furl, fhtml) in enumerate(frames):
            z.writestr(f"frames/{i:02d}.html", fhtml)
            manifest.append({"url": furl, "path": f"frames/{i:02d}.html", "resource_type": "frame"})
        for name, data in extras.items():
            if data is None:
                continue
            if not isinstance(data, (bytes, str)):
                data = json.dumps(data, indent=1, default=str)
            z.writestr(name, data)
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        z.writestr("report.json", json.dumps(report, indent=1, default=str))
    return {"files": len(manifest) + len(sources) + len(extras) + 3, "bytes": total,
            "pages": len(pages), "snapshot": snap, "sources_from_maps": len(sources)}
