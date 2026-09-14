"""Policy engine for selecting an agent and execution target."""

from .classifier import IntentClassifier, configured_classifier
from .models import ComputeProfile, RouteDecision, RouterPolicy


AGENTS = {
    "business": "business-operations-agent",
    "financial": "align-finance-agent",
    "homelab": "homelab-operator-agent",
    "research": "research-agent",
    "general": "generalist-agent",
}


class Router:
    """Interprets a request, filters unsafe targets, and ranks the remainder."""

    def __init__(self, profiles: list[ComputeProfile], policy: RouterPolicy, classifier: IntentClassifier | None = None) -> None:
        self.profiles, self.policy, self.classifier = profiles, policy, classifier or configured_classifier()

    def route(self, request: str) -> RouteDecision:
        """Return the most suitable agent and compute target for a request."""
        intent = self.classifier.classify(request)
        candidates = [p for p in self.profiles if p.available and intent.complexity in p.capabilities]
        if intent.privacy_sensitive:
            local = [p for p in candidates if p.location == "local"]
            if local:
                candidates = local
        if not candidates:
            raise RuntimeError("No available compute target can handle this request")
        ranked = [(self._score(p, intent), p) for p in candidates]
        score, target = max(ranked, key=lambda item: item[0])
        return RouteDecision(AGENTS.get(intent.domain, AGENTS["general"]), target.name, target.location, intent, round(score, 2), self._reasons(target, intent))

    def _score(self, target: ComputeProfile, intent) -> float:
        score = 100.0
        for rank, priority in enumerate(self.policy.priority_order):
            weight = len(self.policy.priority_order) - rank
            if priority == "cost" and target.location == "local":
                score -= (target.power_watts / 1000 * 0.25 * self.policy.local_energy_cost_per_kwh) * weight
            elif priority == "cost" and target.location == "cloud":
                score -= self.policy.cloud_cost_per_request * 10 * weight
            elif priority == "privacy" and intent.privacy_sensitive and target.location == "local":
                score += 12 * weight
            elif priority == "speed":
                score -= target.estimated_latency_seconds * weight
            elif priority == "useful_heat" and self.policy.heating_season and self.policy.outdoor_temperature_f < 55 and target.location == "local":
                score += 4 * weight
        if target.location == "local" and target.temperature_c >= self.policy.max_local_temperature_c:
            score -= 1_000
        return score

    def _reasons(self, target: ComputeProfile, intent) -> list[str]:
        reasons = [f"{target.name} supports {intent.complexity} tasks and is available"]
        if intent.privacy_sensitive and target.location == "local":
            reasons.append("kept privacy-sensitive work on local compute")
        if target.location == "local" and self.policy.heating_season and self.policy.outdoor_temperature_f < 55:
            reasons.append("local power draw provides useful heat under the current policy")
        if target.temperature_c >= self.policy.max_local_temperature_c:
            reasons.append("warning: target is at the configured temperature limit")
        return reasons
