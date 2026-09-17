"""Password authentication and short-lived opaque browser sessions."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager

import bcrypt


SESSION_COOKIE = "control_center_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
CSRF_HEADER = "X-CSRF-Token"


class AuthenticationError(ValueError):
    """Raised when credentials or a session are not valid."""


class RateLimitError(AuthenticationError):
    """Raised after too many failed login attempts from one client."""

    def __init__(self, retry_after: int):
        super().__init__("too_many_login_attempts")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    csrf_token: str
    expires_at: float


class AuthManager:
    """Store only a bcrypt password hash in PostgreSQL; keep opaque sessions in memory."""

    def __init__(self, database_url: str, *, secure_cookie: bool = False,
                 session_ttl: int = SESSION_TTL_SECONDS):
        self.database_url = database_url
        self.secure_cookie = secure_cookie
        self.session_ttl = session_ttl
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
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
        if len(password) < 12 or len(password) > 256:
            raise ValueError("password must be 12-256 characters")
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
            session = Session(token, secrets.token_urlsafe(24), now + self.session_ttl)
            self._sessions[self._digest(token)] = session
            return session

    def authenticate(self, token: str | None) -> Session | None:
        if not token:
            return None
        digest = self._digest(token)
        with self._lock:
            session = self._sessions.get(digest)
            if not session:
                return None
            if session.expires_at <= time.time():
                self._sessions.pop(digest, None)
                return None
            return session

    def check_csrf(self, session: Session, value: str | None) -> bool:
        return bool(value) and secrets.compare_digest(session.csrf_token, value)

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._digest(token), None)

