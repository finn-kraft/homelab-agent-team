import argparse
import getpass
import os
import sys
from agent_core.structured_logging import configure_logging
from .app import ControlCenter
from .auth import AuthManager
from .store import ControlStore
from .telemetry import TelemetryClient
from .projects import ProjectCatalog

def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-control-center")
    parser.add_argument("command", nargs="?", choices=("set-password",),
                        help="manage Control Center credentials")
    parser.add_argument("--host", default=os.getenv("CONTROL_CENTER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CONTROL_CENTER_PORT", "8080")))
    parser.add_argument("--password-stdin", action="store_true",
                        help="read the new password from stdin for set-password")
    args = parser.parse_args(argv)
    configure_logging("agent-control-center")
    database_url = os.getenv("DATABASE_URL")
    if not database_url: raise SystemExit("DATABASE_URL is required")
    auth = AuthManager(database_url,
                       secure_cookie=os.getenv("CONTROL_CENTER_COOKIE_SECURE", "false").lower()
                       in {"1", "true", "yes", "on"},
                       session_ttl=int(os.getenv("CONTROL_CENTER_SESSION_TTL_SECONDS", "43200")),
                       session_idle_ttl=int(os.getenv("CONTROL_CENTER_SESSION_IDLE_SECONDS", "21600")),
                       write_rate_limit=int(os.getenv("CONTROL_CENTER_WRITE_RATE_LIMIT", "120")),
                       role=os.getenv("CONTROL_CENTER_ROLE", "operator"))
    if args.command == "set-password":
        password = sys.stdin.read().rstrip("\r\n") if args.password_stdin else getpass.getpass("New Control Center password: ")
        if not args.password_stdin:
            confirm = getpass.getpass("Repeat password: ")
            if password != confirm: raise SystemExit("Passwords do not match")
        auth.set_password(password)
        print("Control Center password updated.")
        return 0
    projects = ProjectCatalog.from_env()
    ControlCenter(ControlStore(database_url, projects), TelemetryClient(
        os.getenv("GPU_TELEMETRY_URL"), os.getenv("GPU_TELEMETRY_TOKEN"),
        os.getenv("OLLAMA_URL"), os.getenv("ROUTER_URL")
    ), auth).serve(args.host, args.port)
    return 0

if __name__ == "__main__": main()
