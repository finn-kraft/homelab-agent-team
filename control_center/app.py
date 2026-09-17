from __future__ import annotations
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
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
    def snapshot(self):
        return {**self.store.overview(), "telemetry": self.telemetry.snapshot()}
    def handler(self):
        app = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def end_headers(self):
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                )
                super().end_headers()
            def send_json(self, status, payload):
                body = json.dumps(payload, default=str).encode()
                self.send_response(status); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body)
            def send_asset(self, name, content_type):
                body = files("control_center.static").joinpath(name).read_bytes()
                self.send_response(200); self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers(); self.wfile.write(body)
            def send_stream(self):
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "keep-alive")
                self.end_headers()
                for _ in range(12):
                    try:
                        payload = json.dumps(app.snapshot(), default=str)
                        self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode()); self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError): break
                    time.sleep(5)
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    return self.send_asset("index.html", "text/html; charset=utf-8")
                if parsed.path == "/app.js": return self.send_asset("app.js", "text/javascript; charset=utf-8")
                if parsed.path == "/styles.css": return self.send_asset("styles.css", "text/css; charset=utf-8")
                if parsed.path == "/health":
                    healthy = app.store.healthy()
                    return self.send_json(200 if healthy else 503, {"status":"ok" if healthy else "degraded"})
                if not app.authorized(self.headers.get("Authorization")):
                    return self.send_json(401, {"error": "unauthorized"})
                try:
                    if parsed.path == "/api/overview": return self.send_json(200, app.snapshot())
                    if parsed.path == "/api/projects": return self.send_json(200, app.store.project_list())
                    if parsed.path == "/api/stream": return self.send_stream()
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
                        if parts[3] == "answer":
                            app.store.answer(int(parts[2]), data["answer"])
                            return self.send_json(200, {"status":"resumed"})
                        try: result = app.perform_action(int(parts[2]), parts[3], data)
                        except PermissionError: return self.send_json(409, {"error": "confirmation_required"})
                        return self.send_json(200, result)
                    return self.send_json(404, {"error": "not_found"})
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self.send_json(400, {"error": "invalid_request"})
        return Handler
    def serve(self, host="127.0.0.1", port=8080):
        ThreadingHTTPServer((host, port), self.handler()).serve_forever()
