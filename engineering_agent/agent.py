"""Public V2 EngineeringAgent surface over the preserved implementation."""

from coder_agent.agent import EngineeringAgent

# Compatibility is intentionally one-way: the canonical symbol is imported
# first, while old callers can still resolve ``CoderAgent`` during migration.
CoderAgent = EngineeringAgent

__all__ = ["CoderAgent", "EngineeringAgent"]
