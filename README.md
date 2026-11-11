# url2src

URL -> full rendered website source + every asset (HTML/CSS/JS/images/fonts/JSON, TS via source maps) as ZIP.
Detects loading screens, reports their duration in ms, live per-second via SSE. Multi-bot (API keys, queue, rate-limit).

## Deploy on Render
1. Push this folder to a GitHub repo (Render cannot take a zip directly).
2. Render -> New -> Blueprint -> pick the repo (render.yaml). Or New -> Web Service -> Runtime: Docker.
3. Env: API_KEYS = `botA:key1,botB:key2` (each bot = its own key + rate limit). WORKERS = parallel pages (1 for 512MB RAM).
4. Open https://YOUR-APP.onrender.com/ for the test UI.

## API (send key via `x-api-key` header or `?key=`)
- GET  /api/fetch?url=URL            -> JSON {html, timings, resources, zip_url, timeline}   (&format=html raw, &html=0 no body)
- GET  /api/live?url=URL             -> SSE live events (tick every 1s, loader_start/loader_end with ms)
- POST /api/jobs  (url=...)          -> 202 {job_id, status_url, live_url, zip_url}   (async, best for bots)
- GET  /api/jobs/<id>[?since=N]      -> status + events
- GET  /api/jobs/<id>/live | /html | /zip
- GET  /api/stats  /health

Options: max_wait=30 (3-120) settle_ms=1200 scroll=1 assets=1 sourcemaps=1 mobile=0 wait_selector=CSS

## Env
API_KEYS, WORKERS(1), RATE_PER_MIN(60), PER_BOT_ACTIVE(10), MAX_QUEUE(100), JOB_TTL(1800),
MAX_BODY_MB(25), MAX_TOTAL_MB(150), MAX_EXTRA_URLS(200), ALLOW_PRIVATE(0)

ZIP layout: index.html (links rewritten to local), original.html, assets/<host>/..., sources/ (original TS/JSX from source maps),
frames/, manifest.json, report.json (loader timeline).
