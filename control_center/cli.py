import argparse
import os
from .app import ControlCenter
from .store import ControlStore
from .telemetry import TelemetryClient
from .projects import ProjectCatalog

def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-control-center")
    parser.add_argument("--host", default=os.getenv("CONTROL_CENTER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CONTROL_CENTER_PORT", "8080")))
    args = parser.parse_args(argv)
    token = os.getenv("CONTROL_CENTER_TOKEN")
    if not token: raise SystemExit("CONTROL_CENTER_TOKEN is required")
    projects = ProjectCatalog.from_env()
    ControlCenter(ControlStore(os.environ["DATABASE_URL"], projects), TelemetryClient(
        os.getenv("GPU_TELEMETRY_URL"), os.getenv("GPU_TELEMETRY_TOKEN"),
        os.getenv("OLLAMA_URL"), os.getenv("ROUTER_URL")
    ), token).serve(args.host, args.port)
    return 0

if __name__ == "__main__": main()
