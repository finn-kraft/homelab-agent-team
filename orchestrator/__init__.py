"""Deterministic, durable workflow coordination for the agent team.

The orchestrator deliberately does not decide what work means or how it should
be implemented. It owns only the state transitions between the existing
Planner, EngineeringAgent, Reviewer, verifier, and controlled Git checkpoint service.
"""

from .config import OrchestratorConfig
from .orchestrator import AgentOrchestrator, AdvanceResult
from .store import OrchestratorStore

__all__ = ["AdvanceResult", "AgentOrchestrator", "OrchestratorConfig", "OrchestratorStore"]
