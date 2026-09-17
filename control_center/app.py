from __future__ import annotations

import json
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

from .auth import AuthManager, AuthenticationError, CSRF_HEADER, RateLimitError, SESSION_COOKIE


class ControlCenter:
    def __init__(self, store, telemetry, auth: AuthManager):
        self.store, self.telemetry, self.auth = store, telemetry, auth

    def snapshot(self):
        return {**self.store.overview(), "telemetry": self.telemetry.snapshot()}

    def perform_action(self, job_id, action, data):
        if action not in {"pause", "resume", "cancel", "remove"}:
            raise ValueError("unsupported action")
        if action in {"cancel", "remove"} and data.get("confirm") is not True:
            raise PermissionError("confirmation_required")
        if action == "remove":
            self.store.remove_queued_job(job_id)
            return {"status": "removed"}
        self.store.action(job_id, action)
        return {"status": action}

    @staticmethod
    def _cookie_token(header: str | None) -> str | None:
        if not header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

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

            def send_json(self, status, payload, headers=None):
                body = json.dumps(payload, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def send_asset(self, name, content_type):
                body = files("control_center.static").joinpath(name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

            def session_cookie_header(self, token, *, expires=False):
                value = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
                if app.auth.secure_cookie:
                    value += "; Secure"
                if expires:
                    value += "; Max-Age=0"
                return value

            def set_session_cookie(self, token, *, expires=False):
                self.send_header("Set-Cookie", self.session_cookie_header(token, expires=expires))

            def session(self):
                return app.auth.authenticate(app._cookie_token(self.headers.get("Cookie")))

            def require_session(self):
                value = self.session()
                if not value:
                    self.send_json(401, {"error": "unauthorized"})
                return value

            def require_csrf(self, session):
                if not app.auth.check_csrf(session, self.headers.get(CSRF_HEADER)):
                    self.send_json(403, {"error": "csrf_validation_failed"})
                    return False
                return True

            def send_stream(self, session):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for _ in range(12):
                    if not app.auth.authenticate(session.token):
                        break
                    try:
                        payload = json.dumps(app.snapshot(), default=str)
                        self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    time.sleep(5)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    return self.send_asset("index.html", "text/html; charset=utf-8")
                if parsed.path == "/app.js":
                    return self.send_asset("app.js", "text/javascript; charset=utf-8")
                if parsed.path == "/styles.css":
                    return self.send_asset("styles.css", "text/css; charset=utf-8")
                if parsed.path == "/health":
                    healthy = app.store.healthy()
                    return self.send_json(200 if healthy else 503,
                                          {"status": "ok" if healthy else "degraded"})
                session = self.require_session()
                if not session:
                    return
                try:
                    if parsed.path == "/api/session":
                        return self.send_json(200, {"authenticated": True, "csrf_token": session.csrf_token})
                    if parsed.path == "/api/overview":
                        return self.send_json(200, app.snapshot())
                    if parsed.path == "/api/projects":
                        return self.send_json(200, app.store.project_list())
                    if parsed.path == "/api/stream":
                        return self.send_stream(session)
                    if parsed.path == "/api/events":
                        return self.send_json(200, app.store.events({
                            key: value[0] for key, value in parse_qs(parsed.query).items()
                        }))
                    if parsed.path.startswith("/api/jobs/"):
                        return self.send_json(200, app.store.job(int(parsed.path.rsplit("/", 1)[1])))
                    return self.send_json(404, {"error": "not_found"})
                except (KeyError, ValueError):
                    return self.send_json(400, {"error": "invalid_request"})

            def body(self):
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 65536:
                    raise ValueError("request_body_too_large")
                return json.loads(self.rfile.read(size) or b"{}")

            def do_POST(self):
                parsed = urlparse(self.path)
                try:
                    data = self.body()
                except (ValueError, json.JSONDecodeError):
                    return self.send_json(400, {"error": "invalid_request"})
                if parsed.path == "/api/login":
                    try:
                        session = app.auth.login(data.get("password", ""), self.client_address[0])
                    except RateLimitError as exc:
                        return self.send_json(429, {"error": "too_many_login_attempts"},
                                              {"Retry-After": str(exc.retry_after)})
                    except AuthenticationError:
                        return self.send_json(401, {"error": "invalid_credentials"})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.set_session_cookie(session.token)
                    body = json.dumps({"authenticated": True, "csrf_token": session.csrf_token}).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                session = self.require_session()
                if not session or not self.require_csrf(session):
                    return
                if parsed.path == "/api/logout":
                    app.auth.logout(session.token)
                    return self.send_json(200, {"status": "logged_out"},
                                          {"Set-Cookie": self.session_cookie_header("", expires=True)})
                try:
                    parts = parsed.path.strip("/").split("/")
                    if parts == ["api", "jobs"]:
                        return self.send_json(201, {"job_id": app.store.create(data)})
                    if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                        if parts[3] == "answer":
                            app.store.answer(int(parts[2]), data["answer"])
                            return self.send_json(200, {"status": "resumed"})
                        try:
                            result = app.perform_action(int(parts[2]), parts[3], data)
                        except PermissionError:
                            return self.send_json(409, {"error": "confirmation_required"})
                        return self.send_json(200, result)
                    return self.send_json(404, {"error": "not_found"})
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self.send_json(400, {"error": "invalid_request"})

        return Handler

    def serve(self, host="127.0.0.1", port=8080):
        ThreadingHTTPServer((host, port), self.handler()).serve_forever()
