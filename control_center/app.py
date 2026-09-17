from __future__ import annotations
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

class ControlCenter:
    def __init__(self, store, telemetry, token):
        self.store, self.telemetry, self.token = store, telemetry, token
    def authorized(self, header):
        supplied = (header or "").removeprefix("Bearer ")
        return bool(self.token) and hmac.compare_digest(supplied, self.token)
    def perform_action(self, job_id, action, data):
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("unsupported action")
        if action == "cancel" and data.get("confirm") is not True:
            raise PermissionError("confirmation_required")
        self.store.action(job_id, action)
        return {"status": action}
    def handler(self):
        app = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def send_json(self, status, payload):
                body = json.dumps(payload, default=str).encode()
                self.send_response(status); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    body = HTML.encode(); self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                if not app.authorized(self.headers.get("Authorization")):
                    return self.send_json(401, {"error": "unauthorized"})
                try:
                    if parsed.path == "/api/overview":
                        return self.send_json(200, {**app.store.overview(), "telemetry": app.telemetry.snapshot()})
                    if parsed.path == "/api/events":
                        return self.send_json(200, app.store.events({k:v[0] for k,v in parse_qs(parsed.query).items()}))
                    if parsed.path.startswith("/api/jobs/"):
                        return self.send_json(200, app.store.job(int(parsed.path.rsplit("/", 1)[1])))
                    return self.send_json(404, {"error": "not_found"})
                except (KeyError, ValueError):
                    return self.send_json(400, {"error": "invalid_request"})
            def do_POST(self):
                if not app.authorized(self.headers.get("Authorization")):
                    return self.send_json(401, {"error": "unauthorized"})
                try:
                    size = min(int(self.headers.get("Content-Length", "0")), 65536)
                    data = json.loads(self.rfile.read(size) or b"{}")
                    parts = urlparse(self.path).path.strip("/").split("/")
                    if parts == ["api", "jobs"]:
                        return self.send_json(201, {"job_id": app.store.create(data)})
                    if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                        try: result = app.perform_action(int(parts[2]), parts[3], data)
                        except PermissionError: return self.send_json(409, {"error": "confirmation_required"})
                        return self.send_json(200, result)
                    return self.send_json(404, {"error": "not_found"})
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self.send_json(400, {"error": "invalid_request"})
        return Handler
    def serve(self, host="127.0.0.1", port=8080):
        ThreadingHTTPServer((host, port), self.handler()).serve_forever()

HTML = '''<!doctype html><meta charset="utf-8"><title>Agent Control Center</title><style>
body{font:14px system-ui;background:#0b1020;color:#e8ecf4;margin:0}header{padding:20px;background:#151d35}main{padding:20px;max-width:1200px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}.card{background:#151d35;border:1px solid #2a3658;border-radius:10px;padding:14px}.flow{font-size:18px;margin:18px 0;color:#8bd5ff}button,input{padding:8px;background:#202c4b;color:white;border:1px solid #46577e;border-radius:5px}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #2a3658;text-align:left}.ok{color:#8ff0a4}</style>
<header><h1>Homelab Agent Control Center</h1><input id=t type=password placeholder="API token"><button onclick=load()>Connect</button></header><main><div class=flow>Planner → Coder → Reviewer ↩ Changes Requested → Verification → Commit → Planner</div><div id=cards class=grid></div><h2>Create job</h2><div class=card><input id=goal placeholder="Goal"><input id=repo placeholder="Repository"><input id=branch placeholder="Worker branch"><button onclick=createJob()>Create</button></div><h2>Jobs</h2><div class=card><table><thead><tr><th>ID</th><th>Goal</th><th>Status</th><th>Phase</th><th>Iteration</th><th>Controls</th></tr></thead><tbody id=jobs></tbody></table></div><h2>Recent durable events</h2><div id=events class=card></div></main><script>
let token='';async function request(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+token,'Content-Type':'application/json'};let r=await fetch(p,o);if(!r.ok)throw Error(r.status);return r.json()}async function load(){token=t.value;let o=await request('/api/overview');cards.innerHTML=`<div class=card><b>PostgreSQL</b><p class=ok>connected</p></div><div class=card><b>Ollama</b><pre>${JSON.stringify(o.telemetry.ollama,null,2)}</pre></div><div class=card><b>GPU</b><pre>${JSON.stringify(o.telemetry.gpu,null,2)}</pre></div><div class=card><b>Inference</b><pre>${JSON.stringify(o.inference,null,2)}</pre></div>`;jobs.innerHTML=o.jobs.map(j=>`<tr><td>${j.id}</td><td>${esc(j.goal)}</td><td>${j.status}</td><td>${j.current_phase||''}</td><td>${j.iteration_count}</td><td><button onclick=act(${j.id},'pause')>Pause</button> <button onclick=act(${j.id},'resume')>Resume</button> <button onclick=act(${j.id},'cancel')>Cancel</button></td></tr>`).join('');let e=await request('/api/events');events.innerHTML=e.slice(0,50).map(x=>`<p><b>${x.created_at}</b> ${esc(x.agent)}: ${esc(x.event_type)}</p>`).join('')}async function createJob(){await request('/api/jobs',{method:'POST',body:JSON.stringify({goal:goal.value,repository:repo.value,branch:branch.value})});load()}async function act(id,a){if(a==='cancel'&&!confirm('Cancel this job?'))return;await request(`/api/jobs/${id}/${a}`,{method:'POST',body:JSON.stringify({confirm:a==='cancel'})});load()}function esc(s){let d=document.createElement('div');d.textContent=s;return d.innerHTML}setInterval(()=>{if(token)load()},5000)</script>'''
