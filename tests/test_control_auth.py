from contextlib import contextmanager

import pytest

from control_center.auth import AuthManager, AuthenticationError, RateLimitError


class Result:
    def __init__(self, row=None): self.row = row
    def fetchone(self): return self.row


class Connection:
    def __init__(self, password_hash=None): self.password_hash = password_hash; self.written_hash = None
    def execute(self, sql, args=()):
        if "SELECT password_hash" in sql:
            return Result({"password_hash": self.password_hash} if self.password_hash else None)
        if "INSERT INTO control_center_credentials" in sql:
            self.written_hash = args[0]
        return Result(None)


def bound_auth(password_hash=None):
    auth = AuthManager("unused", session_ttl=60)
    connection = Connection(password_hash)
    @contextmanager
    def connect():
        yield connection
    auth.connect = connect
    return auth, connection


def test_password_hash_and_opaque_session():
    writer, connection = bound_auth()
    writer.set_password("correct horse battery staple")
    # A real database row contains a bcrypt hash, never the password itself.
    assert connection.written_hash and connection.written_hash != "correct horse battery staple"
    auth, _ = bound_auth(connection.written_hash)
    session = auth.login("correct horse battery staple", "127.0.0.1")
    assert session.token and session.csrf_token and session.token != connection.written_hash
    assert auth.authenticate(session.token) == session
    assert auth.check_csrf(session, session.csrf_token)
    auth.logout(session.token)
    assert auth.authenticate(session.token) is None

def test_password_minimum_is_eight_characters():
    auth, writer = bound_auth()
    with pytest.raises(ValueError):
        auth.set_password("short7")
    auth.set_password("eight888")


def test_failed_login_is_rate_limited():
    import bcrypt
    auth, _ = bound_auth(bcrypt.hashpw(b"correct horse battery staple", bcrypt.gensalt()).decode())
    for _ in range(5):
        with pytest.raises(AuthenticationError): auth.login("wrong password", "client")
    with pytest.raises(RateLimitError): auth.login("wrong password", "client")
