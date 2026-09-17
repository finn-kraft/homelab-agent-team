from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from .models import ModelRoute


class BackendError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    backend: str
    latency_seconds: float = 0
    usage: dict | None = None


class HTTPBackend:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: int = 120, retries: int = 2):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.timeout, self.retries = timeout, retries

    def _post(self, url: str, payload: dict) -> tuple[dict, float]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response), time.monotonic() - started
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
        raise BackendError(f"backend unavailable after retries: {last}")


class OllamaBackend(HTTPBackend):
    def complete(self, messages: list[dict[str, str]]) -> LLMResponse:
        data, latency = self._post(f"{self.base_url}/api/chat", {
            "model": self.model, "messages": messages, "stream": False, "format": "json",
        })
        usage = {k: data[k] for k in ("prompt_eval_count", "eval_count") if k in data}
        return LLMResponse(data["message"]["content"], self.model, "ollama", latency, usage)


class OpenRouterBackend(HTTPBackend):
    def complete(self, messages: list[dict[str, str]]) -> LLMResponse:
        data, latency = self._post(f"{self.base_url}/chat/completions", {
            "model": self.model, "messages": messages,
            "response_format": {"type": "json_object"},
        })
        return LLMResponse(data["choices"][0]["message"]["content"], self.model,
                           "openrouter", latency, data.get("usage"))


class Router:
    """Small compatibility router for agents that do not use the Router API.

    ``needs_strong_model`` remains part of the public method signature because
    the specialist agents already supply it as useful routing metadata.  It is
    deliberately *not* an immediate cloud override: normal work stays local
    until the configured repeated-failure threshold is reached.
    """

    def __init__(self, local: OllamaBackend, cloud: OpenRouterBackend | None = None,
                 escalate_after: int = 4):
        self.local, self.cloud = local, cloud
        self.escalate_after = max(1, int(escalate_after))

    def choose(self, attempt: int, needs_strong_model: bool = False):
        # A difficult task alone is not a reason to immediately send source
        # code or financial logic to the cloud.  The caller's repeated attempt
        # count is the deterministic default escalation signal.
        if self.cloud and attempt >= self.escalate_after:
            return self.cloud
        return self.local


class _FailoverBackend:
    """Use a cloud backup only after the selected local backend is unavailable."""

    def __init__(self, primary: HTTPBackend, fallback: HTTPBackend | None,
                 on_fallback: Callable[[HTTPBackend], None] | None = None,
                 on_response: Callable[[LLMResponse], None] | None = None):
        self.primary, self.fallback = primary, fallback
        self._on_fallback, self._on_response = on_fallback, on_response

    @property
    def model(self) -> str:
        return self.primary.model

    def complete(self, messages: list[dict[str, str]]) -> LLMResponse:
        try:
            response = self.primary.complete(messages)  # type: ignore[attr-defined]
        except BackendError:
            if self.fallback is None:
                raise
            if self._on_fallback:
                self._on_fallback(self.fallback)
            response = self.fallback.complete(messages)  # type: ignore[attr-defined]
        if self._on_response:
            self._on_response(response)
        return response


class InferenceRouter(Router):
    """Model policy adapter for the separate Homelab Routing Agent.

    Workflow ownership stays with the Orchestrator.  This class only asks the
    Router where one LLM call should run, validates the answer against the
    Ollama-first policy, and falls back safely when the Router is unavailable.
    It intentionally has the same ``choose`` API as :class:`Router` so current
    Planner, Coder, and Reviewer implementations remain compatible.
    """

    def __init__(
        self,
        local: OllamaBackend,
        cloud: OpenRouterBackend | None = None,
        *,
        router_url: str | None = None,
        caller_agent: str,
        task_type: str,
        escalate_after: int = 4,
        timeout: float = 3.0,
        privacy_sensitive: bool = False,
    ):
        super().__init__(local, cloud, escalate_after)
        self.caller_agent = caller_agent
        self.task_type = task_type
        self.router_url = self._inference_endpoint(router_url)
        self.timeout = max(0.1, float(timeout))
        self.privacy_sensitive = privacy_sensitive
        self.last_route: ModelRoute | None = None

    @staticmethod
    def _inference_endpoint(router_url: str | None) -> str | None:
        if not router_url:
            return None
        endpoint = router_url.rstrip("/")
        if endpoint.endswith("/route/inference"):
            return endpoint
        if endpoint.endswith("/route"):
            return f"{endpoint}/inference"
        return f"{endpoint}/route/inference"

    def _route_request(self, attempt: int, needs_strong_model: bool) -> dict[str, Any] | None:
        if not self.router_url:
            return None
        payload = {
            "request": f"{self.task_type} for {self.caller_agent}",
            "caller_agent": self.caller_agent,
            "task_type": self.task_type,
            # External routing API uses one-based attempts.  Planner's first
            # call historically reports zero prior failures, so normalize it.
            "attempt": max(1, int(attempt)),
            "complexity": "heavy" if needs_strong_model else "medium",
            "privacy_sensitive": self.privacy_sensitive,
            "needs_strong_model": bool(needs_strong_model),
        }
        request = urllib.request.Request(
            self.router_url,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.load(response)
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _normalise_target(value: dict[str, Any]) -> str:
        target = str(value.get("execution_target") or value.get("provider") or "ollama").lower()
        if target in {"local", "ollama", "local_ollama"}:
            return "ollama"
        if target in {"cloud", "openrouter", "open_router"}:
            return "openrouter"
        return "ollama"

    @staticmethod
    def _with_model(backend: HTTPBackend, model: str) -> HTTPBackend:
        if not model or model == backend.model:
            return backend
        return type(backend)(backend.base_url, model, backend.api_key,
                             backend.timeout, backend.retries)

    def _set_route(self, *, provider: str, model: str, location: str, reason: str,
                   attempt: int, fallback: bool = False) -> None:
        self.last_route = ModelRoute(
            caller_agent=self.caller_agent,
            provider=provider,
            model=model,
            location=location,
            reason=reason,
            attempt=max(1, int(attempt)),
            task_type=self.task_type,
            fallback=fallback,
            privacy_sensitive=self.privacy_sensitive,
        )

    def choose(self, attempt: int, needs_strong_model: bool = False):
        """Return a backend chosen by policy, never trusting cloud too early."""

        policy = self._route_request(attempt, needs_strong_model)
        if policy is None:
            primary = self.local
            self._set_route(
                provider="ollama", model=primary.model, location="local",
                reason="Routing Agent unavailable; using safe local Ollama fallback.",
                attempt=attempt, fallback=bool(self.router_url),
            )
        else:
            target = self._normalise_target(policy)
            requested_model = str(policy.get("model") or "")
            router_reason = str(policy.get("reason") or "Routing Agent policy decision.")
            if target == "openrouter" and self.cloud is not None and attempt >= self.escalate_after:
                primary = self._with_model(self.cloud, requested_model)
                self._set_route(
                    provider="openrouter", model=primary.model, location="cloud",
                    reason=router_reason, attempt=attempt,
                )
                return _FailoverBackend(primary, None, on_response=self._record_response)
            primary = self._with_model(self.local, requested_model if target == "ollama" else "")
            policy_reason = router_reason
            if target == "openrouter":
                if self.cloud is None:
                    policy_reason = f"{router_reason} Cloud backend is not configured; staying local."
                else:
                    policy_reason = (
                        f"{router_reason} OpenRouter deferred until local attempt "
                        f"{self.escalate_after}."
                    )
            self._set_route(
                provider="ollama", model=primary.model, location="local",
                reason=policy_reason, attempt=attempt,
            )

        # An actual local availability failure is a valid exception to the
        # repeated-attempt threshold.  It remains observable through last_route.
        fallback = self.cloud if not self.privacy_sensitive else None

        def note_failover(backend: HTTPBackend) -> None:
            self._set_route(
                provider="openrouter", model=backend.model, location="cloud",
                reason="Local Ollama call failed; using configured OpenRouter availability backup.",
                attempt=attempt, fallback=True,
            )

        return _FailoverBackend(primary, fallback, note_failover, self._record_response)

    def _record_response(self, response: LLMResponse) -> None:
        if self.last_route is None:
            return
        self.last_route.model = response.model
        self.last_route.provider = response.backend
        self.last_route.latency_seconds = response.latency_seconds
        self.last_route.usage = response.usage
        if response.backend == "openrouter" and response.usage:
            for key in ("cost", "total_cost", "estimated_cost"):
                if key in response.usage:
                    try:
                        self.last_route.estimated_cloud_cost = float(response.usage[key])
                    except (TypeError, ValueError):
                        pass
                    break
        self.last_route.timestamp = datetime.now(UTC)
