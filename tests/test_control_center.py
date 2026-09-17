import pytest
from control_center.app import ControlCenter

class Store:
    def overview(self): return {"jobs": [], "agents": [], "inference": {}, "repository_locks": []}
    def events(self, _filters): return []
    def action(self, _job, _action): pass

class Telemetry:
    def snapshot(self): return {"gpu": {"status": "ok"}, "ollama": {"status": "ok"}}

@pytest.fixture
def app():
    return ControlCenter(Store(), Telemetry(), "secret-token")

def test_api_authorization_is_required(app):
    assert not app.authorized("") and not app.authorized("Bearer wrong")
    assert app.authorized("Bearer secret-token")

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
