"""API key store. Persists to a JSON file (KEYS_FILE) or Postgres (DATABASE_URL)."""
import hmac
import json
import re
import secrets
import threading
import time

KEY_RE = re.compile(r"^[A-Za-z0-9_.@!#$%^&*+=~-]{6,128}$")


def _num(v, name, lo=1):
    if v in (None, "", False):
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if n < lo:
        raise ValueError(f"{name} must be >= {lo}")
    return n


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


class KeyStore:
    def __init__(self, path, db_url=None, default_rate=60, default_active=10):
        self.path, self.db_url = path, db_url
        self.default_rate, self.default_active = default_rate, default_active
        self.lock = threading.RLock()
        self.keys, self.static, self.dirty, self.readonly = {}, {}, False, False
        self.settings = {"require_key": False, "public_rate_per_min": None, "public_max_active": None}
        self._load()

    # ------------------------------------------------------------ persistence
    def _read(self):
        if self.db_url:
            import psycopg2
            c = psycopg2.connect(self.db_url)
            try:
                with c, c.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS u2s_store (id INT PRIMARY KEY, data TEXT)")
                    cur.execute("SELECT data FROM u2s_store WHERE id=1")
                    row = cur.fetchone()
                return json.loads(row[0]) if row else {}
            finally:
                c.close()
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write(self, text):
        if self.db_url:
            import psycopg2
            c = psycopg2.connect(self.db_url)
            try:
                with c, c.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS u2s_store (id INT PRIMARY KEY, data TEXT)")
                    cur.execute("INSERT INTO u2s_store (id, data) VALUES (1, %s) "
                                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data", (text,))
            finally:
                c.close()
            return
        import os
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)

    def _load(self):
        for attempt in range(3):
            try:
                d = self._read()
                self.keys = {k["id"]: k for k in d.get("keys", [])}
                self.settings.update(d.get("settings", {}))
                return
            except Exception as e:
                print(f"[keystore] load failed ({attempt + 1}/3): {e}", flush=True)
                time.sleep(1)
        self.readonly = True  # never overwrite a store we could not read
        print("[keystore] READ-ONLY: could not load existing keys", flush=True)

    def save(self):
        with self.lock:
            if self.readonly:
                return
            try:
                self._write(json.dumps({"keys": list(self.keys.values()), "settings": self.settings}))
                self.dirty = False
            except Exception as e:
                self.dirty = True
                print(f"[keystore] save failed: {e}", flush=True)

    def flush(self):
        if self.dirty:
            self.save()

    # ------------------------------------------------------------ lookup
    def _all(self):
        return list(self.static.values()) + list(self.keys.values())

    def set_static(self, mapping):
        for name, key in mapping.items():
            rid = "env-" + re.sub(r"[^A-Za-z0-9]", "_", name)
            self.static[rid] = {"id": rid, "name": name, "key": key, "enabled": True, "static": True,
                                "created": None, "expires": None, "rate_per_min": None, "max_active": None,
                                "daily_limit": None, "total": 0, "day": "", "day_count": 0,
                                "last_used": None, "note": "from API_KEYS env"}

    def find(self, k):
        kb, found = k.encode("utf-8", "ignore"), None
        for r in self._all():
            if hmac.compare_digest(kb, r["key"].encode("utf-8")):
                found = r
        return found

    def check(self, k):
        r = self.find(k)
        if not r:
            return None, "invalid"
        if not r.get("enabled", True):
            return r, "disabled"
        if r.get("expires") and time.time() > r["expires"]:
            return r, "expired"
        return r, None

    def name_of(self, rid):
        r = self.keys.get(rid) or self.static.get(rid)
        return r["name"] if r else ("public" if rid == "public" else "(deleted)")

    def require_key(self):
        return bool(self.settings.get("require_key")) or bool(self.static)

    def limits(self, r):
        return {"rate": r.get("rate_per_min") or self.default_rate,
                "active": r.get("max_active") or self.default_active, "daily": r.get("daily_limit")}

    def public_limits(self):
        return {"rate": self.settings.get("public_rate_per_min") or self.default_rate,
                "active": self.settings.get("public_max_active") or self.default_active, "daily": None}

    def hit(self, rid, daily):
        """Count one job for this key. False if the daily quota is used up."""
        with self.lock:
            r = self.keys.get(rid) or self.static.get(rid)
            if not r:
                return True
            today = _today()
            if r.get("day") != today:
                r["day"], r["day_count"] = today, 0
            if daily and r["day_count"] >= daily:
                return False
            r["day_count"] += 1
            r["total"] = r.get("total", 0) + 1
            r["last_used"] = time.time()
            if not r.get("static"):
                self.dirty = True
            return True

    # ------------------------------------------------------------ admin ops
    def view(self):
        today, out = _today(), []
        for r in self._all():
            c = dict(r)
            if c.get("day") != today:
                c["day_count"] = 0
            out.append(c)
        out.sort(key=lambda r: (bool(r.get("static")), -(r.get("created") or 0)))
        return out

    @staticmethod
    def generate_key():
        return "u2s_" + secrets.token_urlsafe(24)

    def _valid_key(self, key, self_id=None):
        if not KEY_RE.match(key or ""):
            raise ValueError("custom key: 6-128 chars, letters/numbers and _ . - @ ! # $ % ^ & * + = ~ only")
        other = self.find(key)
        if other and other["id"] != self_id:
            raise ValueError("this key already exists")

    def create(self, name, key=None, rate_per_min=None, max_active=None, daily_limit=None,
               expires_days=None, note="", enabled=True):
        name = (name or "").strip()
        if not name or len(name) > 60:
            raise ValueError("name is required (max 60 chars)")
        with self.lock:
            if key:
                self._valid_key(key)
            else:
                key = self.generate_key()
                while self.find(key):
                    key = self.generate_key()
            days = _num(expires_days, "expires_days")
            rec = {"id": secrets.token_hex(4), "name": name, "key": key, "note": (note or "")[:200],
                   "enabled": bool(enabled), "static": False, "created": time.time(),
                   "expires": time.time() + days * 86400 if days else None,
                   "rate_per_min": _num(rate_per_min, "rate_per_min"), "max_active": _num(max_active, "max_active"),
                   "daily_limit": _num(daily_limit, "daily_limit"), "total": 0, "day": _today(),
                   "day_count": 0, "last_used": None}
            self.keys[rec["id"]] = rec
            self.save()
            return dict(rec)

    def _get(self, rid):
        if rid in self.static:
            raise ValueError("keys from the API_KEYS env var can only be changed in Render's Environment tab")
        if rid not in self.keys:
            raise KeyError("key not found")
        return self.keys[rid]

    def update(self, rid, **f):
        with self.lock:
            r = self._get(rid)
            if "name" in f:
                n = (f["name"] or "").strip()
                if not n or len(n) > 60:
                    raise ValueError("name is required (max 60 chars)")
                r["name"] = n
            if "note" in f:
                r["note"] = (f["note"] or "")[:200]
            if "enabled" in f:
                r["enabled"] = bool(f["enabled"])
            for fld in ("rate_per_min", "max_active", "daily_limit"):
                if fld in f:
                    r[fld] = _num(f[fld], fld)
            if "expires_days" in f:
                d = _num(f["expires_days"], "expires_days")
                r["expires"] = time.time() + d * 86400 if d else None
            if f.get("key"):
                self._valid_key(f["key"], rid)
                r["key"] = f["key"]
            self.save()
            return dict(r)

    def regen(self, rid):
        with self.lock:
            r = self._get(rid)
            k = self.generate_key()
            while self.find(k):
                k = self.generate_key()
            r["key"] = k
            self.save()
            return dict(r)

    def reset(self, rid):
        with self.lock:
            r = self._get(rid)
            r["total"], r["day_count"], r["last_used"] = 0, 0, None
            self.save()
            return dict(r)

    def delete(self, rid):
        with self.lock:
            self._get(rid)
            del self.keys[rid]
            self.save()

    def set_settings(self, **s):
        with self.lock:
            if "require_key" in s:
                self.settings["require_key"] = bool(s["require_key"])
            for k in ("public_rate_per_min", "public_max_active"):
                if k in s:
                    self.settings[k] = _num(s[k], k)
            self.save()
            return dict(self.settings)

    def export(self):
        with self.lock:
            return {"keys": list(self.keys.values()), "settings": dict(self.settings)}

    def import_(self, data, replace=False):
        with self.lock:
            if replace:
                self.keys = {}
            added = 0
            for r in data.get("keys", []):
                if not all(k in r for k in ("id", "name", "key")) or not KEY_RE.match(str(r["key"])):
                    continue
                if any(o["key"] == r["key"] and o["id"] != r["id"] for o in self._all()):
                    continue
                r["static"] = False
                self.keys[r["id"]] = r
                added += 1
            if isinstance(data.get("settings"), dict) and replace:
                self.settings.update(data["settings"])
            self.readonly = False
            self.save()
            return added
