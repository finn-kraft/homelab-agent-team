import unittest

from routing_agent.classifier import KeywordClassifier
from routing_agent.models import ComputeProfile, RouterPolicy
from routing_agent.router import Router


class RouterTests(unittest.TestCase):
    """Regression tests for the policy engine."""

    def test_private_financial_request_stays_local(self) -> None:
        """Local compute wins when a capable target is available for private work."""
        profiles = [
            ComputeProfile("local", "local", True, ["light", "medium"], 100, 50, 12),
            ComputeProfile("cloud", "cloud", True, ["light", "medium", "heavy"], 0, 0, 2),
        ]
        router = Router(profiles, RouterPolicy(), KeywordClassifier())
        decision = router.route("Review this customer's invoice and financial records")
        self.assertEqual("business-operations-agent", decision.agent)
        self.assertEqual("local", decision.location)

    def test_heavy_work_uses_capable_target(self) -> None:
        """A short architecture request is still recognized as heavy work."""
        profiles = [
            ComputeProfile("local", "local", True, ["light", "medium"], 100, 50, 12),
            ComputeProfile("cloud", "cloud", True, ["light", "medium", "heavy"], 0, 0, 2),
        ]
        decision = Router(profiles, RouterPolicy(), KeywordClassifier()).route("Design a Kubernetes architecture roadmap")
        self.assertEqual("heavy", decision.intent.complexity)
        self.assertEqual("cloud", decision.location)

    def test_inference_stays_on_ollama_before_escalation_threshold(self) -> None:
        """A strong-model request does not bypass the Ollama-first policy."""
        profiles = [
            ComputeProfile("local", "local", True, ["light", "medium", "heavy"], 100, 50, 12),
            ComputeProfile("cloud", "cloud", True, ["light", "medium", "heavy"], 0, 0, 2),
        ]
        decision = Router(profiles, RouterPolicy(), KeywordClassifier()).route_inference(
            request="Review a difficult diff",
            caller_agent="reviewer-agent",
            task_type="code_review",
            attempt=3,
            complexity="heavy",
            needs_strong_model=True,
        )
        self.assertEqual("ollama", decision.provider)
        self.assertFalse(decision.escalation_permitted)

    def test_inference_escalates_after_configured_local_failure_threshold(self) -> None:
        """The first default cloud-eligible attempt is attempt four."""
        profiles = [
            ComputeProfile("local", "local", True, ["light", "medium", "heavy"], 100, 50, 12),
            ComputeProfile("cloud", "cloud", True, ["light", "medium", "heavy"], 0, 0, 2),
        ]
        decision = Router(profiles, RouterPolicy(), KeywordClassifier()).route_inference(
            request="Review a difficult diff",
            caller_agent="reviewer-agent",
            task_type="code_review",
            attempt=4,
            complexity="heavy",
            needs_strong_model=True,
            local_failures=3,
        )
        self.assertEqual("openrouter", decision.provider)
        self.assertTrue(decision.escalation_permitted)

    def test_private_inference_remains_local_after_threshold(self) -> None:
        """Privacy protection wins over normal cloud escalation."""
        profiles = [
            ComputeProfile("local", "local", True, ["light", "medium", "heavy"], 100, 50, 12),
            ComputeProfile("cloud", "cloud", True, ["light", "medium", "heavy"], 0, 0, 2),
        ]
        decision = Router(profiles, RouterPolicy(), KeywordClassifier()).route_inference(
            request="Review a customer financial change",
            caller_agent="reviewer-agent",
            task_type="code_review",
            attempt=4,
            complexity="heavy",
            privacy_sensitive=True,
            needs_strong_model=True,
            local_failures=3,
        )
        self.assertEqual("ollama", decision.provider)
