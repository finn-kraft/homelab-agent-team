from __future__ import annotations
import json
import urllib.error
import urllib.request

class TelemetryClient:
    """Read trusted JSON telemetry; never exposes remote command execution."""
    def __init__(self, gpu_url=None, gpu_token=None, ollama_url=None, timeout=2):
        self.gpu_url, self.gpu_token = gpu_url, gpu_token
        self.ollama_url, self.timeout = (ollama_url or "").rstrip("/"), timeout
    def _get(self, url, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=self.timeout
            ) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError):
            return {"status": "unavailable"}
    def snapshot(self):
        return {
            "gpu": self._get(self.gpu_url, self.gpu_token) if self.gpu_url else {"status": "not_configured"},
            "ollama": self._get(f"{self.ollama_url}/api/ps") if self.ollama_url else {"status": "not_configured"},
        }
