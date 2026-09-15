"""Data contracts for routing decisions."""

from dataclasses import asdict, dataclass, field
from typing import Literal

Complexity = Literal["light", "medium", "heavy"]
Location = Literal["local", "cloud"]


@dataclass
class TaskIntent:
    """The structured interpretation of a user's request."""

    domain: str = "general"
    urgency: int = 3
    complexity: Complexity = "medium"
    privacy_sensitive: bool = False
    needs_live_data: bool = False
    summary: str = ""


@dataclass
class ComputeProfile:
    """A currently available local or cloud execution option."""

    name: str
    location: Location
    available: bool
    capabilities: list[Complexity]
    power_watts: int
    temperature_c: float
    estimated_latency_seconds: float


@dataclass
class RouterPolicy:
    """Owner-controlled rules used to rank feasible execution options."""

    priority_order: list[str] = field(
        default_factory=lambda: ["availability", "cost", "privacy", "speed", "useful_heat"]
    )
    local_energy_cost_per_kwh: float = 0.14
    cloud_cost_per_request: float = 0.03
    heating_season: bool = False
    outdoor_temperature_f: float = 70
    max_local_temperature_c: float = 78
    # Internal development-agent inference uses a different decision from the
    # end-user/domain route above.  Keep it explicit so a faster cloud model
    # cannot silently become the normal default.
    inference_local_model: str = "llama3.2:latest"
    inference_cloud_model: str = "openrouter/auto"
    inference_escalation_attempt: int = 4
    allow_cloud_inference: bool = True


@dataclass
class RouteDecision:
    """An auditable decision about where a task should be handled."""

    agent: str
    execution_target: str
    location: Location
    intent: TaskIntent
    score: float
    reasons: list[str]

    def to_dict(self) -> dict:
        """Return a JSON-ready representation."""
        return asdict(self)


@dataclass
class InferenceRouteDecision:
    """A policy decision for an internal agent's LLM invocation.

    This deliberately does not select a domain agent.  The caller already is
    the Planner, Coder, or Reviewer; this object only decides which configured
    inference backend it may use.
    """

    execution_target: str
    provider: Literal["ollama", "openrouter"]
    model: str
    location: Location
    reason: str
    compute_profile: str
    escalation_permitted: bool

    def to_dict(self) -> dict:
        """Return the stable JSON contract served by ``/route/inference``."""
        return asdict(self)
