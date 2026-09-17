from agent_core.evals import run_evaluations


def test_fixed_model_contract_fixtures_pass():
    result = run_evaluations()
    assert result["passed"] is True
    assert result["cases"] >= 6
