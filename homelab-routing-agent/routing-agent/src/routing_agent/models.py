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
