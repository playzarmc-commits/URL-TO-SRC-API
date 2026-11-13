import hmac
import time

from flask import Blueprint, Response, jsonify, request


def register_admin(app, store, admin_key, get_jobs, get_qsize, workers):
    bp = Blueprint("admin", __name__)
    fails = {}  # ip -> [count, blocked_until]

    def ip():
        xff = request.headers.get("X-Forwarded-For", "")
        return xff.split(",")[0].strip() or request.remote_addr or "?"

    @bp.before_request
    def auth():
        if request.method == "OPTIONS" or not request.path.startswith("/admin/api/"):
            return None
        a = ip()
        f = fails.get(a, [0, 0])
        if f[1] > time.time():
            return jsonify(error="too many wrong attempts, wait 5 minutes"), 429
        k = request.headers.get("x-admin-key") or request.args.get("admin") or ""
        if k and hmac.compare_digest(k.encode("utf-8", "ignore"), admin_key.encode("utf-8")):
            fails.pop(a, None)
            return None
        if k:
            f[0] += 1
            if f[0] >= 5:
                f[0], f[1] = 0, time.time() + 300
            fails[a] = f
        return jsonify(error="unauthorized"), 401

    def body():
        return request.get_json(silent=True) or {}

    def guarded(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except ValueError as e:
                return jsonify(error=str(e)), 400
            except KeyError as e:
                return jsonify(error=str(e).strip("'\"")), 404
        wrapper.__name__ = fn.__name__
        return wrapper

    def check_not_admin(key):
        if key and hmac.compare_digest(key.encode(), admin_key.encode()):
            raise ValueError("this key cannot equal the admin key")

    @bp.get("/admin")
    def panel():
        return Response(PANEL, mimetype="text/html")

    @bp.get("/admin/api/keys")
    def keys():
        return jsonify(keys=store.view())

    @bp.post("/admin/api/keys")
    @guarded
    def create():
        b = body()
        check_not_admin(b.get("key"))
        if b.get("type", "signed") == "signed" and not b.get("key"):
            r = store.create_signed(b.get("name"), b.get("rate_per_min"), b.get("max_active"),
                                    b.get("daily_limit"), b.get("expires_days"), b.get("note", ""))
        else:
            r = store.create(b.get("name"), b.get("key") or None, b.get("rate_per_min"), b.get("max_active"),
                             b.get("daily_limit"), b.get("expires_days"), b.get("note", ""), b.get("enabled", True))
        return jsonify(r), 201

    @bp.get("/admin/api/generate")
    @guarded
    def generate():
        """Quick generator for curl/browser: /admin/api/generate?name=botA&rate=30&days=30&admin=..."""
        a = request.args
        check_not_admin(a.get("key"))
        if a.get("type", "signed") == "signed" and not a.get("key"):
            r = store.create_signed(a.get("name") or "bot", a.get("rate"), a.get("active"), a.get("daily"),
                                    a.get("days"), a.get("note", ""))
        else:
            r = store.create(a.get("name") or "bot", a.get("key") or None, a.get("rate"), a.get("active"),
                             a.get("daily"), a.get("days"), a.get("note", ""))
        return jsonify(r), 201

    @bp.patch("/admin/api/keys/<rid>")
    @guarded
    def update(rid):
        b = body()
        check_not_admin(b.get("key"))
        allowed = ("name", "note", "enabled", "rate_per_min", "max_active", "daily_limit", "expires_days", "key")
        return jsonify(store.update(rid, **{k: v for k, v in b.items() if k in allowed}))

    @bp.post("/admin/api/revoke")
    @guarded
    def revoke():
        store.revoke_token(body().get("key"))
        return jsonify(ok=True)

    @bp.post("/admin/api/keys/<rid>/regen")
    @guarded
    def regen(rid):
        return jsonify(store.regen(rid))

    @bp.post("/admin/api/keys/<rid>/reset")
    @guarded
    def reset(rid):
        return jsonify(store.reset(rid))

    @bp.delete("/admin/api/keys/<rid>")
    @guarded
    def delete(rid):
        store.delete(rid)
        return jsonify(ok=True)

    @bp.route("/admin/api/settings", methods=["GET", "POST"])
    @guarded
    def settings():
        if request.method == "POST":
            return jsonify(store.set_settings(**body()))
        return jsonify(dict(store.settings, env_keys_force_auth=bool(store.static)))

    @bp.get("/admin/api/stats")
    def stats():
        jobs = list(get_jobs().values())
        return jsonify(workers=workers, queued=get_qsize(), jobs=len(jobs),
                       running=sum(j.status == "running" for j in jobs),
                       keys=len(store.keys) + len(store.static), revoked=len(store.revoked), require_key=store.require_key(),
                       storage="postgres" if store.db_url else "file", readonly=store.readonly)

    @bp.get("/admin/api/jobs")
    def jobs():
        js = sorted(get_jobs().values(), key=lambda j: -j.created)[:40]
        return jsonify(jobs=[{"id": j.id, "key": store.name_of(j.bot), "url": j.opts["url"], "status": j.status,
                              "created": j.created, "error": j.error,
                              "total_ms": (j.result or {}).get("timings", {}).get("total_ms"),
                              "loader_ms": (j.result or {}).get("timings", {}).get("loading_screen_total_ms")}
                             for j in js])

    @bp.get("/admin/api/export")
    def export():
        return jsonify(store.export())

    @bp.post("/admin/api/import")
    @guarded
    def import_():
        b = body()
        return jsonify(imported=store.import_(b.get("data") or {}, bool(b.get("replace"))))

    app.register_blueprint(bp)


PANEL = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>url2src admin</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--mut:#667;--card:#f3f4f8;--bd:#d6d9e2;--ac:#4f46e5;--ok:#16a34a;--bad:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#eceef3;--mut:#9aa1b2;--card:#181b22;--bd:#2a2f3a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui}
main{max-width:820px;margin:0 auto;padding:12px}h2{margin:18px 0 8px;font-size:16px}
input,button,select{font:inherit;padding:9px;border:1px solid var(--bd);border-radius:8px;background:var(--card);color:var(--fg)}
input{width:100%;margin:3px 0}button{cursor:pointer}button.pri{background:var(--ac);color:#fff;border-color:var(--ac)}
button.danger{color:var(--bad)}.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px;margin:8px 0}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.mut{color:var(--mut);font-size:12px;margin:3px 0}.key{display:block;word-break:break-all;margin:6px 0;font-size:13px}
.btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.btns button{padding:6px 10px;font-size:12px}
.tag{font-size:11px;padding:2px 7px;border-radius:99px;border:1px solid var(--bd)}.tag.ok{color:var(--ok)}.tag.bad,.bad{color:var(--bad)}
#msg{position:sticky;top:0;padding:8px;border-radius:8px;display:none;margin:6px 0}
</style></head><body><main>
<div id=login><h2>url2src admin</h2><input id=ak type=password placeholder="Admin key"><button class=pri onclick="doLogin()" style="width:100%;margin-top:6px">Login</button></div>
<div id=app style="display:none">
<div class=row><h2 style="flex:1">url2src admin</h2><button onclick="logout()">Logout</button></div>
<div id=msg></div><div id=stats class=mut></div>

<h2 id=formtitle>Generate key</h2><div class=card>
<input id=f_name placeholder="Bot / key name (required)">
<select id=f_type style="width:100%;margin:3px 0"><option value=signed>Signed key: survives restarts, no database needed (recommended)</option><option value=stored>Stored key: editable, needs persistent storage</option></select>
<input id=f_key placeholder="Custom key (only for Stored type; empty = auto generate)">
<div class=grid><input id=f_rate type=number placeholder="Requests / min (default)"><input id=f_active type=number placeholder="Max active jobs (default)">
<input id=f_daily type=number placeholder="Daily limit (unlimited)"><input id=f_days type=number placeholder="Expires in days (never)"></div>
<input id=f_note placeholder="Note (optional)">
<div class=btns><button class=pri id=f_btn onclick="submitForm()">Generate key</button><button id=f_cancel onclick="resetForm()" style="display:none">Cancel</button></div></div>

<h2>Keys</h2><div id=keys></div>

<h2>Settings</h2><div class=card>
<label><input id=s_req type=checkbox style="width:auto"> Require API key (block requests without a key)</label>
<div class=grid><input id=s_rate type=number placeholder="Keyless rate / min"><input id=s_act type=number placeholder="Keyless max active"></div>
<div class=mut id=s_note></div><div class=btns><button class=pri onclick="saveSettings()">Save settings</button></div></div>

<h2>Backup</h2><div class=card><div class=mut>Free Render plans lose files on restart. Export keys regularly, or set DATABASE_URL / a Render Disk.</div>
<div class=btns><button onclick="exportKeys()">Export keys</button><button onclick="document.getElementById('imp').click()">Import keys</button></div>
<input id=imp type=file accept=application/json style="display:none" onchange="importKeys(this)"></div>

<h2>Revoke a signed key</h2><div class=card><input id=rv_key placeholder="Paste a signed key (sk_...)"><div class=btns><button class=danger onclick="revokeKey()">Revoke</button></div></div>

<h2>Recent jobs</h2><div id=jobs></div>
</div>
<script>
const $=id=>document.getElementById(id);
let AK=localStorage.ak||'',KEYS=[],SHOW=new Set(),EDIT=null;
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dt=t=>t?new Date(t*1000).toLocaleString():'never';
function msg(t,bad){const m=$('msg');m.textContent=t;m.style.display='block';m.style.background=bad?'#dc262622':'#16a34a22';setTimeout(()=>m.style.display='none',4000)}
async function api(p,m,b){const r=await fetch('/admin/api'+p,{method:m||'GET',headers:{'x-admin-key':AK,'content-type':'application/json'},body:b?JSON.stringify(b):undefined});
 const j=await r.json().catch(()=>({}));if(r.status===401){logout(true);throw new Error('unauthorized')}if(!r.ok)throw new Error(j.error||r.status);return j}
async function doLogin(){AK=$('ak').value;try{await api('/stats');localStorage.ak=AK;show()}catch(e){alert(e.message)}}
function logout(silent){localStorage.removeItem('ak');AK='';$('app').style.display='none';$('login').style.display='block';if(silent!==true)$('ak').value=''}
function show(){$('login').style.display='none';$('app').style.display='block';load()}
async function load(){try{
 const [k,s,st,jb]=await Promise.all([api('/keys'),api('/settings'),api('/stats'),api('/jobs')]);
 KEYS=k.keys;renderKeys();
 $('stats').textContent='workers '+st.workers+' | queued '+st.queued+' | running '+st.running+' | jobs '+st.jobs+' | keys '+st.keys+' | storage '+st.storage+(st.readonly?' (READ-ONLY!)':'')+' | revoked '+st.revoked;autoBackup(st);
 $('s_req').checked=!!s.require_key;$('s_req').disabled=!!s.env_keys_force_auth;$('s_rate').value=s.public_rate_per_min||'';$('s_act').value=s.public_max_active||'';
 $('s_note').textContent=s.env_keys_force_auth?'API_KEYS env is set, so a key is always required.':'';
 $('jobs').innerHTML=jb.jobs.map(j=>'<div class=card><div class=row><b>'+esc(j.key)+'</b><span class=tag>'+esc(j.status)+'</span><span class=mut>'+dt(j.created)+'</span></div><div class=mut style="word-break:break-all">'+esc(j.url)+'</div><div class=mut>'+(j.total_ms!=null?'wait '+j.total_ms+'ms | loader '+j.loader_ms+'ms':esc(j.error||''))+'</div></div>').join('')||'<div class=mut>No jobs yet</div>';
 }catch(e){if(e.message!=='unauthorized')msg(e.message,1)}}
function card(k){
 const exp=k.expires?(k.expires*1000<Date.now()?'<b class=bad>expired</b>':'expires '+new Date(k.expires*1000).toLocaleDateString()):'no expiry';
 const sh=SHOW.has(k.id),kt=sh?k.key:k.key.slice(0,6)+'......'+k.key.slice(-3);
 const own=k.static?'':k.signed?'<button onclick="resetU(\\''+k.id+'\\')">Reset usage</button><button class=danger onclick="del(\\''+k.id+'\\')">Revoke</button>':'<button onclick="toggle(\\''+k.id+'\\','+(!k.enabled)+')">'+(k.enabled?'Disable':'Enable')+'</button><button onclick="edit(\\''+k.id+'\\')">Edit</button><button onclick="regen(\\''+k.id+'\\')">New key</button><button onclick="resetU(\\''+k.id+'\\')">Reset usage</button><button class=danger onclick="del(\\''+k.id+'\\')">Delete</button>';
 return '<div class=card><div class=row><b>'+esc(k.name)+'</b><span class="tag '+(k.enabled?'ok':'bad')+'">'+(k.enabled?'ON':'OFF')+'</span>'+(k.static?'<span class=tag>env</span>':k.signed?'<span class=tag>signed</span>':'<span class=tag>stored</span>')+'</div>'
 +'<code class=key>'+esc(kt)+'</code>'
 +'<div class=mut>today '+(k.day_count||0)+(k.daily_limit?'/'+k.daily_limit:'')+' | total '+(k.total||0)+' | '+(k.rate_per_min||'default')+'/min | active '+(k.max_active||'default')+' | '+exp+'</div>'
 +'<div class=mut>last used: '+dt(k.last_used)+(k.note?' | '+esc(k.note):'')+'</div>'
 +'<div class=btns><button onclick="reveal(\\''+k.id+'\\')">'+(sh?'Hide':'Show')+'</button><button onclick="copyKey(\\''+k.id+'\\')">Copy</button>'+own+'</div></div>'}
function renderKeys(){$('keys').innerHTML=KEYS.map(card).join('')||'<div class=mut>No keys yet. Generate one above.</div>'}
function find(id){return KEYS.find(k=>k.id===id)}
function reveal(id){SHOW.has(id)?SHOW.delete(id):SHOW.add(id);renderKeys()}
async function copyKey(id){try{await navigator.clipboard.writeText(find(id).key);msg('Key copied')}catch(e){prompt('Copy key:',find(id).key)}}
async function toggle(id,v){await act(()=>api('/keys/'+id,'PATCH',{enabled:v}))}
async function regen(id){if(confirm('Old key will stop working. Continue?'))await act(()=>api('/keys/'+id+'/regen','POST'),'New key generated')}
async function resetU(id){await act(()=>api('/keys/'+id+'/reset','POST'),'Usage reset')}
async function del(id){if(confirm('Delete this key permanently?'))await act(()=>api('/keys/'+id,'DELETE'),'Deleted')}
async function act(fn,ok){try{await fn();if(ok)msg(ok);load()}catch(e){msg(e.message,1)}}
function val(id){const v=$(id).value.trim();return v===''?null:v}
function body(){return{name:$('f_name').value.trim(),type:$('f_type').value,key:val('f_key'),rate_per_min:val('f_rate'),max_active:val('f_active'),daily_limit:val('f_daily'),expires_days:val('f_days'),note:$('f_note').value.trim()}}
async function submitForm(){const b=body();try{
 if(EDIT){delete b.key;await api('/keys/'+EDIT,'PATCH',b);msg('Updated')}
 else{const r=await api('/keys','POST',b);SHOW.add(r.id);msg('Key created: '+r.key)}
 resetForm();load()}catch(e){msg(e.message,1)}}
function edit(id){const k=find(id);EDIT=id;$('formtitle').textContent='Edit key';$('f_btn').textContent='Update key';$('f_cancel').style.display='inline-block';
 $('f_name').value=k.name;$('f_key').value='';$('f_key').disabled=true;$('f_key').placeholder='(use "New key" to change the key)';
 $('f_rate').value=k.rate_per_min||'';$('f_active').value=k.max_active||'';$('f_daily').value=k.daily_limit||'';
 $('f_days').value=k.expires?Math.max(1,Math.ceil((k.expires*1000-Date.now())/86400000)):'';$('f_note').value=k.note||'';window.scrollTo(0,0)}
function resetForm(){EDIT=null;$('formtitle').textContent='Generate key';$('f_btn').textContent='Generate key';$('f_cancel').style.display='none';
 ['f_name','f_key','f_rate','f_active','f_daily','f_days','f_note'].forEach(i=>$(i).value='');$('f_key').disabled=false;$('f_key').placeholder='Custom key (leave empty = auto generate)'}
async function saveSettings(){await act(()=>api('/settings','POST',{require_key:$('s_req').checked,public_rate_per_min:val('s_rate'),public_max_active:val('s_act')}),'Settings saved')}
async function revokeKey(){const k=$('rv_key').value.trim();if(!k)return;await act(()=>api('/revoke','POST',{key:k}),'Key revoked');$('rv_key').value=''}
async function autoBackup(st){try{const cur=await api('/export');const old=JSON.parse(localStorage.u2s_backup||'{}');
 if(st.keys===0&&(old.keys||[]).length&&confirm('Server has no keys (restart?). Restore '+old.keys.length+' keys from this browser backup?')){await api('/import','POST',{data:old,replace:false});return load()}
 if((old.revoked||[]).length){await api('/import','POST',{data:{revoked:old.revoked,keys:[]},replace:false})}
 const now=await api('/export');localStorage.u2s_backup=JSON.stringify(now)}catch(e){}}
async function exportKeys(){try{const d=await api('/export');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(d,null,1)],{type:'application/json'}));a.download='url2src-keys.json';a.click()}catch(e){msg(e.message,1)}}
async function importKeys(inp){const f=inp.files[0];if(!f)return;try{const data=JSON.parse(await f.text());const r=await api('/import','POST',{data:data,replace:confirm('OK = replace all keys, Cancel = merge')});msg('Imported '+r.imported+' keys');load()}catch(e){msg(e.message,1)}inp.value=''}
$('ak').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
if(AK){api('/stats').then(show).catch(()=>{})}
</script></main></body></html>"""
