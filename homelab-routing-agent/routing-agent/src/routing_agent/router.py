"""Policy engine for selecting an agent and execution target."""

from .classifier import IntentClassifier, configured_classifier
from .models import ComputeProfile, InferenceRouteDecision, RouteDecision, RouterPolicy


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

    def route_inference(
        self,
        *,
        request: str,
        caller_agent: str,
        task_type: str,
        attempt: int = 1,
        complexity: str = "medium",
        privacy_sensitive: bool = False,
        needs_strong_model: bool = False,
        local_failures: int = 0,
    ) -> InferenceRouteDecision:
        """Choose an LLM backend for an already-selected internal agent.

        The policy is intentionally Ollama-first.  A request for a stronger
        model is *not* by itself a cloud escalation: a capable local target is
        used through the configured repeated-failure threshold.  Cloud is used
        earlier only when no safe, capable local target is available.
        """
        del request, caller_agent, task_type  # metadata is audited by the caller, not reclassified here
        if complexity not in {"light", "medium", "heavy"}:
            raise ValueError("complexity must be light, medium, or heavy")
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        if local_failures < 0:
            raise ValueError("local_failures cannot be negative")
        if self.policy.inference_escalation_attempt < 2:
            raise ValueError("inference_escalation_attempt must be at least 2")

        local = self._inference_candidates("local", complexity)
        cloud = self._inference_candidates("cloud", complexity)
        local_target = self._best_inference_target(local, complexity)
        cloud_target = self._best_inference_target(cloud, complexity)

        # The attempt number is the canonical signal when older callers do not
        # provide local_failures.  A fourth attempt is the first cloud-eligible
        # attempt under the default policy (attempts 1-3 stay local).
        repeated_local_failure = max(attempt, local_failures + 1) >= self.policy.inference_escalation_attempt
        cloud_eligible = (
            self.policy.allow_cloud_inference
            and not privacy_sensitive
            and cloud_target is not None
            and repeated_local_failure
            and (needs_strong_model or local_failures > 0 or attempt >= self.policy.inference_escalation_attempt)
        )

        if local_target is not None and not cloud_eligible:
            reason = "local Ollama preferred; escalation threshold not reached"
            if privacy_sensitive:
                reason = "privacy-sensitive work kept on local Ollama"
            elif needs_strong_model:
                reason = "local Ollama preferred until repeated local failures reach the escalation threshold"
            return self._local_inference_decision(local_target, reason, False)

        if cloud_eligible:
            return InferenceRouteDecision(
                execution_target="openrouter",
                provider="openrouter",
                model=self.policy.inference_cloud_model,
                location="cloud",
                reason="configured local failure threshold reached; OpenRouter escalation permitted",
                compute_profile=cloud_target.name,
                escalation_permitted=True,
            )

        # An unavailable/incapable local backend is a fallback condition, not a
        # policy escalation.  It is important to expose that distinction to the
        # Orchestrator's audit log.
        if cloud_target is not None and self.policy.allow_cloud_inference and not privacy_sensitive:
            return InferenceRouteDecision(
                execution_target="openrouter",
                provider="openrouter",
                model=self.policy.inference_cloud_model,
                location="cloud",
                reason="no available local Ollama target can safely handle this task; using configured cloud fallback",
                compute_profile=cloud_target.name,
                escalation_permitted=False,
            )

        if local_target is not None:
            return self._local_inference_decision(
                local_target,
                "local Ollama is the only available policy-compliant inference target",
                False,
            )
        raise RuntimeError("No available inference target can handle this request")

    def _inference_candidates(self, location: str, complexity: str) -> list[ComputeProfile]:
        """Return available profiles safe enough for the requested inference."""
        candidates = [
            profile
            for profile in self.profiles
            if profile.location == location and profile.available and complexity in profile.capabilities
        ]
        if location == "local":
            candidates = [profile for profile in candidates if profile.temperature_c < self.policy.max_local_temperature_c]
        return candidates

    def _best_inference_target(self, candidates: list[ComputeProfile], complexity: str) -> ComputeProfile | None:
        """Reuse the configured policy ranking without invoking classification."""
        if not candidates:
            return None
        intent = type("InferenceIntent", (), {"complexity": complexity, "privacy_sensitive": False})()
        return max(candidates, key=lambda profile: self._score(profile, intent))

    def _local_inference_decision(
        self,
        target: ComputeProfile,
        reason: str,
        escalation_permitted: bool,
    ) -> InferenceRouteDecision:
        return InferenceRouteDecision(
            execution_target="ollama",
            provider="ollama",
            model=self.policy.inference_local_model,
            location="local",
            reason=reason,
            compute_profile=target.name,
            escalation_permitted=escalation_permitted,
        )

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
