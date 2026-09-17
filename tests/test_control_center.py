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
    from control_center.app import HTML
    assert "shell" not in HTML.lower() and "filesystem" not in HTML.lower()
