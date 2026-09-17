import pytest
from control_center.app import ControlCenter

class Store:
    def overview(self): return {"jobs": [], "agents": [], "inference": {}, "repository_locks": []}
    def events(self, _filters): return []
    def action(self, _job, _action): pass

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

def test_dashboard_has_no_arbitrary_command_or_filesystem_api():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    assert "shell" not in html.lower() and "filesystem" not in html.lower()

def test_static_dashboard_contains_primary_operator_workflow():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    script = files("control_center.static").joinpath("app.js").read_text()
    assert "Start a new job" in html
    assert all(stage in html for stage in ("Planner", "Coder", "Reviewer", "Verification", "Commit"))
    assert "/api/stream" in script and "Needs attention" in script
    assert "/api/login" in script and "Control Center password" in html

def test_dashboard_uses_accessible_delegated_controls():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    script = files("control_center.static").joinpath("app.js").read_text()
    assert "aria-label=\"Primary navigation\"" in html
    assert "onclick=" not in html and "onclick=\"" not in script
    assert "data-action=\"job-action\"" in script
    assert "/api/login" in script and "credentials: 'same-origin'" in script
