import hashlib
import ipaddress
import json
import mimetypes
import os
import queue
import re
import socket
import threading
import time
import urllib.request
import uuid
import zipfile
from collections import defaultdict, deque
from urllib.parse import urlparse

from flask import Flask, Response, g, jsonify, request, send_file, stream_with_context

from admin import register_admin
from capture import capture
from fast import NeedBrowser, run_http
from keystore import KeyStore
from pack import build_zip

# ---------------------------------------------------------------- config
WORKERS = int(os.environ.get("WORKERS", "1"))               # parallel browsers
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "100"))
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "60"))     # default per key
PER_BOT_ACTIVE = int(os.environ.get("PER_BOT_ACTIVE", "10"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))           # crawl cap

SPEED_MODES = {
    "full":     {"settle_ms": 1200, "idle_ms": 800, "min_ms": 300, "fast_scroll": False, "crawl_concurrency": 1},
    "balanced": {"settle_ms": 450,  "idle_ms": 350, "min_ms": 200, "fast_scroll": True,  "crawl_concurrency": 4},
    "turbo":    {"settle_ms": 200,  "idle_ms": 150, "min_ms": 100, "fast_scroll": True,  "crawl_concurrency": 8},
}
JOB_TTL = int(os.environ.get("JOB_TTL", "1800"))
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/url2src")
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "0") == "1"
PROXY_URL = os.environ.get("PROXY_URL", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "asraful123")        # override with the ADMIN_KEY env var
SIGNING_SECRET = os.environ.get("SIGNING_SECRET") or ADMIN_KEY
os.makedirs(DATA_DIR, exist_ok=True)

KEYS = {}  # static keys from env: API_KEYS="botA:key1,botB:key2" or "key1,key2"
for i, part in enumerate(p.strip() for p in os.environ.get("API_KEYS", "").split(",") if p.strip()):
    name, key = part.split(":", 1) if ":" in part else (f"bot{i + 1}", part)
    KEYS[name] = key

STORE = KeyStore(os.environ.get("KEYS_FILE", os.path.join(DATA_DIR, "keys.json")), os.environ.get("DATABASE_URL"),
                 RATE_PER_MIN, PER_BOT_ACTIVE, SIGNING_SECRET,
                 [x for x in os.environ.get("REVOKED_IDS", "").split(",") if x.strip()])
STORE.set_static(KEYS)

ACTIVE = ("queued", "running")
app = Flask(__name__)
QUEUE = queue.Queue()
JOBS, RATE, CACHE = {}, defaultdict(deque), {}


class Job:
    def __init__(self, bot, opts, fp):
        self.id, self.bot, self.opts, self.fp = uuid.uuid4().hex[:16], bot, opts, fp
        self.status, self.events, self.cond = "queued", [], threading.Condition()
        self.result, self.error, self.created, self.finished = None, None, time.time(), 0
        self.zip_path = os.path.join(DATA_DIR, self.id + ".zip")
        self.html_path = os.path.join(DATA_DIR, self.id + ".html")

    def emit(self, ev):
        with self.cond:
            ev["seq"] = len(self.events)
            self.events.append(ev)
            self.cond.notify_all()

    def set_status(self, s):
        with self.cond:
            self.status = s
            if s not in ACTIVE:
                self.finished = time.time()
            self.cond.notify_all()


# ---------------------------------------------------------------- job execution
def make_extras(r):
    info, links = r["info"], r["info"].get("links", [])
    host = urlparse(r["final_url"]).netloc.lower()
    http = [l["href"].split("#")[0] for l in links if l["href"].startswith("http")]
    return {
        "screenshot.jpg": r["screenshot"],
        "rendered_with_shadow.html": r["shadow_html"],
        "meta.json": {"url": r["final_url"], "title": r["title"], "status": r["status"], "blocked": r["blocked"],
                      "auto_unlock_clicks": r.get("auto_unlock_clicks", []),
                      **{k: info.get(k) for k in ("meta", "headings", "tech", "counts")}},
        "links.json": {"internal": sorted({h for h in http if urlparse(h).netloc.lower() == host}),
                       "external": sorted({h for h in http if urlparse(h).netloc.lower() != host}),
                       "total": len(links)},
        "pages.json": [{"url": p["url"], "title": p["title"], "status": p["status"], "timings": p["timings"]}
                       for p in r["pages"]],
        "network.json": r["network"],
        "console.json": {"console": r["console"], "errors": r["errors"]},
        "storage.json": r["storage"],
    }


def fire_webhook(job):
    url = job.opts.get("webhook")
    if not url:
        return
    payload = json.dumps({"job_id": job.id, "status": job.status, "error": job.error, "result": job.result},
                         default=str).encode()

    def send():
        for i in range(3):
            try:
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json",
                                                                         "User-Agent": "url2src"})
                urllib.request.urlopen(req, timeout=10).read()
                return
            except Exception:
                time.sleep(2 * (i + 1))

    threading.Thread(target=send, daemon=True).start()


def run_job(browser, job):
    job.set_status("running")
    job.emit({"type": "start", "url": job.opts["url"], "job_id": job.id})
    try:
        o = job.opts
        engine = o["engine"]
        r = None
        if engine in ("http", "auto"):
            try:
                r = run_http(o["url"], o, job.emit, auto=(engine == "auto"))
            except NeedBrowser as e:
                job.emit({"type": "phase", "phase": "fallback_to_browser", "reason": str(e)})
            except Exception as e:
                if engine == "http":
                    raise
                job.emit({"type": "phase", "phase": "fallback_to_browser", "reason": f"http engine failed: {e}"})
        if r is None:
            r = capture(browser, o["url"], o, job.emit)
        with open(job.html_path, "w", encoding="utf-8") as f:
            f.write(r["html"])
        by_type = defaultdict(int)
        for x in r["resources"]:
            by_type[x["resource_type"] or "other"] += 1
        report = {"url": job.opts["url"], "final_url": r["final_url"], "status": r["status"], "title": r["title"],
                  "finish_reason": r["finish_reason"], "blocked": r["blocked"], "options": job.opts,
                  "timings": r["timings"], "skipped": r["skipped"], "timeline": list(job.events)}
        stats = build_zip(job.zip_path, r["pages"], r["resources"], r["sources"], r["frames"], report,
                          make_extras(r), snapshot=job.opts["snapshot"])
        b = f"/api/jobs/{job.id}"
        job.result = {
            "job_id": job.id, "url": job.opts["url"], "final_url": r["final_url"], "status": r["status"],
            "title": r["title"], "html_length": len(r["html"]), "finish_reason": r["finish_reason"],
            "blocked": r["blocked"], "tech": r["info"].get("tech", []), "timings": r["timings"],
            "auto_unlock_clicks": r.get("auto_unlock_clicks", []),
            "pages": [{"url": p["url"], "title": p["title"], "status": p["status"],
                       "total_ms": p["timings"].get("total_ms"),
                       "loader_ms": p["timings"].get("loading_screen_total_ms",
                                                     sum(s["duration_ms"] for s in p["timings"].get("loading_screens", [])))}
                      for p in r["pages"]],
            "resources": {"count": len(r["resources"]), "by_type": dict(by_type), "skipped": len(r["skipped"]),
                          "frames": len(r["frames"]), **stats},
            "zip_bytes": os.path.getsize(job.zip_path),
            "html_url": b + "/html", "zip_url": b + "/zip",
            "files": {n: f"{b}/file/{n}" for n in ("snapshot.html", "screenshot.jpg", "meta.json", "links.json",
                                                    "pages.json", "network.json", "console.json", "storage.json",
                                                    "rendered_with_shadow.html", "report.json", "manifest.json")}}
        job.set_status("done")
        job.emit({"type": "result", **job.result})
    except Exception as e:
        job.error = str(e)
        job.set_status("error")
        job.emit({"type": "error", "error": str(e)})
    fire_webhook(job)


def worker(idx):
    from playwright.sync_api import sync_playwright
    while True:
        try:
            with sync_playwright() as p:
                kw = {"args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                               "--disable-blink-features=AutomationControlled"]}
                if PROXY_URL:
                    kw["proxy"] = {"server": PROXY_URL}
                browser = p.chromium.launch(**kw)
                while True:
                    job = QUEUE.get()
                    run_job(browser, job)
                    if not browser.is_connected():
                        break
        except Exception as e:
            print(f"[worker {idx}] restart: {e}", flush=True)
            time.sleep(2)


def janitor():
    n = 0
    while True:
        time.sleep(10)
        STORE.flush()
        n += 1
        if n % 6:
            continue
        now = time.time()
        for jid, j in list(JOBS.items()):
            if j.status not in ACTIVE and now - j.created > JOB_TTL:
                JOBS.pop(jid, None)
                for p in (j.zip_path, j.html_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        for k, j in list(CACHE.items()):
            if j.id not in JOBS:
                CACHE.pop(k, None)


if not os.environ.get("NO_WORKERS"):
    for _i in range(WORKERS):
        threading.Thread(target=worker, args=(_i,), daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()


# ---------------------------------------------------------------- helpers
def extract_key():
    auth = request.headers.get("Authorization", "")
    return request.headers.get("x-api-key") or (auth[7:] if auth.startswith("Bearer ") else "") \
        or request.values.get("key") or ""


def is_private(host):
    try:
        for _, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
    except Exception:
        pass
    return False


def check_url(url, what="url"):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"invalid {what}")
    if not ALLOW_PRIVATE and is_private(p.hostname):
        raise ValueError(f"private/internal addresses are blocked ({what})")


def parse_opts():
    d = dict(request.values.items())
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        d.update(j)

    def num(k, default, lo, hi):
        try:
            x = float(d.get(k, default))
        except (TypeError, ValueError):
            x = default
        return max(lo, min(hi, x))

    def flag(k, default):
        v = d.get(k, default)
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")

    def jsonish(k, typ):
        v = d.get(k)
        if v in (None, ""):
            return typ()
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                raise ValueError(f"{k} must be valid JSON")
        if not isinstance(v, typ):
            raise ValueError(f"{k} must be a {typ.__name__}")
        return v

    url = str(d.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    if "://" not in url:
        url = "https://" + url
    check_url(url)

    crawl = flag("crawl", False)
    click = d.get("click") or []
    if isinstance(click, str):
        click = [c.strip() for c in click.split("||") if c.strip()]
    unlock_text = d.get("unlock_text") or []
    if isinstance(unlock_text, str):
        unlock_text = [c.strip().lower() for c in unlock_text.split("||") if c.strip()]
    headers = {str(k)[:100]: str(v)[:500] for k, v in list(jsonish("headers", dict).items())[:20]}
    cookies = [c for c in jsonish("cookies", list)[:50] if isinstance(c, dict)]
    for fld in ("include", "exclude"):
        pat = str(d.get(fld) or "")[:200]
        if pat:
            try:
                re.compile(pat)
            except re.error:
                raise ValueError(f"{fld} is not a valid regex")
        d[fld] = pat
    webhook = str(d.get("webhook") or "").strip()
    if webhook:
        check_url(webhook, "webhook")
    engine = str(d.get("engine") or "auto").lower()
    if engine not in ("auto", "http", "browser"):
        raise ValueError("engine must be auto, http or browser")
    speed = str(d.get("speed") or "balanced").lower()
    if speed not in SPEED_MODES:
        raise ValueError("speed must be full, balanced or turbo")
    sm = SPEED_MODES[speed]
    out = {
        "url": url, "max_wait": num("max_wait", 30, 3, 120), "settle_ms": num("settle_ms", 1200, 200, 10000),
        "scroll": flag("scroll", True), "assets": flag("assets", True), "sourcemaps": flag("sourcemaps", True),
        "mobile": flag("mobile", False), "wait_selector": str(d.get("wait_selector") or "")[:200],
        "click": [str(c)[:200] for c in click][:10], "headers": headers, "cookies": cookies,
        "user_agent": str(d.get("user_agent") or "")[:300],
        "viewport_w": int(num("viewport_w", 0, 0, 3840)) or None, "viewport_h": int(num("viewport_h", 0, 0, 4000)) or None,
        "screenshot": flag("screenshot", True), "snapshot": flag("snapshot", True), "shadow": flag("shadow", True),
        "crawl": crawl, "max_pages": int(num("max_pages", 10 if crawl else 1, 1, MAX_PAGES)) if crawl else 1,
        "depth": int(num("depth", 2, 1, 5)), "crawl_budget": num("crawl_budget", 90, 10, 300),
        "include": d["include"], "exclude": d["exclude"], "cache": num("cache", 0, 0, 3600), "webhook": webhook,
        "engine": engine, "speed": speed, "crawl_concurrency": int(num("crawl_concurrency", sm["crawl_concurrency"], 1, 8)),
        "auto_unlock": flag("auto_unlock", True), "unlock_rounds": int(num("unlock_rounds", 3, 0, 6)),
        "unlock_text": unlock_text[:10]}
    out["settle_ms"] = num("settle_ms", sm["settle_ms"], 100, 10000)
    out.update(idle_ms=sm["idle_ms"], min_ms=sm["min_ms"], fast_scroll=sm["fast_scroll"])
    if speed == "turbo":
        out["screenshot"] = flag("screenshot", False)
        out["sourcemaps"] = flag("sourcemaps", False)
    return out


def make_job():
    try:
        opts = parse_opts()
    except ValueError as e:
        return None, (jsonify(error=str(e)), 400)
    bot, now, lim = g.bot, time.time(), g.limits
    fp = hashlib.sha1(json.dumps({k: v for k, v in opts.items() if k not in ("webhook", "cache")},
                                  sort_keys=True, default=str).encode()).hexdigest()
    g.cached = False
    if opts["cache"] > 0:
        j = CACHE.get((bot, fp))
        if j and j.id in JOBS and (j.status in ACTIVE or (j.status == "done" and now - j.finished <= opts["cache"])):
            g.cached = True
            return j, None
    dq = RATE[bot]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= lim["rate"]:
        return None, (jsonify(error="rate limit exceeded", limit_per_min=lim["rate"]), 429, {"Retry-After": "10"})
    if QUEUE.qsize() >= MAX_QUEUE:
        return None, (jsonify(error="queue full, retry shortly"), 503, {"Retry-After": "5"})
    if sum(1 for j in list(JOBS.values()) if j.bot == bot and j.status in ACTIVE) >= lim["active"]:
        return None, (jsonify(error="too many active jobs for this key", limit=lim["active"]), 429)
    if not STORE.hit(bot, lim["daily"]):
        return None, (jsonify(error="daily limit reached for this key", daily_limit=lim["daily"]), 429)
    dq.append(now)
    job = Job(bot, opts, fp)
    JOBS[job.id] = job
    if opts["cache"] > 0:
        CACHE[(bot, fp)] = job
    job.emit({"type": "queued", "job_id": job.id, "queue_position": QUEUE.qsize() + 1})
    QUEUE.put(job)
    return job, None


def own_job(jid):
    j = JOBS.get(jid)
    return j if j and j.bot == g.bot else None


def safe(resp):
    """Captured pages are untrusted: sandbox them so their scripts can never touch this origin."""
    resp.headers["Content-Security-Policy"] = "sandbox"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def stream_job(job, start=0):
    i = start
    while True:
        with job.cond:
            if i >= len(job.events) and job.status in ACTIVE:
                job.cond.wait(15)
            batch = job.events[i:]
            i += len(batch)
            done = job.status not in ACTIVE
        if batch:
            for ev in batch:
                yield f"data: {json.dumps(ev)}\n\n"
        elif done:
            return
        else:
            yield ": ping\n\n"


def sse(job, start=0):
    return Response(stream_with_context(stream_job(job, start)), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- hooks
@app.before_request
def guard():
    if request.method == "OPTIONS":
        return Response(status=204)
    if not request.path.startswith("/api/"):
        return None
    k = extract_key()
    if k:
        rec, err = STORE.check(k)
        if err == "invalid":
            return jsonify(error="invalid API key"), 401
        if err == "expired":
            return jsonify(error="API key expired"), 401
        if err == "disabled":
            return jsonify(error="API key disabled or revoked"), 403
        g.bot, g.limits = rec["id"], STORE.limits(rec)
    elif STORE.require_key():
        return jsonify(error="API key required: send x-api-key header or ?key="), 401
    else:
        g.bot, g.limits = "public", STORE.public_limits()


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return "ok"


@app.get("/api/stats")
def stats():
    jobs = list(JOBS.values())
    return jsonify(workers=WORKERS, queued=QUEUE.qsize(), jobs=len(jobs),
                   running=sum(j.status == "running" for j in jobs),
                   mine=sum(j.bot == g.bot for j in jobs), key=STORE.name_of(g.bot))


@app.route("/api/jobs", methods=["GET", "POST"])
def create_job():
    job, err = make_job()
    if err:
        return err
    b = f"/api/jobs/{job.id}"
    return jsonify(job_id=job.id, status=job.status, cached=g.cached, status_url=b, live_url=b + "/live",
                   html_url=b + "/html", zip_url=b + "/zip"), 202


@app.get("/api/jobs/<jid>")
def job_status(jid):
    j = own_job(jid)
    if not j:
        return jsonify(error="not found"), 404
    since = int(request.args.get("since", 0) or 0)
    return jsonify(job_id=j.id, status=j.status, error=j.error, result=j.result, events=j.events[since:])


@app.get("/api/jobs/<jid>/live")
def job_live(jid):
    j = own_job(jid)
    return sse(j, int(request.args.get("since", 0) or 0)) if j else (jsonify(error="not found"), 404)


@app.get("/api/jobs/<jid>/html")
def job_html(jid):
    j = own_job(jid)
    if not j or j.status != "done":
        return jsonify(error="not ready or not found"), 404
    return safe(send_file(j.html_path, mimetype="text/html; charset=utf-8"))


@app.get("/api/jobs/<jid>/zip")
def job_zip(jid):
    j = own_job(jid)
    if not j or j.status != "done":
        return jsonify(error="not ready or not found"), 404
    host = (urlparse(j.opts["url"]).hostname or "site").replace(".", "_")
    return send_file(j.zip_path, mimetype="application/zip", as_attachment=True, download_name=f"{host}.zip")


@app.get("/api/jobs/<jid>/file/<path:name>")
def job_file(jid, name):
    """Read one file out of the job's zip (snapshot.html, screenshot.jpg, meta.json, pages/about.html, ...)."""
    j = own_job(jid)
    if not j or j.status != "done":
        return jsonify(error="not ready or not found"), 404
    with zipfile.ZipFile(j.zip_path) as z:
        if name not in z.namelist():
            return jsonify(error="no such file in zip", files=z.namelist()[:200]), 404
        data = z.read(name)
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return safe(Response(data, mimetype=mt))


@app.route("/api/fetch", methods=["GET", "POST"])
def api_fetch():
    """Synchronous: waits for the job and returns the result (html + timings)."""
    job, err = make_job()
    if err:
        return err
    o = job.opts
    deadline = time.time() + o["max_wait"] + (o["crawl_budget"] if o["crawl"] else 0) + 90
    with job.cond:
        while job.status in ACTIVE and time.time() < deadline:
            job.cond.wait(1)
    if job.status != "done":
        return jsonify(error=job.error or "timeout", job_id=job.id, status=job.status), 502
    if request.values.get("format") == "html":
        return safe(send_file(job.html_path, mimetype="text/html; charset=utf-8"))
    out = dict(job.result, cached=g.cached)
    if request.values.get("html", "1") != "0":
        with open(job.html_path, encoding="utf-8") as f:
            out["html"] = f.read()
    out["timeline"] = job.events
    return jsonify(out)


@app.route("/api/live", methods=["GET", "POST"])
def api_live():
    """Creates a job and streams live per-second events (Server-Sent Events)."""
    job, err = make_job()
    return err if err else sse(job)


UI = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>url2src</title>
<style>body{font:14px system-ui;margin:12px;max-width:780px}input,button{font:inherit;padding:9px;margin:3px 0;width:100%;box-sizing:border-box}
.r{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0}.r label{display:flex;gap:5px;align-items:center}.r input{width:auto;margin:0}
pre{background:#0d1117;color:#7ee787;padding:8px;height:320px;overflow:auto;font-size:12px;white-space:pre-wrap}a{display:inline-block;margin:8px 10px 0 0}</style>
<h3>url2src</h3><input id=u placeholder="https://example.com"><input id=k placeholder="API key (if required)">
<div class=r><label><input type=checkbox id=crawl>Crawl site</label><label>Pages <input id=mp type=number value=10 style="width:60px"></label>
<label><input type=checkbox id=mob>Mobile</label><label><input type=checkbox id=sc checked>Scroll</label></div>
<div class=r><label>Engine <select id=eng><option value=auto selected>Auto (fast, falls back if needed)</option><option value=http>HTTP only (fastest, no JS)</option><option value=browser>Browser (JS sites, loading screens)</option></select></label>
<label>Speed <select id=spd><option value=balanced selected>Balanced</option><option value=turbo>Turbo</option><option value=full>Full (most accurate)</option></select></label></div>
<button id=go>Start</button><pre id=log></pre><div id=dl></div><a href="/admin">Admin panel</a>
<script>
const $=id=>document.getElementById(id);$('k').value=localStorage.k||'';
$('go').onclick=()=>{localStorage.k=$('k').value;$('log').textContent='';$('dl').innerHTML='';
const q=new URLSearchParams({url:$('u').value,key:$('k').value,crawl:$('crawl').checked?1:0,max_pages:$('mp').value,mobile:$('mob').checked?1:0,scroll:$('sc').checked?1:0,engine:$('eng').value,speed:$('spd').value});
const es=new EventSource('/api/live?'+q);
const add=t=>{$('log').textContent+=t+'\\n';$('log').scrollTop=1e9};
es.onmessage=e=>{const d=JSON.parse(e.data);
 if(d.type==='tick')add((d.page?'['+d.page.split('/').slice(3).join('/')+'] ':'')+d.elapsed_s+'s ready='+d.ready_state+' loader='+(d.loading_screen_visible?d.loader:'no')+' loader_total='+d.loading_screen_total_ms+'ms req='+d.requests_in_flight+' files='+d.resources_captured);
 else if(d.type==='loader_start')add('>> loading screen START @'+d.at_ms+'ms '+d.selector);
 else if(d.type==='loader_end')add('<< loading screen END @'+d.at_ms+'ms lasted '+d.duration_ms+'ms');
 else if(d.type==='page')add('PAGE '+d.status+' '+d.url+' '+d.total_ms+'ms loader '+d.loader_ms+'ms');
 else if(d.type==='result'){add('DONE ['+d.timings.engine+'] wait='+d.timings.total_ms+'ms loader='+d.timings.loading_screen_total_ms+'ms pages='+d.pages.length+' files='+d.resources.count+(d.blocked?' (BLOCKED by site?)':''));
  const k='?key='+encodeURIComponent($('k').value),L=(u,t)=>'<a href="'+u+k+'" target=_blank>'+t+'</a>';
  $('dl').innerHTML=L(d.zip_url,'ZIP')+L(d.files['snapshot.html'],'Single-file snapshot')+L(d.files['screenshot.jpg'],'Screenshot')+L(d.files['meta.json'],'Meta/tech')+L(d.files['network.json'],'Network log');es.close()}
 else if(d.type==='error'){add('ERROR '+d.error);es.close()}
 else add(JSON.stringify(d))};
es.onerror=()=>{es.close()}};
</script>"""


@app.get("/")
def home():
    return Response(UI, mimetype="text/html")


register_admin(app, STORE, ADMIN_KEY, lambda: JOBS, lambda: QUEUE.qsize(), WORKERS)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
