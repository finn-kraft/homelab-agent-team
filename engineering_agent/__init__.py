"""Public V2 EngineeringAgent package.

The implementation intentionally reuses the proven workspace/tooling module so
existing imports and installations continue to work during the migration.
"""

from coder_agent.agent import CoderAgent, EngineeringAgent
from coder_agent.models import AgentResult, Status, Task, WorkPackage
from coder_agent.worker import EngineeringWorker

__all__ = [
    "AgentResult",
    "CoderAgent",
    "EngineeringAgent",
    "EngineeringWorker",
    "Status",
    "Task",
    "WorkPackage",
]
