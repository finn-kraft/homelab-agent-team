import pytest
from control_center.app import ControlCenter

class Store:
    def __init__(self): self.calls = []
    def overview(self): return {"jobs": [], "agents": [], "inference": {}, "repository_locks": []}
    def events(self, _filters): return []
    def action(self, job, action): self.calls.append((job, action))
    def remove_job(self, job): self.calls.append((job, "remove"))

class Telemetry:
    def snapshot(self): return {"gpu": {"status": "ok"}, "ollama": {"status": "ok"}}

class Auth:
    secure_cookie = False
    def authenticate(self, _token): return None
    def check_csrf(self, _session, _value): return True

@pytest.fixture
def app():
    return ControlCenter(Store(), Telemetry(), Auth())

def test_cancel_requires_explicit_confirmation(app):
    with pytest.raises(PermissionError): app.perform_action(1, "cancel", {})
    assert app.perform_action(1, "cancel", {"confirm": True}) == {"status": "cancel"}
    assert app.store.calls == [(1, "cancel")]


def test_remove_requires_confirmation_and_returns_removed(app):
    with pytest.raises(PermissionError): app.perform_action(1, "remove", {})
    assert app.perform_action(1, "remove", {"confirm": True}) == {"status": "removed"}

def test_dashboard_has_no_arbitrary_command_or_filesystem_api():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    assert "shell" not in html.lower() and "filesystem" not in html.lower()

def test_static_dashboard_contains_primary_operator_workflow():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    script = files("control_center.static").joinpath("app.js").read_text()
    assert "Start a new job" in html
    assert all(stage in html for stage in ("Planner", "Engineer", "Reviewer", "Verification", "Commit"))
    assert "(Coder)" not in html and "EngineeringAgent" in script
    assert "data-job-action=\"cancel\" data-job-id" in script
    assert "/api/stream" in script and "Needs attention" in script
    assert "/api/login" in script and "Control Center password" in html
    assert "Remove from queue" in script and "data-job-action=\"remove\"" in script

def test_dashboard_uses_accessible_delegated_controls():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    script = files("control_center.static").joinpath("app.js").read_text()
    assert "aria-label=\"Primary navigation\"" in html
    assert "onclick=" not in html and "onclick=\"" not in script
    assert "data-action=\"job-action\"" in script
    assert "/api/login" in script and "credentials: 'same-origin'" in script


def test_blocked_attention_prefers_durable_step_reason():
    from control_center.store import ControlStore
    result = {
        "job": {"status": "blocked", "blocker": None},
        "steps": [],
        "current_step_detail": {"blocker": "pytest is not installed"},
        "events": [],
    }
    attention = ControlStore._attention(result)
    assert attention["reason"] == "pytest is not installed"
    assert attention["can_answer"] is False
