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


def test_control_actions_record_operation_lifecycle():
    class OperationStore(Store):
        def __init__(self):
            super().__init__(); self.operation_updates = []
        def begin_operation(self, job, action, requested_by):
            self.calls.append((job, action, requested_by)); return "op-123"
        def update_operation(self, operation_id, status, detail=None, error=None):
            self.operation_updates.append((operation_id, status, detail, error))

    store = OperationStore()
    value = ControlCenter(store, Telemetry(), Auth()).perform_action(
        7, "cancel", {"confirm": True}
    )
    assert value["operation_id"] == "op-123"
    assert value["operation_status"] == "applied"
    assert [item[1] for item in store.operation_updates] == ["accepted", "applied"]


def test_control_action_failure_is_bound_to_operation():
    class FailingStore(Store):
        def begin_operation(self, *_args): return "op-failed"
        def update_operation(self, operation_id, status, detail=None, error=None):
            self.calls.append((operation_id, status, error))
        def action(self, *_args): raise RuntimeError("lease is still held")

    from control_center.app import OperationFailed
    store = FailingStore()
    with pytest.raises(OperationFailed) as raised:
        ControlCenter(store, Telemetry(), Auth()).perform_action(7, "pause", {})
    assert raised.value.operation_id == "op-failed"
    assert store.calls[-1][1] == "failed"

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
    assert "engineering-agent-v3-controls" in html

def test_dashboard_uses_accessible_delegated_controls():
    from importlib.resources import files
    html = files("control_center.static").joinpath("index.html").read_text()
    script = files("control_center.static").joinpath("app.js").read_text()
    assert "aria-label=\"Primary navigation\"" in html
    assert "onclick=" not in html and "onclick=\"" not in script
    assert "data-action=\"job-action\"" in script
    assert "event.preventDefault()" in script and "event.stopPropagation()" in script
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


def test_health_exposes_schema_migration_state():
    from control_center.store import ControlStore

    class Workflow:
        def schema_readiness(self):
            return {"ready": False, "migration_required": True,
                    "missing_tables": ["jobs"], "missing_columns": {}}

    value = ControlStore.__new__(ControlStore)
    value.workflow = Workflow()
    report = value.health()
    assert report["ready"] is False
    assert report["migration_required"] is True
    assert value.healthy() is False


def test_health_marks_database_unavailable_when_readiness_cannot_connect():
    from control_center.store import ControlStore

    class Workflow:
        def schema_readiness(self):
            return {"ready": False, "migration_required": True,
                    "missing_tables": [], "missing_columns": {},
                    "error": "connection refused"}

    value = ControlStore.__new__(ControlStore)
    value.workflow = Workflow()
    assert value.health()["database"] == "unavailable"
