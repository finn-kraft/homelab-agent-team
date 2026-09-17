"""Password authentication and short-lived opaque browser sessions."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from contextlib import contextmanager

import bcrypt


SESSION_COOKIE = "control_center_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_IDLE_TTL_SECONDS = 6 * 60 * 60
WRITE_RATE_LIMIT = 120
CSRF_HEADER = "X-CSRF-Token"


class AuthenticationError(ValueError):
    """Raised when credentials or a session are not valid."""


class RateLimitError(AuthenticationError):
    """Raised after too many failed login attempts from one client."""

    def __init__(self, retry_after: int):
        super().__init__("too_many_login_attempts")
        self.retry_after = retry_after


class WriteRateLimitError(RateLimitError):
    """Raised when one authenticated session sends too many writes."""


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    csrf_token: str
    expires_at: float
    created_at: float = 0.0
    last_seen: float = field(default=0.0, compare=False)
    client_key: str = ""
    role: str = "operator"


class AuthManager:
    """Store only a bcrypt password hash in PostgreSQL; keep opaque sessions in memory."""

    def __init__(self, database_url: str, *, secure_cookie: bool = False,
                 session_ttl: int = SESSION_TTL_SECONDS,
                 session_idle_ttl: int = SESSION_IDLE_TTL_SECONDS,
                 write_rate_limit: int = WRITE_RATE_LIMIT,
                 role: str = "operator"):
        self.database_url = database_url
        self.secure_cookie = secure_cookie
        self.session_ttl = max(60, int(session_ttl))
        self.session_idle_ttl = max(60, int(session_idle_ttl))
        self.write_rate_limit = max(0, int(write_rate_limit))
        self.role = str(role or "operator").lower()
        if self.role not in {"admin", "operator", "viewer"}:
            raise ValueError("role must be admin, operator, or viewer")
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._writes: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    @contextmanager
    def connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise RuntimeError("install the project to enable password authentication") from exc
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def ensure_schema(self) -> None:
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS control_center_credentials (
                id SMALLINT PRIMARY KEY CHECK (id = 1), password_hash TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")

    def set_password(self, password: str) -> None:
        password = str(password)
        if len(password) < 8 or len(password) > 256:
            raise ValueError("password must be 8-256 characters")
        self.ensure_schema()
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
        with self.connect() as connection:
            connection.execute("""INSERT INTO control_center_credentials(id,password_hash)
                VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET password_hash=EXCLUDED.password_hash,
                updated_at=now()""", (password_hash,))

    def configured(self) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM control_center_credentials WHERE id=1"
            ).fetchone() is not None

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _check_rate(self, client_key: str, now: float) -> None:
        with self._lock:
            failures = [value for value in self._failures.get(client_key, []) if now - value < 300]
            self._failures[client_key] = failures
            if len(failures) >= 5:
                raise RateLimitError(max(1, int(300 - (now - failures[0]))))

    def _record_failure(self, client_key: str, now: float) -> None:
        with self._lock:
            self._failures.setdefault(client_key, []).append(now)

    def login(self, password: str, client_key: str) -> Session:
        now = time.time()
        self._check_rate(client_key, now)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM control_center_credentials WHERE id=1"
            ).fetchone()
        valid = bool(row) and bcrypt.checkpw(
            str(password).encode("utf-8"), row["password_hash"].encode("ascii")
        )
        if not valid:
            self._record_failure(client_key, now)
            raise AuthenticationError("invalid_credentials")
        with self._lock:
            self._failures.pop(client_key, None)
            token = secrets.token_urlsafe(32)
            session = Session(token, secrets.token_urlsafe(24), now + self.session_ttl,
                              now, now, str(client_key), self.role)
            self._sessions[self._digest(token)] = session
            return session

    def authenticate(self, token: str | None, client_key: str | None = None) -> Session | None:
        if not token:
            return None
        digest = self._digest(token)
        with self._lock:
            session = self._sessions.get(digest)
            if not session:
                return None
            now = time.time()
            if session.expires_at <= now or (
                session.last_seen and now - session.last_seen > self.session_idle_ttl
            ):
                self._sessions.pop(digest, None)
                return None
            if client_key and session.client_key and not secrets.compare_digest(session.client_key, str(client_key)):
                return None
            refreshed = replace(session, last_seen=now)
            self._sessions[digest] = refreshed
            return refreshed

    def check_csrf(self, session: Session, value: str | None) -> bool:
        return bool(value) and secrets.compare_digest(session.csrf_token, value)

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._digest(token), None)

    def check_write(self, session: Session) -> None:
        """Enforce role and per-session write limits before CSRF-protected writes."""
        if session.role == "viewer":
            raise AuthenticationError("read_only_session")
        if not self.write_rate_limit:
            return
        now = time.time()
        digest = self._digest(session.token)
        with self._lock:
            writes = [value for value in self._writes.get(digest, []) if now - value < 60]
            if len(writes) >= self.write_rate_limit:
                retry_after = max(1, int(60 - (now - writes[0])))
                self._writes[digest] = writes
                raise WriteRateLimitError(retry_after)
            writes.append(now)
            self._writes[digest] = writes

    def session_info(self, session: Session) -> dict[str, object]:
        return {
            "role": session.role,
            "created_at": session.created_at,
            "last_seen": session.last_seen,
            "expires_at": session.expires_at,
            "idle_timeout_seconds": self.session_idle_ttl,
        }
