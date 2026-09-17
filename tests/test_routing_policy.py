from __future__ import annotations

import json

from agent_core.llm import BackendError, InferenceRouter, LLMResponse, Router


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
