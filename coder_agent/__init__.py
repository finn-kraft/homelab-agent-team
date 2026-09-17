"""Persistent repository-scoped Engineering Worker.

``CoderAgent`` remains an import-compatible alias for V1 deployments.
"""

from .agent import CoderAgent, EngineeringAgent

__all__ = ["CoderAgent", "EngineeringAgent"]

__version__ = "0.1.0"
