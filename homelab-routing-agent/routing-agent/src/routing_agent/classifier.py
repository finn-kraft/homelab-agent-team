"""LLM-backed intent classification with an offline fallback."""

import json
import os
from typing import Protocol

from .models import TaskIntent


class IntentClassifier(Protocol):
    """Classifies a request into the fields the routing policy needs."""

    def classify(self, request: str) -> TaskIntent:
        """Return the best structured interpretation of *request*."""


class KeywordClassifier:
    """Dependency-free classifier used when an LLM is not configured."""

    DOMAINS = {
        "business": ("quote", "estimate", "invoice", "customer", "tax", "business"),
        "financial": ("budget", "expense", "cash flow", "credit", "finance", "align"),
        "homelab": ("proxmox", "vm", "kubernetes", "k3s", "server", "tailscale", "network"),
        "research": ("research", "compare", "find", "recommend", "look up"),
    }

    def classify(self, request: str) -> TaskIntent:
        """Classify common homelab requests without requiring a network call."""
        text = request.lower()
        domain = next((name for name, words in self.DOMAINS.items() if any(w in text for w in words)), "general")
        urgency = 5 if any(w in text for w in ("urgent", "asap", "down", "broken", "emergency")) else 3
        complex_markers = ("architecture", "roadmap", "refactor", "build")
        complexity = "heavy" if any(w in text for w in complex_markers) else "medium"
        if len(request) < 70 and complexity != "heavy":
            complexity = "light"
        private = any(w in text for w in ("password", "tax", "customer", "invoice", "financial"))
        return TaskIntent(domain, urgency, complexity, private, domain == "research", request[:180])


class OpenAICompatibleClassifier:
    """Uses any OpenAI-compatible chat-completions API to classify requests."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model

    def classify(self, request: str) -> TaskIntent:
        """Ask the configured model for a compact JSON classification."""
        import httpx

        prompt = (
            "Classify this request for an agent router. Return only JSON with domain "
            "(business|financial|homelab|research|general), urgency (1-5), complexity "
            "(light|medium|heavy), privacy_sensitive (boolean), needs_live_data (boolean), "
            "and summary. Request: " + request
        )
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0},
            timeout=20,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return TaskIntent(**json.loads(content))


def configured_classifier() -> IntentClassifier:
    """Return the LLM classifier when configured, otherwise the offline one."""
    base_url, api_key, model = (os.getenv("ROUTER_LLM_BASE_URL"), os.getenv("ROUTER_LLM_API_KEY"), os.getenv("ROUTER_LLM_MODEL"))
    if base_url and api_key and model:
        return OpenAICompatibleClassifier(base_url, api_key, model)
    return KeywordClassifier()
