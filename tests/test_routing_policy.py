from __future__ import annotations

import json
import time
import urllib.error

from agent_core.llm import (
    BackendError, CircuitStateStore, HTTPBackend, InferenceRouter, LLMResponse,
    Router, RoutingPolicy,
)


class Backend:
    def __init__(self, name: str, *, fail: bool = False):
        self.model = f"{name}-model"
        self.name = name
        self.fail = fail

    def complete(self, messages):
        if self.fail:
            raise BackendError(f"{self.name} unavailable")
        return LLMResponse("{}", self.model, self.name, 0.1, {"tokens": 2})


class PolicyRouter(InferenceRouter):
    def __init__(self, local, cloud, response, **kwargs):
        super().__init__(
            local, cloud, caller_agent="coder-agent", task_type="code_implementation",
            router_url="http://router.invalid", **kwargs,
        )
        self.response = response

    def _route_request(self, attempt, needs_strong_model):
        return self.response


def test_router_unavailable_defaults_to_local_ollama():
    local, cloud = Backend("ollama"), Backend("openrouter")
    router = PolicyRouter(local, cloud, None)

    response = router.choose(1).complete([])

    assert response.backend == "ollama"
    assert router.last_route.fallback is True
    assert "unavailable" in router.last_route.reason.lower()


def test_cloud_request_is_deferred_before_fourth_attempt():
    local, cloud = Backend("ollama"), Backend("openrouter")
    decision = {"execution_target": "openrouter", "reason": "complex review"}
    router = PolicyRouter(local, cloud, decision, escalate_after=4)

    assert router.choose(3).complete([]).backend == "ollama"
    assert "deferred" in router.last_route.reason.lower()
    assert router.choose(4).complete([]).backend == "openrouter"


def test_actual_ollama_outage_can_use_cloud_availability_backup():
    local, cloud = Backend("ollama", fail=True), Backend("openrouter")
    router = PolicyRouter(local, cloud, None)

    response = router.choose(1).complete([])

    assert response.backend == "openrouter"
    assert router.last_route.fallback is True
    assert "failed" in router.last_route.reason.lower()


def test_privacy_policy_prevents_cloud_availability_backup():
    local, cloud = Backend("ollama", fail=True), Backend("openrouter")
    router = PolicyRouter(local, cloud, None, privacy_sensitive=True)

    try:
        router.choose(5).complete([])
    except BackendError:
        pass
    else:
        raise AssertionError("privacy-sensitive local failure must not leak to cloud")


def test_legacy_router_default_uses_five_local_attempts():
    local, cloud = object(), object()
    router = Router(local, cloud)

    assert router.choose(3, needs_strong_model=True) is local
    assert router.choose(4) is local
    assert router.choose(5) is local
    assert router.choose(6) is cloud


def test_normal_agent_route_uses_supported_medium_complexity(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b'{"execution_target":"ollama"}'
    def open_request(request, timeout):
        captured.update(json.loads(request.data))
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    router = InferenceRouter(
        Backend("ollama"), Backend("openrouter"), router_url="http://router.invalid",
        caller_agent="coder-agent", task_type="code_implementation",
    )

    assert router._route_request(1, False)["execution_target"] == "ollama"
    assert captured["complexity"] == "medium"


def test_paid_backend_http_402_opens_circuit(monkeypatch):
    backend = HTTPBackend("https://example.invalid", "model", "secret", retries=0)
    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://example.invalid", 402, "payment", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", rejected)
    try:
        backend._post("https://example.invalid", {})
    except BackendError as exc:
        assert "circuit" in str(exc)
    else:
        raise AssertionError("expected circuit-opening backend error")
    assert backend.circuit_open and backend.circuit_retry_seconds > 0


def test_open_cloud_circuits_fall_back_to_local_without_repeating_error():
    local = Backend("ollama")
    standard = HTTPBackend("https://example.invalid", "standard", "secret")
    premium = HTTPBackend("https://example.invalid", "premium", "secret")
    standard._circuit_open_until = time.time() + 60
    premium._circuit_open_until = time.time() + 60
    router = InferenceRouter(
        local, standard, premium, caller_agent="engineering-agent",
        task_type="code_implementation", router_url=None, escalate_after=4,
    )

    response = router.choose(7).complete([])

    assert response.backend == "ollama"
    assert router.last_route.fallback is True


def test_paid_tier_failure_walks_all_the_way_back_to_local():
    local = Backend("ollama")
    standard = Backend("openrouter-standard", fail=True)
    premium = Backend("openrouter-premium", fail=True)
    router = InferenceRouter(
        local, standard, premium, caller_agent="engineering-agent",
        task_type="code_implementation", router_url=None, escalate_after=4,
    )

    response = router.choose(4).complete([])

    assert response.backend == "ollama"


def test_open_circuit_is_skipped_if_it_opens_after_chain_creation():
    local = Backend("ollama")
    standard = HTTPBackend("https://example.invalid", "standard", "secret")
    router = InferenceRouter(
        local, standard, caller_agent="engineering-agent",
        task_type="code_implementation", router_url=None, escalate_after=4,
    )

    backend = router.choose(4)
    # Simulate a breaker opening between route selection and model execution.
    standard._circuit_open_until = time.time() + 60
    assert backend.complete([]).backend == "ollama"


def test_per_agent_policy_can_forbid_cloud_and_require_capabilities():
    local, cloud = Backend("ollama"), Backend("openrouter")
    router = InferenceRouter(
        local, cloud, caller_agent="reviewer-agent", task_type="code_review",
        router_url=None, escalate_after=2,
        policy=RoutingPolicy(allowed_providers=frozenset({"ollama"}),
                             required_capabilities=frozenset({"vision"})),
    )
    assert router.choose(4).complete([]).backend == "ollama"


def test_circuit_state_survives_backend_reconstruction(tmp_path):
    path = tmp_path / "circuits.json"
    state = CircuitStateStore(str(path))
    state.save("openrouter", "https://example.invalid", "model", time.time() + 60)
    backend = HTTPBackend(
        "https://example.invalid", "model", "secret", retries=0,
        circuit_state_path=str(path),
    )
    assert backend.circuit_open is True


def test_failover_records_cloud_usage_once():
    local = Backend("ollama")
    standard = Backend("openrouter-standard", fail=True)

    class Premium(Backend):
        def complete(self, messages):
            return LLMResponse("{}", self.model, "openrouter", 0.2, {"cost": 0.25})

    premium = Premium("openrouter-premium")
    router = InferenceRouter(
        local, standard, premium, caller_agent="engineering-agent",
        task_type="code_implementation", router_url=None, escalate_after=4,
    )

    assert router.choose(4).complete([]).backend == "openrouter"
    assert router.cloud_spend == 0.25
    assert router.cloud_latency_seconds == [0.2]
