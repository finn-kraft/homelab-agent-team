"""EngineeringAgent CLI, backed by the preserved coder command implementation."""

from coder_agent.cli import build_agent, build_engineer, main

__all__ = ["build_agent", "build_engineer", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
