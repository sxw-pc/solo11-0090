from __future__ import annotations
from pagedrift.audit.termination import evaluate_termination
PLAN = {
    "termination": {
        "allow": ["saw_empty_page", "partial_page_lt_limit", "exhausted_by_max_key"]
    }
}

def _run(**observation):
    base = {
        "status": "completed",
        "termination_observation": {
            "last_page_empty": False,
            "last_page_short": False,
            "keyset_reached_max": False,
            "stopped_at_max_pages": False,
        },
    }
    base["termination_observation"].update(observation)
    return base

def test_empty_page_matches_declared_condition() -> None:
    proof = evaluate_termination(_run(last_page_empty=True), PLAN)
    assert proof["satisfied"] is True
    assert proof["matched_conditions"] == ["saw_empty_page"]
    assert proof["stop_reason"] == "saw_empty_page"

def test_short_page_matches_declared_condition() -> None:
    proof = evaluate_termination(_run(last_page_short=True), PLAN)
    assert proof["satisfied"] is True
    assert proof["matched_conditions"] == ["partial_page_lt_limit"]

def test_max_key_matches_declared_condition() -> None:
    proof = evaluate_termination(_run(keyset_reached_max=True), PLAN)
    assert proof["satisfied"] is True
    assert proof["matched_conditions"] == ["exhausted_by_max_key"]

def test_observed_but_undeclared_condition_is_not_a_proof() -> None:
    plan = {"termination": {"allow": ["saw_empty_page"]}}
    proof = evaluate_termination(_run(last_page_short=True), plan)
    assert proof["satisfied"] is False
    assert proof["observed_but_not_declared"] == ["partial_page_lt_limit"]
    assert proof["matched_conditions"] == []

def test_max_pages_is_never_a_proof() -> None:
    proof = evaluate_termination(_run(stopped_at_max_pages=True), PLAN)
    assert proof["satisfied"] is False
    assert proof["stop_reason"] == "max_pages_limit"
    assert any("max_pages" in reason for reason in proof["rejected_because"])

def test_failed_run_is_never_a_proof() -> None:
    run = _run(last_page_empty=True)
    run["status"] = "failed"
    proof = evaluate_termination(run, PLAN)
    assert proof["satisfied"] is False
    assert proof["stop_reason"] == "saw_empty_page"
    assert any("failed" in reason for reason in proof["rejected_because"])

def test_unknown_observation_has_unknown_stop_reason() -> None:
    proof = evaluate_termination(_run(), PLAN)
    assert proof["satisfied"] is False
    assert proof["stop_reason"] == "unknown"
