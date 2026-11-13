"""Shared network helpers: private-address guard + SSRF-safe parallel HTTP fetching (no browser)."""
import functools
import gzip
import io
import ipaddress
import os
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "0") == "1"
PROXY_URL = os.environ.get("PROXY_URL", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("FETCH_THREADS", "32")), thread_name_prefix="fetch")


@functools.lru_cache(maxsize=4096)
def _private(host):
    try:
        for _, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0].split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
    except Exception:
        return False
    return False


def is_private(host):
    return False if ALLOW_PRIVATE else _private(host or "")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        h = urlparse(newurl).hostname
        if not h or is_private(h):
            raise urllib.error.URLError("redirect to a private address was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener():
    handlers = [_Redirect()]
    if PROXY_URL:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
    return urllib.request.build_opener(*handlers)


_OPENER = _build_opener()


def _gunzip(body, limit):
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as g:
        out = g.read(limit + 1)
    return out


def fetch(url, headers=None, timeout=6, max_bytes=25 * 1024 * 1024):
    """-> dict(url, status, ctype, body, headers) | None.  body=None means 'too large'."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or is_private(p.hostname):
        return None
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    try:
        with _OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            body = r.read(max_bytes + 1)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip" and len(body) <= max_bytes:
                body = _gunzip(body, max_bytes)
            if len(body) > max_bytes:
                body = None
            return {"url": r.geturl(), "status": r.status, "body": body, "headers": dict(r.headers.items()),
                    "ctype": (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "body": b"", "ctype": "", "headers": dict(e.headers.items() if e.headers else [])}
    except Exception:
        return None
