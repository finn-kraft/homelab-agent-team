from __future__ import annotations

import json
import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .models import ModelRoute


class BackendError(RuntimeError):
    pass


class CircuitStateStore:
    """Tiny atomic JSON store for backend breaker state across restarts."""

    def __init__(self, path: str | None = None):
        configured = path or os.getenv(
            "INFERENCE_CIRCUIT_STATE_FILE",
            str(Path.home() / ".cache" / "homelab-agent-team" / "circuit-state.json"),
        )
        self.path = Path(configured).expanduser()

    @staticmethod
    def _key(provider: str, base_url: str, model: str) -> str:
        return hashlib.sha256(f"{provider}|{base_url}|{model}".encode()).hexdigest()

    def load(self, provider: str, base_url: str, model: str) -> float:
        try:
            values = json.loads(self.path.read_text())
            return max(0.0, float(values.get(self._key(provider, base_url, model), 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0.0

    def save(self, provider: str, base_url: str, model: str, until: float) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            values = {}
            try:
                values = json.loads(self.path.read_text())
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            values[self._key(provider, base_url, model)] = max(0.0, float(until))
            handle, temporary = tempfile.mkstemp(prefix=".circuit-", dir=self.path.parent)
            with os.fdopen(handle, "w") as stream:
                json.dump(values, stream)
            os.replace(temporary, self.path)
        except OSError:
            # Breaker persistence is best-effort; a read-only service account
            # must still be able to use in-process failover.
            return


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Per-agent routing guardrails applied after router recommendations."""

    allowed_providers: frozenset[str] = frozenset({"ollama", "openrouter"})
    privacy_sensitive: bool = False
    max_cloud_cost: float | None = None
    max_latency_seconds: float | None = None
    required_capabilities: frozenset[str] = frozenset()


def routing_policy_from_env(prefix: str) -> RoutingPolicy:
    """Load optional per-agent routing guardrails from environment variables."""
    raw = os.getenv(f"{prefix}_ALLOWED_PROVIDERS", "ollama,openrouter")
    providers = frozenset(value.strip().lower() for value in raw.split(",") if value.strip())
    cost = float(os.getenv(f"{prefix}_MAX_CLOUD_COST", "0"))
    latency = float(os.getenv(f"{prefix}_MAX_LATENCY_SECONDS", "0"))
    capabilities = frozenset(
        value.strip().lower()
        for value in os.getenv(f"{prefix}_REQUIRED_CAPABILITIES", "").split(",")
        if value.strip()
    )
    return RoutingPolicy(
        allowed_providers=providers or frozenset({"ollama"}),
        privacy_sensitive=os.getenv(f"{prefix}_PRIVACY_SENSITIVE", "false").lower()
        in {"1", "true", "yes", "on"},
        max_cloud_cost=cost if cost > 0 else None,
        max_latency_seconds=latency if latency > 0 else None,
        required_capabilities=capabilities,
    )


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    backend: str
    latency_seconds: float = 0
    usage: dict | None = None


class HTTPBackend:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: int = 120,
        retries: int = 2,
        circuit_seconds: float = 900,
        circuit_state_path: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = max(0.1, float(timeout))
        self.retries = max(0, int(retries))
        self.circuit_seconds = max(1.0, float(circuit_seconds))
        self._circuit_state = CircuitStateStore(circuit_state_path)
        self._provider_name = "openrouter" if api_key else "ollama"
        self._circuit_open_until = self._circuit_state.load(
            self._provider_name, self.base_url, self.model
        )

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open_until > time.time()

    @property
    def circuit_retry_seconds(self) -> int:
        return max(0, int(self._circuit_open_until - time.time()))

    def _post(self, url: str, payload: dict) -> tuple[dict, float]:
        if self._circuit_open_until > time.time():
            raise BackendError("backend circuit open; retry window has not elapsed")
        headers = {"Content-Type": "application/json"}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            url,
            json.dumps(payload).encode(),
            headers,
        )

        last: Exception | None = None

        for attempt in range(self.retries + 1):
            started = time.monotonic()

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                ) as response:
                    self._clear_circuit()
                    return (
                        json.load(response),
                        time.monotonic() - started,
                    )

            except urllib.error.HTTPError as exc:
                last = exc
                if self.api_key and exc.code in {401, 402, 403}:
                    self._circuit_open_until = time.time() + self.circuit_seconds
                    self._circuit_state.save(
                        self._provider_name, self.base_url, self.model, self._circuit_open_until
                    )
                    raise BackendError(
                        f"paid backend circuit opened after HTTP {exc.code}"
                    ) from exc
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc

                if attempt < self.retries:
                    time.sleep(2 ** attempt)

        # Repeated transport failures are also circuit-worthy. Persisting this
        # short cool-down prevents every worker restart from stampeding a dead
        # endpoint while still allowing the normal availability fallback.
        self._circuit_open_until = time.time() + self.circuit_seconds
        self._circuit_state.save(
            self._provider_name, self.base_url, self.model, self._circuit_open_until
        )
        raise BackendError(f"backend unavailable after retries: {last}")

    def health_probe(self) -> dict[str, Any]:
        """Probe provider availability without consuming a model completion."""
        if self.circuit_open:
            return {"ok": False, "status": "circuit_open", "retry_seconds": self.circuit_retry_seconds}
        endpoint = f"{self.base_url}/models" if self.api_key else f"{self.base_url}/api/tags"
        request = urllib.request.Request(endpoint, headers={
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
        })
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 5.0)) as response:
                response.read(1)
            return {"ok": True, "status": "ready",
                    "latency_seconds": time.monotonic() - started}
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            return {"ok": False, "status": "unavailable", "error": str(exc)[:300],
                    "latency_seconds": time.monotonic() - started}

    def _clear_circuit(self) -> None:
        if self._circuit_open_until:
            self._circuit_open_until = 0.0
            self._circuit_state.save(self._provider_name, self.base_url, self.model, 0.0)


class OllamaBackend(HTTPBackend):
    def complete(
        self,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        data, latency = self._post(
            f"{self.base_url}/api/chat",
            {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": "json",
            },
        )

        usage = {
            key: data[key]
            for key in ("prompt_eval_count", "eval_count")
            if key in data
        }

        return LLMResponse(
            data["message"]["content"],
            self.model,
            "ollama",
            latency,
            usage,
        )


class OpenRouterBackend(HTTPBackend):
    def complete(
        self,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        data, latency = self._post(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": messages,
                "response_format": {
                    "type": "json_object",
                },
            },
        )

        return LLMResponse(
            data["choices"][0]["message"]["content"],
            self.model,
            "openrouter",
            latency,
            data.get("usage"),
        )


class Router:
    """Deterministic local -> standard -> premium cloud router."""

    def __init__(
        self,
        local: OllamaBackend,
        cloud: OpenRouterBackend | None = None,
        escalate_after: int = 6,
        *,
        premium_cloud: OpenRouterBackend | None = None,
    ):
        self.local = local
        self.cloud = cloud
        self.premium_cloud = premium_cloud
        self.escalate_after = max(2, int(escalate_after))

    def choose(
        self,
        attempt: int,
        needs_strong_model: bool = False,
    ):
        attempt = max(1, int(attempt))
        cycle_length = self.escalate_after + 1
        cycle_attempt = ((attempt - 1) % cycle_length) + 1

        if cycle_attempt < self.escalate_after:
            return self.local

        if cycle_attempt == self.escalate_after and self.cloud:
            return self.cloud

        if cycle_attempt == cycle_length and self.premium_cloud:
            return self.premium_cloud

        if self.cloud:
            return self.cloud

        return self.local


class _FailoverBackend:
    """Run a primary backend with one availability fallback."""

    def __init__(
        self,
        primary: HTTPBackend,
        fallback: HTTPBackend | None,
        on_fallback: Callable[[HTTPBackend], None] | None = None,
        on_response: Callable[[LLMResponse], None] | None = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self._on_fallback = on_fallback
        self._on_response = on_response

    @property
    def model(self) -> str:
        return self.primary.model

    def complete(
        self,
        messages: list[dict[str, str]],
    ) -> LLMResponse:
        # A paid backend may have opened its breaker after this failover chain
        # was built. Skip it at call time instead of surfacing the stale
        # ``backend circuit open`` error to the workflow.
        if getattr(self.primary, "circuit_open", False):
            if self.fallback is None:
                raise BackendError(
                    f"{self.primary.model} circuit is open and no fallback is available"
                )
            if self._on_fallback:
                self._on_fallback(self.fallback)
            response = self.fallback.complete(messages)
            if self._on_response:
                self._on_response(response)
            return response
        try:
            response = self.primary.complete(messages)

        except BackendError:
            if self.fallback is None:
                raise

            if self._on_fallback:
                self._on_fallback(self.fallback)

            response = self.fallback.complete(messages)

        if self._on_response:
            self._on_response(response)

        return response


class InferenceRouter(Router):
    """Tiered inference policy with Routing Agent observability.

    Scheduled model policy:

        attempts 1-5 -> local Ollama
        attempt 6    -> standard OpenRouter
        attempt 7+   -> premium OpenRouter

    ``escalate_after`` defines the first standard-cloud attempt, so
    the default value of 6 gives five local attempts.

    If local Ollama is unavailable rather than merely producing a
    poor response, the standard cloud backend is used immediately as
    an availability fallback.

    Privacy-sensitive requests never leave Ollama.
    """

    def __init__(
        self,
        local: OllamaBackend,
        cloud: OpenRouterBackend | None = None,
        premium_cloud: OpenRouterBackend | None = None,
        *,
        router_url: str | None = None,
        caller_agent: str,
        task_type: str,
        escalate_after: int = 6,
        timeout: float = 3.0,
        privacy_sensitive: bool = False,
        policy: RoutingPolicy | None = None,
        allowed_providers: frozenset[str] | None = None,
        max_cloud_cost: float | None = None,
        max_latency_seconds: float | None = None,
        required_capabilities: frozenset[str] | None = None,
    ):
        super().__init__(
            local,
            cloud,
            escalate_after,
            premium_cloud=premium_cloud,
        )

        self.caller_agent = caller_agent
        self.task_type = task_type
        self.router_url = self._inference_endpoint(router_url)
        self.timeout = max(0.1, float(timeout))
        self.policy = policy or RoutingPolicy(
            allowed_providers=allowed_providers or frozenset({"ollama", "openrouter"}),
            privacy_sensitive=privacy_sensitive,
            max_cloud_cost=max_cloud_cost,
            max_latency_seconds=max_latency_seconds,
            required_capabilities=required_capabilities or frozenset(),
        )
        self.privacy_sensitive = privacy_sensitive or self.policy.privacy_sensitive
        self.last_route: ModelRoute | None = None
        self.cloud_spend = 0.0
        self.cloud_latency_seconds: list[float] = []

    @staticmethod
    def _inference_endpoint(
        router_url: str | None,
    ) -> str | None:
        if not router_url:
            return None

        endpoint = router_url.rstrip("/")

        if endpoint.endswith("/route/inference"):
            return endpoint

        if endpoint.endswith("/route"):
            return f"{endpoint}/inference"

        return f"{endpoint}/route/inference"

    def _route_request(
        self,
        attempt: int,
        needs_strong_model: bool,
    ) -> dict[str, Any] | None:
        if not self.router_url:
            return None

        payload = {
            "request": (
                f"{self.task_type} for {self.caller_agent}"
            ),
            "caller_agent": self.caller_agent,
            "task_type": self.task_type,
            "attempt": max(1, int(attempt)),
            "complexity": (
                "heavy"
                if needs_strong_model
                else "medium"
            ),
            "privacy_sensitive": self.privacy_sensitive,
            "needs_strong_model": bool(needs_strong_model),
            "allowed_providers": sorted(self.policy.allowed_providers),
            "required_capabilities": sorted(self.policy.required_capabilities),
            "cloud_budget_remaining": (
                max(0.0, self.policy.max_cloud_cost - self.cloud_spend)
                if self.policy.max_cloud_cost is not None else None
            ),
        }

        request = urllib.request.Request(
            self.router_url,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                value = json.load(response)

        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
            ValueError,
        ):
            return None

        return value if isinstance(value, dict) else None

    @staticmethod
    def _normalise_target(
        value: dict[str, Any],
    ) -> str:
        target = str(
            value.get("execution_target")
            or value.get("provider")
            or "ollama"
        ).lower()

        if target in {
            "local",
            "ollama",
            "local_ollama",
        }:
            return "ollama"

        if target in {
            "cloud",
            "openrouter",
            "open_router",
        }:
            return "openrouter"

        return "ollama"

    @staticmethod
    def _with_model(
        backend: HTTPBackend,
        model: str,
    ) -> HTTPBackend:
        if not model or model == backend.model:
            return backend

        return type(backend)(
            backend.base_url,
            model,
            backend.api_key,
            backend.timeout,
            backend.retries,
            backend.circuit_seconds,
        )

    def _provider(self, backend: HTTPBackend | None) -> str:
        return "ollama" if backend is self.local else "openrouter"

    def _policy_allows(self, backend: HTTPBackend | None) -> bool:
        if backend is None:
            return False
        provider = self._provider(backend)
        if provider not in self.policy.allowed_providers:
            return False
        if provider == "ollama":
            return True
        if self.privacy_sensitive:
            return False
        if self.policy.max_cloud_cost is not None and self.cloud_spend >= self.policy.max_cloud_cost:
            return False
        if (
            self.policy.max_latency_seconds is not None
            and self.cloud_latency_seconds
            and sum(self.cloud_latency_seconds[-3:]) / len(self.cloud_latency_seconds[-3:])
            > self.policy.max_latency_seconds
        ):
            return False
        capabilities = set(getattr(backend, "capabilities", {"text", "code"}))
        return self.policy.required_capabilities.issubset(capabilities)

    def _available(self, backend: HTTPBackend | None) -> bool:
        """Return whether a backend can be attempted without tripping its breaker."""
        return self._policy_allows(backend) and not getattr(backend, "circuit_open", False)

    def _first_available(self, *backends: HTTPBackend | None) -> HTTPBackend | None:
        return next((backend for backend in backends if self._available(backend)), None)

    def _fallback_chain(self, *backends: HTTPBackend | None):
        """Build an availability chain ending at the local backend when supplied."""
        chain = None
        for backend in reversed(backends):
            if self._available(backend):
                # The outer chain records the final response once. Nested
                # callbacks would double-count cloud spend and latency when a
                # request walks through more than one availability fallback.
                chain = _FailoverBackend(backend, chain)
        return chain

    def _set_route(
        self,
        *,
        provider: str,
        model: str,
        location: str,
        reason: str,
        attempt: int,
        fallback: bool = False,
    ) -> None:
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

    def choose(
        self,
        attempt: int,
        needs_strong_model: bool = False,
    ):
        """Choose the deterministic inference tier."""

        attempt = max(1, int(attempt))

        # The Routing Agent remains useful for policy observability,
        # but the retry number determines the model tier.
        policy = self._route_request(
            attempt,
            needs_strong_model,
        )

        router_reason = (
            str(policy.get("reason"))
            if policy and policy.get("reason")
            else None
        )

        if self.privacy_sensitive:
            primary = self.local

            self._set_route(
                provider="ollama",
                model=primary.model,
                location="local",
                reason=(
                    "Privacy-sensitive request forced to "
                    "local Ollama."
                ),
                attempt=attempt,
            )

            return _FailoverBackend(
                primary,
                None,
                on_response=self._record_response,
            )

        if "ollama" not in self.policy.allowed_providers:
            primary = self._first_available(self.cloud, self.premium_cloud)
            if primary is None:
                raise BackendError("routing policy disallows local Ollama and no cloud backend is available")
            self._set_route(
                provider="openrouter", model=primary.model, location="cloud",
                reason="Per-agent routing policy requires an allowed cloud provider.",
                attempt=attempt,
            )
            return _FailoverBackend(primary, None, on_response=self._record_response)

        # Normal local tier.
        if attempt < self.escalate_after:
            primary = self.local

            self._set_route(
                provider="ollama",
                model=primary.model,
                location="local",
                reason=(
                    "Routing Agent unavailable; using local Ollama."
                    if policy is None and self.router_url
                    else (
                        f"{router_reason} Cloud request deferred until "
                        f"attempt {self.escalate_after}."
                        if (
                            policy
                            and self._normalise_target(policy) == "openrouter"
                        )
                        else (
                            router_reason
                            or "Local inference tier."
                        )
                    )
                ),
                attempt=attempt,
                fallback=(
                    policy is None
                    and bool(self.router_url)
                ),
            )

            # Availability failure is different from poor model
            # output. If Ollama cannot be reached, immediately use
            # the inexpensive cloud backend.
            # Never hand an open paid backend to the failover path. The local
            # request should remain useful even while an OpenRouter key or
            # billing problem is cooling down.
            fallback = self._fallback_chain(self.cloud, self.premium_cloud)

            def note_local_failover(
                backend: HTTPBackend,
            ) -> None:
                self._set_route(
                    provider="openrouter",
                    model=backend.model,
                    location="cloud",
                    reason=(
                        "Local Ollama failed or is unavailable; using "
                        "standard OpenRouter availability "
                        "fallback."
                    ),
                    attempt=attempt,
                    fallback=True,
                )

            return _FailoverBackend(
                primary,
                fallback,
                note_local_failover,
                self._record_response,
            )

        # First scheduled cloud tier.
        if self.cloud is not None and self._policy_allows(self.cloud) and getattr(self.cloud, "circuit_open", False):
            primary = self.local
            self._set_route(
                provider="ollama", model=primary.model, location="local",
                reason=("Standard cloud circuit is open; continuing with local "
                        f"Ollama for {getattr(self.cloud, 'circuit_retry_seconds', 0)}s."),
                attempt=attempt, fallback=True,
            )
            return _FailoverBackend(primary, None, on_response=self._record_response)

        if (
            attempt == self.escalate_after
            and self._policy_allows(self.cloud)
        ):
            primary = self.cloud

            self._set_route(
                provider="openrouter",
                model=primary.model,
                location="cloud",
                reason=(
                    "Local attempt budget exhausted; "
                    "standard cloud tier selected."
                ),
                attempt=attempt,
            )

            def note_standard_failover(
                backend: HTTPBackend,
            ) -> None:
                self._set_route(
                    provider="openrouter",
                    model=backend.model,
                    location="cloud",
                    reason=(
                        "Standard OpenRouter backend "
                        "unavailable; using premium "
                        "availability fallback."
                    ),
                    attempt=attempt,
                    fallback=True,
                )

            return _FailoverBackend(
                primary,
                self._fallback_chain(self.premium_cloud, self.local),
                note_standard_failover,
                self._record_response,
            )

        # Premium rescue tier.
        if (
            attempt > self.escalate_after
            and self._policy_allows(self.premium_cloud)
        ):
            primary = self.premium_cloud

            self._set_route(
                provider="openrouter",
                model=primary.model,
                location="cloud",
                reason=(
                    "Standard tier did not resolve the task; "
                    "premium cloud tier selected."
                ),
                attempt=attempt,
            )

            return _FailoverBackend(
                primary,
                self._fallback_chain(self.cloud, self.local),
                on_response=self._record_response,
            )

        # Premium tier missing: continue with standard cloud.
        if self._policy_allows(self.cloud):
            primary = self.cloud

            self._set_route(
                provider="openrouter",
                model=primary.model,
                location="cloud",
                reason=(
                    "Premium cloud tier is not configured; "
                    "continuing with standard cloud."
                ),
                attempt=attempt,
            )

            return _FailoverBackend(
                primary,
                self._fallback_chain(self.local),
                on_response=self._record_response,
            )

        # No cloud backend configured.
        primary = self.local

        self._set_route(
            provider="ollama",
            model=primary.model,
            location="local",
            reason=(
                "Cloud tiers are not configured; "
                "remaining on local Ollama."
            ),
            attempt=attempt,
        )

        return _FailoverBackend(
            primary,
            None,
            on_response=self._record_response,
        )

    def health(self) -> dict[str, Any]:
        """Return a fast provider health snapshot for operators and probes."""
        result: dict[str, Any] = {"caller_agent": self.caller_agent,
                                  "cloud_spend": self.cloud_spend, "backends": {}}
        for name, backend in (("ollama", self.local), ("openrouter", self.cloud),
                              ("openrouter_premium", self.premium_cloud)):
            if backend is None:
                continue
            probe = getattr(backend, "health_probe", None)
            result["backends"][name] = probe() if probe else {
                "ok": not getattr(backend, "circuit_open", False),
                "status": "circuit_open" if getattr(backend, "circuit_open", False) else "unknown",
            }
        return result

    def _record_response(
        self,
        response: LLMResponse,
    ) -> None:
        if self.last_route is None:
            return

        self.last_route.model = response.model
        self.last_route.provider = response.backend
        self.last_route.latency_seconds = (
            response.latency_seconds
        )
        self.last_route.usage = response.usage

        if (
            response.backend == "openrouter"
            and response.usage
        ):
            for key in (
                "cost",
                "total_cost",
                "estimated_cost",
            ):
                if key not in response.usage:
                    continue

                try:
                    self.last_route.estimated_cloud_cost = (
                        float(response.usage[key])
                    )
                except (TypeError, ValueError):
                    pass

                break

            if self.last_route.estimated_cloud_cost is not None:
                self.cloud_spend += max(0.0, self.last_route.estimated_cloud_cost)
            self.cloud_latency_seconds.append(max(0.0, float(response.latency_seconds or 0)))

        self.last_route.timestamp = datetime.now(UTC)
