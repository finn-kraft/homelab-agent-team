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
