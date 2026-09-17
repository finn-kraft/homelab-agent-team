"""Canonical V2 EngineeringAgent command-line entry point.

The implementation and its durable worker transport still live in the
preserved ``coder_agent`` module during the compatibility migration.  Keeping
this tiny forwarding module gives installations a real ``engineering-agent``
entry point while ensuring both names execute the exact same code path.
"""

from coder_agent.cli import build_agent, build_engineer, main

__all__ = ["build_agent", "build_engineer", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
