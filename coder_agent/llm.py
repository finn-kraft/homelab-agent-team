"""Compatibility imports; shared model routing lives in agent_core."""

from agent_core.llm import (BackendError, HTTPBackend, LLMResponse, OllamaBackend,
                            OpenRouterBackend, Router)

__all__ = ["BackendError", "HTTPBackend", "LLMResponse", "OllamaBackend",
           "OpenRouterBackend", "Router"]
