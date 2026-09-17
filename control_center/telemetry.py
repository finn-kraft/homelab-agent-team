from __future__ import annotations
import json
import csv
import io
import shutil
import subprocess
import urllib.error
import urllib.request

class TelemetryClient:
    """Read trusted JSON telemetry; never exposes remote command execution."""
    def __init__(self, gpu_url=None, gpu_token=None, ollama_url=None, router_url=None, timeout=2):
        self.gpu_url, self.gpu_token = gpu_url, gpu_token
        self.ollama_url, self.timeout = (ollama_url or "").rstrip("/"), timeout
        self.router_url = (router_url or "").rstrip("/")
    def _get(self, url, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=self.timeout
            ) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError):
            return {"status": "unavailable"}

    @staticmethod
    def _local_gpu():
        """Read only fixed GPU fields; never accepts a command from configuration."""
        if not shutil.which("nvidia-smi"):
            return {"status": "not_configured"}
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2, check=False, shell=False,
            )
            row = next(csv.reader(io.StringIO(result.stdout)), None)
            if result.returncode != 0 or not row or len(row) < 6:
                return {"status": "unavailable"}
            name, utilization, used, total, temperature, power = [item.strip() for item in row[:6]]
            return {"status": "online", "name": name,
                    "utilization_percent": float(utilization), "vram_used_mb": float(used),
                    "vram_total_mb": float(total), "temperature_c": float(temperature),
                    "power_w": float(power)}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"status": "unavailable"}

    @staticmethod
    def _normalise_gpu(value):
        if not isinstance(value, dict):
            return {"status": "unavailable"}
        value = value.get("gpu", value)
        aliases = {
            "utilization_percent": ("utilization_percent", "utilization"),
            "vram_used_mb": ("vram_used_mb", "memory_used_mib", "memory_used_mb"),
            "vram_total_mb": ("vram_total_mb", "memory_total_mib", "memory_total_mb"),
            "temperature_c": ("temperature_c", "temperature"),
            "power_w": ("power_w", "power"),
        }
        result = {"status": value.get("status", "online" if value.get("name") else "unavailable"),
                  "name": value.get("name") or value.get("gpu_name")}
        for target, keys in aliases.items():
            for key in keys:
                if value.get(key) is not None:
                    result[target] = value[key]
                    break
        if value.get("context_length") is not None:
            result["context_length"] = value["context_length"]
        return result
    def snapshot(self):
        ollama = self._get(f"{self.ollama_url}/api/ps") if self.ollama_url else {"status": "not_configured"}
        if "models" in ollama:
            ollama["status"] = "online"
            ollama["loaded_model"] = ollama["models"][0] if ollama["models"] else None
            loaded = ollama["loaded_model"] or {}
            ollama["model"] = loaded.get("name") or loaded.get("model")
            ollama["context_length"] = loaded.get("context_length")
            ollama["size_vram"] = loaded.get("size_vram")
            ollama["online"] = True
        else:
            ollama["online"] = ollama.get("status") == "online"
        gpu = self._normalise_gpu(
            self._get(self.gpu_url, self.gpu_token) if self.gpu_url else self._local_gpu()
        )
        return {
            "gpu": gpu,
            "ollama": ollama,
            "routing_agent": self._get(f"{self.router_url}/health") if self.router_url else {"status": "not_configured"},
        }
