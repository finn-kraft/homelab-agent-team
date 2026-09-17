from orchestrator.state import canonical_agent, canonical_next_state, canonical_phase


def test_legacy_vocabulary_normalizes_to_engineering():
    assert canonical_agent("coder-agent") == "engineering-agent"
    assert canonical_phase("coding") == "engineering"
    assert canonical_next_state("coder_revision") == "engineering_revision"
