"""Pure helpers (no browser): local path mapping, URL rewriting, zip building."""
import hashlib
import json
import os
import posixpath
import re
import zipfile
from urllib.parse import unquote, urlparse

_SAFE = re.compile(r"[^A-Za-z0-9._~@+=,-]")


def _safe(s):
    return _SAFE.sub("_", s)[:100] or "_"


class PathMap:
    """url -> unique local path inside the zip (assets/<host>/<path>)."""

    def __init__(self):
        self.used, self.by_url = {}, {}

    def get(self, url, prefix="assets"):
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
        lp = "/".join([prefix, _safe(u.netloc)] + parts)
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


def rewrite(text, base_url, file_lp, items):
    """Single-pass rewrite of URLs in HTML/CSS to relative zip paths."""
    fdir = posixpath.dirname(file_lp) or "."
    forms = {}
    for url, lp in items:
        forms.update(_forms(url, base_url, posixpath.relpath(lp, fdir)))

    def repl(m):
        tok = m.group(2)
        r = forms.get(tok)
        if r is None and "&amp;" in tok:
            r = forms.get(tok.replace("&amp;", "&"))
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


def build_zip(path, final_url, html, resources, sources, frames, report):
    pm = PathMap()
    main = final_url.split("#")[0]
    res = [r for r in resources if r["url"] != main]
    items = [(r["url"], pm.get(r["url"])) for r in res]

    clean = re.sub(r"<base\b[^>]*>", "", html, flags=re.I)
    clean = re.sub(r"""<meta[^>]+http-equiv=["']?content-security-policy[^>]*>""", "", clean, flags=re.I)
    clean = re.sub(r"""\s(?:integrity|nonce)=("[^"]*"|'[^']*')""", "", clean, flags=re.I)
    index = rewrite(clean, final_url, "index.html", items)
    original = html.replace("<head>", f'<head><base href="{final_url}">', 1) if "<head>" in html else html

    manifest, total = [], 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("index.html", index)
        z.writestr("original.html", original)
        for r, (_, lp) in zip(res, items):
            body = r["body"]
            if _is_css(r):
                body = rewrite(_decode(body), r["url"], lp, items).encode("utf-8")
            z.writestr(lp, body)
            total += len(body)
            manifest.append({"url": r["url"], "path": lp, "status": r["status"],
                             "content_type": r["content_type"], "size": len(r["body"]),
                             "resource_type": r.get("resource_type"), "t_ms": r.get("t_ms")})
        for sp, content in sources.items():
            z.writestr(sp, content)
        for i, (furl, fhtml) in enumerate(frames):
            z.writestr(f"frames/{i:02d}.html", fhtml)
            manifest.append({"url": furl, "path": f"frames/{i:02d}.html", "resource_type": "frame"})
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
        z.writestr("report.json", json.dumps(report, indent=1, default=str))
    return {"files": len(manifest) + len(sources) + 3, "bytes": total, "sources_from_maps": len(sources)}
