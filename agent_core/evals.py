"""Small fixed-fixture contract evaluations for the three model-facing agents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from coder_agent.agent import EngineeringAgent
from planner_agent.decision import InvalidDecision, parse_decision
from reviewer_agent.decision import InvalidReview, parse_review


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    fixture = Path(path) if path else Path(__file__).parent / "fixtures" / "core.json"
    if not fixture.exists() and path is None:
        fixture = Path(__file__).parent.parent / "evals" / "fixtures" / "core.json"
    value = json.loads(fixture.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("model evaluation fixture must be a JSON list")
    return [case for case in value if isinstance(case, dict)]


def _implementation_contract(payload: str) -> tuple[bool, str]:
    try:
        action = EngineeringAgent._parse_action(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        return False, str(exc)
    if action.get("action") in {"read", "write", "delete"}:
        path = str(action.get("path", ""))
        candidate = Path(path)
        if not path or candidate.is_absolute() or "\x00" in path or ".." in candidate.parts:
            return False, "path must remain relative to the repository"
    return True, "valid Engineering action"


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    kind = str(case.get("kind", ""))
    payload = json.dumps(case.get("payload")) if not isinstance(case.get("payload"), str) else case["payload"]
    expected_invalid = bool(case.get("expected_invalid", False))
    criteria = [str(value) for value in case.get("criteria", [])]
    try:
        if kind == "planner":
            parse_decision(payload)
        elif kind == "engineering":
            valid, detail = _implementation_contract(payload)
            if not valid:
                raise ValueError(detail)
        elif kind == "reviewer":
            parse_review(payload, criteria)
        else:
            raise ValueError(f"unknown evaluation kind: {kind}")
        valid = True
        detail = "contract accepted"
    except (InvalidDecision, InvalidReview, ValueError, json.JSONDecodeError) as exc:
        valid = False
        detail = str(exc)
    passed = valid != expected_invalid
    return {"name": case.get("name", "unnamed"), "kind": kind,
            "passed": passed, "valid": valid, "detail": detail}


def run_evaluations(path: str | Path | None = None) -> dict[str, Any]:
    results = [evaluate_case(case) for case in load_cases(path)]
    return {"passed": all(item["passed"] for item in results),
            "cases": len(results), "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-model-evals")
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args(argv)
    result = run_evaluations(args.fixture)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
