from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


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
    def __init__(self, local: OllamaBackend, cloud: OpenRouterBackend | None = None,
                 escalate_after: int = 3):
        self.local, self.cloud, self.escalate_after = local, cloud, escalate_after

    def choose(self, attempt: int, needs_strong_model: bool = False):
        if self.cloud and (needs_strong_model or attempt >= self.escalate_after):
            return self.cloud
        return self.local

