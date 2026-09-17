from orchestrator.store import OrchestratorStore


def test_control_actions_are_state_aware():
    assert OrchestratorStore.allowed_control_actions("running") == {"pause", "cancel"}
    assert OrchestratorStore.allowed_control_actions("paused") == {"resume", "cancel"}
    assert OrchestratorStore.allowed_control_actions("blocked") == {"resume", "cancel"}
    assert OrchestratorStore.allowed_control_actions("needs_human") == {"cancel"}
    assert OrchestratorStore.allowed_control_actions("complete") == set()
    assert OrchestratorStore.allowed_control_actions("cancelled") == set()
