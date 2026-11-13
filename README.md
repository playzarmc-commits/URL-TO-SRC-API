# url2src MAX

URL -> full rendered website source + every asset. Loading-screen detection (ms), live per-second status,
multi-page crawl, single-file snapshot, screenshot, shadow DOM, source maps (TS), multi-bot keys, admin panel.
Two engines, picked automatically: a browser-less HTTP mirror (milliseconds, parallel) for static/server-rendered
sites, and a real Chromium for JS-rendered sites and loading-screen timing.

## Deploy (Render)
1. Push this folder to a GitHub repo, then Render -> New Web Service -> Docker (or Blueprint with render.yaml).
2. Optional env: ADMIN_KEY (default asraful123), SIGNING_SECRET (random string, recommended), WORKERS (1 on 512MB RAM),
   FETCH_THREADS(32), PROXY_URL, REVOKED_IDS, DATABASE_URL, KEYS_FILE, MAX_PAGES(50), MAX_ASSETS(1500),
   RATE_PER_MIN(60), PER_BOT_ACTIVE(10).
3. Test UI: `/`   Admin panel: `/admin`

## Speed
- `engine`: `auto` (default, tries HTTP first and only opens a browser when the page needs JS/has a loading
  screen/is blocked), `http` (never opens a browser: fastest, works on static & server-rendered sites),
  `browser` (always real Chromium: needed for loading-screen timing, client-rendered apps, logins).
- `speed`: `balanced` (default), `turbo` (short settle waits, 8 pages crawled in parallel, screenshots/source
  maps off by default — use when you don't need pixel-perfect waiting), `full` (longest, most careful waits —
  use for sites with slow/unusual loading screens). Each sets `settle_ms`/`idle_ms` defaults you can still override.
- Asset and source-map fetching run on a thread pool (`FETCH_THREADS`, default 32) in parallel, overlapped with
  page work instead of after it. Crawled pages load `crawl_concurrency` (default by speed, max 8) at a time.
- `cache=N` (seconds): an identical request (same key + same options) returns the same job instead of re-running.

## Keys (no database needed)
- Signed keys (`sk_...`, default): limits/expiry/name are inside the key, verified with SIGNING_SECRET, so they keep working
  after Render restarts. Revoke from the panel (open the panel after a restart so it re-applies the revoked list
  from this browser's backup, or put ids in REVOKED_IDS). Changing SIGNING_SECRET/ADMIN_KEY invalidates all signed keys.
- Stored keys (`u2s_...` or custom): editable, but kept in a file that free Render wipes on restart (use DATABASE_URL or a Disk).
- Panel: generate, custom key, limits (req/min, active jobs, daily, expiry), usage, enable/disable, backup/restore.

## API (header `x-api-key`, `Authorization: Bearer`, or `?key=`)
- GET|POST /api/fetch?url=  -> JSON (html, timings, pages, files)   (&format=html, &html=0)
- GET|POST /api/live?url=   -> SSE live events
- POST /api/jobs            -> async job; GET /api/jobs/<id>, /live, /html, /zip, /file/<name-in-zip>
Options (query or JSON body): engine(auto|http|browser) speed(balanced|turbo|full) max_wait settle_ms scroll
assets sourcemaps mobile wait_selector click("sel1||sel2") headers{} cookies[] user_agent viewport_w viewport_h
screenshot snapshot shadow crawl max_pages depth crawl_budget crawl_concurrency include exclude (regex)
cache(seconds) webhook(url) auto_unlock(default on) unlock_rounds(default 3) unlock_text("phrase1||phrase2")

ZIP: index.html, pages/, assets/, sources/ (TS), snapshot.html (single file), original.html, rendered_with_shadow.html,
screenshot.jpg, meta.json (tech stack), links.json, pages.json, network.json, console.json, storage.json, manifest.json, report.json.

## Notes
- Served captured pages/zips carry `Content-Security-Policy: sandbox` so a malicious captured site's JS can never
  run against this origin.
- `engine=http` cannot measure a loading screen (there is no JS execution) — use `browser` or `auto` for that.
- 100% success on every website is not something any tool can promise: heavy bot-protection (Cloudflare/captcha)
  can still block requests. `PROXY_URL` (residential proxy) + `cookies`/`headers` help on protected sites.

## Auto-unlock (click-through gate pages)
Many sites show a one-click gate before the real content ("Generate Key", "Continue", "Enter", "Unlock",
"I Agree", "Accept", cookie banners...). The browser engine now detects and clicks these automatically
(on by default, `auto_unlock=1`), re-settling after each click, up to `unlock_rounds` times (default 3).
- Only short, exact-phrase button/link text from a safe built-in whitelist is clicked (continue/enter/unlock/
  generate key/accept/agree/proceed/watch now/start/get started/open/view content/i agree, etc).
- Anything that looks transactional or account-related is never auto-clicked: subscribe, sign up, register,
  login, buy, order, checkout, pay, donate, delete, remove, confirm payment, send message. A button inside a
  form with real input fields (not just hidden ones) is also skipped, so a normal form is never auto-submitted.
- Add your own site-specific button text with `unlock_text=Generate My Key||Show Result` (checked after the
  built-in list; still filtered by the same blocked-word list).
- Dialogs (alert/confirm/prompt) that a click triggers are auto-dismissed so a click can never hang the job.
- Clicks made are reported in the job result (`auto_unlock_clicks`) and in `meta.json` inside the zip, so you
  can always see what, if anything, was clicked.
- Set `auto_unlock=0` to disable entirely and only use your own `click="selector"` list.
