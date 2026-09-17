"""Persistent repository-scoped Engineering Worker (V1-compatible CoderAgent)."""

from .agent import CoderAgent

EngineeringAgent = CoderAgent

__all__ = ["CoderAgent", "EngineeringAgent"]

__version__ = "0.1.0"
