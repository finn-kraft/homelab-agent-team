from control_center.telemetry import TelemetryClient


def test_ollama_snapshot_contains_loaded_installed_and_version_stats(monkeypatch):
    client = TelemetryClient(
        gpu_url="http://gpu/v1/gpu",
        ollama_url="http://ollama:11434",
        router_url="http://router:8090",
    )

    def fake_get(url, token=None):
        if url.endswith("/api/ps"):
            return {"models": [{"name": "qwen2.5-coder:7b", "size": 2_000_000,
                                "size_vram": 1_500_000, "expires_at": "soon",
                                "details": {"context_length": 32768}}]}
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.2:latest"}]}
        if url.endswith("/api/version"):
            return {"version": "0.12.3"}
        if url.endswith("/health"):
            return {"status": "online"}
        return {"status": "online", "name": "GTX 1070", "utilization_percent": 42,
                "vram_used_mb": 1000, "vram_total_mb": 8192, "temperature_c": 61,
                "power_w": 90}

    monkeypatch.setattr(client, "_get", fake_get)
    snapshot = client.snapshot()
    ollama = snapshot["ollama"]
    assert ollama["online"] is True
    assert ollama["version"] == "0.12.3"
    assert ollama["loaded_count"] == 1
    assert ollama["model_count"] == 2
    assert ollama["size_vram"] == 1_500_000
    assert ollama["context_length"] == 32768
    assert snapshot["gpu"]["temperature_c"] == 61
    assert snapshot["observed_at"]


def test_dashboard_uses_one_second_telemetry_polling():
    from importlib.resources import files

    script = files("control_center.static").joinpath("app.js").read_text()
    assert "api('/api/telemetry')" in script
    assert "setInterval(() => refreshTelemetry(generation), 1000)" in script
