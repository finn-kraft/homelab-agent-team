from __future__ import annotations

import json
import logging
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

from .auth import (AuthManager, AuthenticationError, CSRF_HEADER, RateLimitError,
                   SESSION_COOKIE, WriteRateLimitError)


LOGGER = logging.getLogger(__name__)
WORKFLOW_ACTION_ERROR = (
    "The workflow database is not ready for this action. "
    "Run agent-orchestrator init-db, then restart the Control Center."
)


class OperationFailed(RuntimeError):
    """An operator action was accepted but could not be applied."""

    def __init__(self, operation_id: str, detail: str):
        super().__init__(detail)
        self.operation_id = operation_id
        self.detail = detail


class ControlCenter:
    def __init__(self, store, telemetry, auth: AuthManager):
        self.store, self.telemetry, self.auth = store, telemetry, auth

    def snapshot(self):
        overview = self.store.overview()
        telemetry = self.telemetry_snapshot(overview)
        return {**overview, "telemetry": telemetry,
                "alerts": self._telemetry_alerts(telemetry, overview)}

    def telemetry_snapshot(self, overview=None):
        telemetry = self.telemetry.snapshot()
        inference = (overview or {}).get("inference") if isinstance(overview, dict) else None
        if inference is None:
            getter = getattr(self.store, "inference_snapshot", None)
            inference = getter() if getter is not None else {}
        sample = {**telemetry, "inference": inference}
        record = getattr(self.store, "record_telemetry", None)
        if record is not None:
            try:
                record(sample)
            except Exception:
                LOGGER.debug("telemetry_persist_failed", exc_info=True)
        return telemetry

    @staticmethod
    def _telemetry_alerts(telemetry, overview):
        alerts = []
        gpu = telemetry.get("gpu", {}) if isinstance(telemetry, dict) else {}
        ollama = telemetry.get("ollama", {}) if isinstance(telemetry, dict) else {}
        router = telemetry.get("routing_agent", {}) if isinstance(telemetry, dict) else {}
        try:
            temperature = float(gpu.get("temperature_c"))
            if temperature >= 85:
                alerts.append({"severity": "critical" if temperature >= 90 else "warning",
                               "source": "gpu", "message": f"GPU temperature is {temperature:.0f}°C."})
        except (TypeError, ValueError):
            pass
        try:
            utilization = float(gpu.get("utilization_percent"))
            if utilization >= 98:
                alerts.append({"severity": "warning", "source": "gpu",
                               "message": "GPU utilization has been at capacity."})
        except (TypeError, ValueError):
            pass
        if ollama.get("status") not in {None, "online", "not_configured"}:
            alerts.append({"severity": "warning", "source": "ollama",
                           "message": "Ollama is unavailable; local inference may be delayed."})
        if router.get("status") == "offline":
            alerts.append({"severity": "warning", "source": "routing-agent",
                           "message": "Routing Agent is offline; fallback policy is active."})
        for worker in overview.get("workers", []):
            if worker.get("component") == "orchestrator" and worker.get("online") is False:
                alerts.append({"severity": "warning", "source": "orchestrator",
                               "message": "Orchestrator heartbeat is stale."})
        for work in overview.get("active_work", []):
            if work.get("stale_warning"):
                alerts.append({"severity": "warning", "source": "workflow",
                               "message": f"Step {work.get('step_id')} is stale: {work['stale_warning']}"})
        return alerts

    def perform_action(self, job_id, action, data):
        if action not in {"pause", "resume", "cancel", "remove", "retry",
                          "recover-lease", "rerun-verification", "refresh-planning"}:
            raise ValueError("unsupported action")
        if action in {"cancel", "remove"} and data.get("confirm") is not True:
            raise PermissionError("confirmation_required")

        # Older test doubles and pre-0011 databases do not expose operation
        # tracking. Keep their compact response contract while the production
        # store gets a durable submitted -> accepted -> applied record.
        begin = getattr(self.store, "begin_operation", None)
        if begin is None:
            if action == "remove":
                self.store.remove_job(job_id)
                return {"status": "removed"}
            if action in {"pause", "resume", "cancel"}:
                self.store.action(job_id, action)
                return {"status": action}
            method = {
                "retry": "retry", "recover-lease": "recover_lease",
                "rerun-verification": "rerun_verification",
                "refresh-planning": "refresh_planning",
            }[action]
            getattr(self.store, method)(job_id, data.get("step_id")) if action == "rerun-verification" else getattr(self.store, method)(job_id)
            return {"status": action}

        operation_id = begin(job_id, action, data.get("requested_by", "control-center"))
        update = getattr(self.store, "update_operation", None)
        try:
            if update:
                update(operation_id, "accepted")
            if action == "remove":
                self.store.remove_job(job_id)
                result = {"status": "removed"}
            elif action in {"pause", "resume", "cancel"}:
                self.store.action(job_id, action)
                result = {"status": action}
            else:
                method = {
                    "retry": "retry", "recover-lease": "recover_lease",
                    "rerun-verification": "rerun_verification",
                    "refresh-planning": "refresh_planning",
                }[action]
                if action == "rerun-verification":
                    detail = getattr(self.store, method)(job_id, data.get("step_id"))
                else:
                    detail = getattr(self.store, method)(job_id)
                result = {"status": action, "detail": detail}
            if update:
                update(operation_id, "applied", detail=result)
            return {**result, "operation_id": operation_id, "operation_status": "applied"}
        except Exception as exc:
            if update:
                try:
                    update(operation_id, "failed", error=str(exc))
                except Exception:
                    LOGGER.exception("control_operation_record_failed operation_id=%s", operation_id)
            raise OperationFailed(operation_id, str(exc)) from exc

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
                if app.auth.secure_cookie:
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
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
                if not expires:
                    value += f"; Max-Age={max(60, int(getattr(app.auth, 'session_ttl', 43200)))}"
                if app.auth.secure_cookie:
                    value += "; Secure"
                if expires:
                    value += "; Max-Age=0"
                return value

            def set_session_cookie(self, token, *, expires=False):
                self.send_header("Set-Cookie", self.session_cookie_header(token, expires=expires))

            def session(self):
                token = app._cookie_token(self.headers.get("Cookie"))
                try:
                    return app.auth.authenticate(token, self.client_address[0])
                except TypeError:
                    # Compatibility with small test doubles and older auth
                    # implementations that do not bind sessions to a client.
                    return app.auth.authenticate(token)

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

            def require_write(self, session):
                check = getattr(app.auth, "check_write", None)
                if check is None:
                    return True
                try:
                    check(session)
                except WriteRateLimitError as exc:
                    self.send_json(429, {"error": "write_rate_limited"},
                                   {"Retry-After": str(exc.retry_after)})
                    return False
                except AuthenticationError:
                    self.send_json(403, {"error": "read_only_session"})
                    return False
                return True

            def send_stream(self, session):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    last_id = max(0, int(self.headers.get("Last-Event-ID", "0")))
                except ValueError:
                    last_id = 0
                events_since = getattr(app.store, "events_since", None)
                for _ in range(12):
                    if not app.auth.authenticate(session.token):
                        break
                    try:
                        snapshot = app.snapshot()
                        cursor = int(snapshot.get("event_cursor", 0) or 0)
                        if events_since is not None and last_id and cursor > last_id:
                            for event in events_since(last_id):
                                event_id = int(event.get("id", 0))
                                payload = json.dumps(event, default=str)
                                self.wfile.write(
                                    f"id: {event_id}\nevent: workflow\ndata: {payload}\n\n".encode()
                                )
                                last_id = max(last_id, event_id)
                        payload = json.dumps(snapshot, default=str)
                        self.wfile.write(f"id: {cursor}\nevent: snapshot\ndata: {payload}\n\n".encode())
                        self.wfile.flush()
                        last_id = max(last_id, cursor)
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
                    health = app.store.health()
                    healthy = bool(health.get("ready"))
                    return self.send_json(200 if healthy else 503,
                                          {"status": "ok" if healthy else "degraded",
                                           "health": health})
                session = self.require_session()
                if not session:
                    return
                try:
                    if parsed.path == "/api/session":
                        return self.send_json(200, {"authenticated": True, "csrf_token": session.csrf_token,
                                                    **getattr(app.auth, "session_info", lambda value: {})(session)})
                    if parsed.path == "/api/overview":
                        return self.send_json(200, app.snapshot())
                    if parsed.path == "/api/telemetry":
                        return self.send_json(200, app.telemetry_snapshot())
                    if parsed.path == "/api/telemetry/history":
                        query = parse_qs(parsed.query)
                        hours = int(query.get("hours", [24])[0])
                        limit = int(query.get("limit", [1440])[0])
                        return self.send_json(200, app.store.telemetry_history(hours, limit))
                    if parsed.path == "/api/security/audit":
                        return self.send_json(200, app.store.secret_redaction_audit())
                    if parsed.path == "/api/projects":
                        return self.send_json(200, app.store.project_list())
                    if parsed.path == "/api/missions":
                        return self.send_json(200, app.store.missions())
                    if parsed.path == "/api/work-packages":
                        mission_id = parse_qs(parsed.query).get("mission_id", [None])[0]
                        return self.send_json(200, app.store.work_packages(mission_id))
                    if parsed.path == "/api/human-queue":
                        status = parse_qs(parsed.query).get("status", ["open"])[0]
                        return self.send_json(200, app.store.human_queue(status))
                    if parsed.path == "/api/operations":
                        job_id = parse_qs(parsed.query).get("job_id", [None])[0]
                        return self.send_json(200, app.store.operations(int(job_id) if job_id else None))
                    if parsed.path.startswith("/api/operations/"):
                        operation_id = parsed.path.rsplit("/", 1)[1]
                        result = app.store.operation(operation_id)
                        if result is None:
                            raise KeyError(operation_id)
                        return self.send_json(200, result)
                    if parsed.path.startswith("/api/missions/"):
                        return self.send_json(200, app.store.mission(int(parsed.path.rsplit("/", 1)[1])))
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
                except Exception:
                    LOGGER.exception("control_center_read_failed path=%s", parsed.path)
                    return self.send_json(503, {
                        "error": "workflow_unavailable",
                        "detail": WORKFLOW_ACTION_ERROR,
                    })

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
                if not self.require_write(session):
                    return
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
                    if len(parts) == 4 and parts[:2] == ["api", "human-queue"] and parts[3] == "answer":
                        app.store.answer_human_request(int(parts[2]), data["answer"])
                        return self.send_json(200, {"status": "answered"})
                    if parts == ["api", "worktrees", "cleanup"]:
                        if data.get("confirm") is not True:
                            return self.send_json(409, {"error": "confirmation_required"})
                        begin = getattr(app.store, "begin_operation", None)
                        if begin is None:
                            return self.send_json(200, {"status": "cleaned", "detail": app.store.cleanup_worktrees()})
                        operation_id = begin(None, "cleanup-worktrees", data.get("requested_by", "control-center"))
                        update = getattr(app.store, "update_operation", None)
                        try:
                            if update:
                                update(operation_id, "accepted")
                            detail = app.store.cleanup_worktrees()
                            result = {"status": "cleaned", "detail": detail}
                            if update:
                                update(operation_id, "applied", detail=result)
                            return self.send_json(200, {**result, "operation_id": operation_id,
                                                        "operation_status": "applied"})
                        except Exception as exc:
                            if update:
                                update(operation_id, "failed", error=str(exc))
                            return self.send_json(503, {"error": "workflow_action_failed",
                                                        "operation_id": operation_id,
                                                        "operation_status": "failed", "detail": str(exc)})
                    return self.send_json(404, {"error": "not_found"})
                except (KeyError, ValueError, json.JSONDecodeError):
                    return self.send_json(400, {"error": "invalid_request"})
                except OperationFailed as exc:
                    LOGGER.exception("control_operation_failed operation_id=%s", exc.operation_id)
                    return self.send_json(503, {
                        "error": "workflow_action_failed", "operation_id": exc.operation_id,
                        "operation_status": "failed", "detail": exc.detail,
                    })
                except Exception:
                    LOGGER.exception("control_center_write_failed path=%s", parsed.path)
                    return self.send_json(503, {
                        "error": "workflow_action_failed",
                        "detail": WORKFLOW_ACTION_ERROR,
                    })

        return Handler

    def serve(self, host="127.0.0.1", port=8080):
        ThreadingHTTPServer((host, port), self.handler()).serve_forever()
