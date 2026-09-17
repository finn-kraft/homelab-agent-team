from __future__ import annotations
import json
import csv
import io
import shutil
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
        """Collect all read-only telemetry concurrently for fast one-second polling."""
        if self.ollama_url:
            with ThreadPoolExecutor(max_workers=5, thread_name_prefix="telemetry") as pool:
                ps_future = pool.submit(self._get, f"{self.ollama_url}/api/ps")
                tags_future = pool.submit(self._get, f"{self.ollama_url}/api/tags")
                version_future = pool.submit(self._get, f"{self.ollama_url}/api/version")
                gpu_future = pool.submit(
                    self._get, self.gpu_url, self.gpu_token
                ) if self.gpu_url else pool.submit(self._local_gpu)
                router_future = pool.submit(self._get, f"{self.router_url}/health") \
                    if self.router_url else None
                ps = ps_future.result()
                tags = tags_future.result()
                version = version_future.result()
                gpu_raw = gpu_future.result()
                router = router_future.result() if router_future else {"status": "not_configured"}
        else:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="telemetry") as pool:
                gpu_future = pool.submit(self._get, self.gpu_url, self.gpu_token) \
                    if self.gpu_url else pool.submit(self._local_gpu)
                router_future = pool.submit(self._get, f"{self.router_url}/health") \
                    if self.router_url else None
                ps, tags, version = {"status": "not_configured"}, {}, {}
                gpu_raw = gpu_future.result()
                router = router_future.result() if router_future else {"status": "not_configured"}

        ollama = dict(ps) if isinstance(ps, dict) else {"status": "unavailable"}
        ps_online = "models" in ollama
        loaded_models = ollama.get("models") if isinstance(ollama.get("models"), list) else []
        installed_models = tags.get("models") if isinstance(tags, dict) and isinstance(tags.get("models"), list) else []
        ollama["loaded_models"] = loaded_models
        ollama["models"] = installed_models or loaded_models
        ollama["model_count"] = len(ollama["models"])
        ollama["loaded_count"] = len(loaded_models)
        if loaded_models:
            ollama["status"] = "online"
            ollama["loaded_model"] = loaded_models[0]
            loaded = ollama["loaded_model"] if isinstance(ollama["loaded_model"], dict) else {}
            details = loaded.get("details") if isinstance(loaded.get("details"), dict) else {}
            ollama["model"] = loaded.get("name") or loaded.get("model")
            ollama["context_length"] = loaded.get("context_length") or details.get("context_length")
            ollama["size"] = loaded.get("size")
            ollama["size_vram"] = loaded.get("size_vram")
            ollama["expires_at"] = loaded.get("expires_at")
            ollama["processor"] = loaded.get("processor")
            ollama["online"] = True
        else:
            ollama["status"] = "online" if ps_online else ollama.get("status", "unavailable")
            ollama["online"] = ps_online or ollama.get("status") == "online"
        if isinstance(version, dict) and version.get("version"):
            ollama["version"] = version["version"]
        gpu = self._normalise_gpu(gpu_raw)
        return {
            "gpu": gpu,
            "ollama": ollama,
            "routing_agent": router,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
