"""Compatibility wrapper for deployments that import a worker object."""

from .orchestrator import AgentOrchestrator


class OrchestratorWorker:
    """A foreground worker; all durable scheduling is in ``AgentOrchestrator``."""

    def __init__(self, orchestrator: AgentOrchestrator):
        self.orchestrator = orchestrator

    def run_forever(self) -> None:
        self.orchestrator.run()
