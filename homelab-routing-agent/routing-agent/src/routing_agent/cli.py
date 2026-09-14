"""Command-line interface for the routing agent."""

import json
import sys

from .api import load_router


def main() -> None:
    """Route the text passed on the command line."""
    request = " ".join(sys.argv[1:])
    if not request:
        raise SystemExit('Usage: router "your request"')
    print(json.dumps(load_router().route(request).to_dict(), indent=2))
