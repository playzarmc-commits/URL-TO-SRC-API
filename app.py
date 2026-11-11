import hmac
import ipaddress
import json
import os
import queue
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from urllib.parse import urlparse

from flask import Flask, Response, g, jsonify, request, send_file, stream_with_context

from capture import capture
from pack import build_zip

# ---------------------------------------------------------------- config
WORKERS = int(os.environ.get("WORKERS", "1"))               # parallel browsers/pages workers
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "100"))
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "60"))     # per bot
PER_BOT_ACTIVE = int(os.environ.get("PER_BOT_ACTIVE", "10")) # queued+running jobs per bot
JOB_TTL = int(os.environ.get("JOB_TTL", "1800"))             # seconds to keep results
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/url2src")
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE", "0") == "1"
os.makedirs(DATA_DIR, exist_ok=True)

KEYS = {}  # bot name -> key.  API_KEYS="botA:key1,botB:key2"  or  "key1,key2"
for i, part in enumerate(p.strip() for p in os.environ.get("API_KEYS", "").split(",") if p.strip()):
    name, key = part.split(":", 1) if ":" in part else (f"bot{i + 1}", part)
    KEYS[name] = key

ACTIVE = ("queued", "running")
app = Flask(__name__)
QUEUE = queue.Queue()
JOBS, RATE = {}, defaultdict(deque)


class Job:
    def __init__(self, bot, opts):
        self.id, self.bot, self.opts = uuid.uuid4().hex[:16], bot, opts
        self.status, self.events, self.cond = "queued", [], threading.Condition()
        self.result, self.error, self.created = None, None, time.time()
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
            self.cond.notify_all()


# ---------------------------------------------------------------- workers
def run_job(browser, job):
    job.set_status("running")
    job.emit({"type": "start", "url": job.opts["url"], "job_id": job.id})
    try:
        r = capture(browser, job.opts["url"], job.opts, job.emit)
        with open(job.html_path, "w", encoding="utf-8") as f:
            f.write(r["html"])
        by_type = defaultdict(int)
        for x in r["resources"]:
            by_type[x["resource_type"] or "other"] += 1
        report = {"url": job.opts["url"], "final_url": r["final_url"], "status": r["status"],
                  "title": r["title"], "finish_reason": r["finish_reason"], "timings": r["timings"],
                  "skipped": r["skipped"], "timeline": list(job.events)}
        stats = build_zip(job.zip_path, r["final_url"], r["html"], r["resources"], r["sources"],
                          r["frames"], report)
        base = f"/api/jobs/{job.id}"
        job.result = {
            "job_id": job.id, "url": job.opts["url"], "final_url": r["final_url"], "status": r["status"],
            "title": r["title"], "html_length": len(r["html"]), "finish_reason": r["finish_reason"],
            "timings": r["timings"],
            "resources": {"count": len(r["resources"]), "by_type": dict(by_type),
                          "skipped": len(r["skipped"]), "frames": len(r["frames"]), **stats},
            "zip_bytes": os.path.getsize(job.zip_path), "html_url": base + "/html", "zip_url": base + "/zip"}
        job.set_status("done")
        job.emit({"type": "result", **job.result})
    except Exception as e:
        job.error = str(e)
        job.set_status("error")
        job.emit({"type": "error", "error": str(e)})


def worker(idx):
    from playwright.sync_api import sync_playwright
    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                                                  "--disable-blink-features=AutomationControlled"])
                while True:
                    job = QUEUE.get()
                    run_job(browser, job)
                    if not browser.is_connected():
                        break
        except Exception as e:
            print(f"[worker {idx}] restart: {e}", flush=True)
            time.sleep(2)


def janitor():
    while True:
        time.sleep(60)
        now = time.time()
        for jid, j in list(JOBS.items()):
            if j.status not in ACTIVE and now - j.created > JOB_TTL:
                JOBS.pop(jid, None)
                for p in (j.zip_path, j.html_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


if not os.environ.get("NO_WORKERS"):
    for _i in range(WORKERS):
        threading.Thread(target=worker, args=(_i,), daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()


# ---------------------------------------------------------------- helpers
def get_bot():
    if not KEYS:
        return "public"
    auth = request.headers.get("Authorization", "")
    k = request.headers.get("x-api-key") or (auth[7:] if auth.startswith("Bearer ") else "") \
        or request.values.get("key") or ""
    for name, key in KEYS.items():
        if k and hmac.compare_digest(k, key):
            return name
    return None


def is_private(host):
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
    except Exception:
        pass
    return False


def parse_opts():
    v = request.values
    url = (v.get("url") or "").strip()
    if not url:
        return None, "url is required"
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None, "invalid url"
    if not ALLOW_PRIVATE and is_private(p.hostname):
        return None, "private/internal addresses are blocked"

    def num(k, d, lo, hi):
        try:
            x = float(v.get(k, d))
        except ValueError:
            x = d
        return max(lo, min(hi, x))

    flag = lambda k, d: v.get(k, "1" if d else "0").lower() in ("1", "true", "yes", "on")
    return {"url": url, "max_wait": num("max_wait", 30, 3, 120), "settle_ms": num("settle_ms", 1200, 200, 10000),
            "scroll": flag("scroll", True), "assets": flag("assets", True), "sourcemaps": flag("sourcemaps", True),
            "mobile": flag("mobile", False), "wait_selector": (v.get("wait_selector") or "")[:200]}, None


def make_job():
    opts, err = parse_opts()
    if err:
        return None, (jsonify(error=err), 400)
    bot, now = g.bot, time.time()
    dq = RATE[bot]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_PER_MIN:
        return None, (jsonify(error="rate limit exceeded", limit_per_min=RATE_PER_MIN), 429, {"Retry-After": "10"})
    if QUEUE.qsize() >= MAX_QUEUE:
        return None, (jsonify(error="queue full, retry shortly"), 503, {"Retry-After": "5"})
    if sum(1 for j in list(JOBS.values()) if j.bot == bot and j.status in ACTIVE) >= PER_BOT_ACTIVE:
        return None, (jsonify(error="too many active jobs for this bot", limit=PER_BOT_ACTIVE), 429)
    dq.append(now)
    job = Job(bot, opts)
    JOBS[job.id] = job
    job.emit({"type": "queued", "job_id": job.id, "queue_position": QUEUE.qsize() + 1})
    QUEUE.put(job)
    return job, None


def own_job(jid):
    j = JOBS.get(jid)
    return j if j and j.bot == g.bot else None


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
    if request.path.startswith("/api/"):
        bot = get_bot()
        if bot is None:
            return jsonify(error="unauthorized: send x-api-key header or ?key="), 401
        g.bot = bot


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
                   mine=sum(j.bot == g.bot for j in jobs), bot=g.bot)


@app.route("/api/jobs", methods=["GET", "POST"])
def create_job():
    job, err = make_job()
    if err:
        return err
    b = f"/api/jobs/{job.id}"
    return jsonify(job_id=job.id, status=job.status, status_url=b, live_url=b + "/live",
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
    return send_file(j.html_path, mimetype="text/html; charset=utf-8")


@app.get("/api/jobs/<jid>/zip")
def job_zip(jid):
    j = own_job(jid)
    if not j or j.status != "done":
        return jsonify(error="not ready or not found"), 404
    host = (urlparse(j.opts["url"]).hostname or "site").replace(".", "_")
    return send_file(j.zip_path, mimetype="application/zip", as_attachment=True, download_name=f"{host}.zip")


@app.route("/api/fetch", methods=["GET", "POST"])
def api_fetch():
    """Synchronous: waits for the job and returns the result (html + timings)."""
    job, err = make_job()
    if err:
        return err
    deadline = time.time() + job.opts["max_wait"] + 90
    with job.cond:
        while job.status in ACTIVE and time.time() < deadline:
            job.cond.wait(1)
    if job.status != "done":
        return jsonify(error=job.error or "timeout", job_id=job.id, status=job.status), 502
    if request.values.get("format") == "html":
        return send_file(job.html_path, mimetype="text/html; charset=utf-8")
    out = dict(job.result)
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
pre{background:#0d1117;color:#7ee787;padding:8px;height:320px;overflow:auto;font-size:12px;white-space:pre-wrap}a{display:inline-block;margin:8px 10px 0 0}</style>
<h3>url2src</h3><input id=u placeholder="https://example.com"><input id=k placeholder="API key (if set)">
<button id=go>Start</button><pre id=log></pre><div id=dl></div>
<script>
const $=id=>document.getElementById(id);$('k').value=localStorage.k||'';
$('go').onclick=()=>{localStorage.k=$('k').value;$('log').textContent='';$('dl').innerHTML='';
const es=new EventSource('/api/live?'+new URLSearchParams({url:$('u').value,key:$('k').value}));
const add=t=>{$('log').textContent+=t+'\\n';$('log').scrollTop=1e9};
es.onmessage=e=>{const d=JSON.parse(e.data);
 if(d.type==='tick')add(d.elapsed_s+'s  ready='+d.ready_state+'  loader='+(d.loading_screen_visible?d.loader:'no')+'  loader_total='+d.loading_screen_total_ms+'ms  req='+d.requests_in_flight+'  files='+d.resources_captured);
 else if(d.type==='loader_start')add('>> loading screen START @'+d.at_ms+'ms  '+d.selector);
 else if(d.type==='loader_end')add('<< loading screen END @'+d.at_ms+'ms  lasted '+d.duration_ms+'ms');
 else if(d.type==='result'){add('DONE  wait='+d.timings.total_ms+'ms  loader='+d.timings.loading_screen_total_ms+'ms  files='+d.resources.count);
  const k='?key='+encodeURIComponent($('k').value);
  $('dl').innerHTML='<a href="'+d.zip_url+k+'">Download ZIP</a><a href="'+d.html_url+k+'" target=_blank>Open HTML</a>';es.close()}
 else if(d.type==='error'){add('ERROR '+d.error);es.close()}
 else add(JSON.stringify(d))};
es.onerror=()=>{es.close()}};
</script>"""


@app.get("/")
def home():
    return Response(UI, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
