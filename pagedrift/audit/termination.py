"""Termination proof: map observed stop signals to declared conditions."""
from __future__ import annotations
from typing import Any

def _condition_observed(condition: str, observation: dict[str, Any]) -> bool:
    return {
        "saw_empty_page": bool(observation.get("last_page_empty")),
        "partial_page_lt_limit": bool(observation.get("last_page_short")),
        "exhausted_by_max_key": bool(observation.get("keyset_reached_max")),
    }.get(condition, False)

def evaluate_termination(run: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    observation = dict(run.get("termination_observation") or {})
    allowed = list((plan.get("termination") or {}).get("allow") or [])
    matched: list[str] = []
    rejected: list[str] = []
    observed_but_not_declared: list[str] = []
    for condition in allowed:
        if _condition_observed(condition, observation):
            matched.append(condition)
        else:
            rejected.append(f"{condition} was not observed on the final page")
    for condition in ("saw_empty_page", "partial_page_lt_limit", "exhausted_by_max_key"):
        if _condition_observed(condition, observation) and condition not in allowed:
            observed_but_not_declared.append(condition)
    status = run.get("status", "completed")
    if status == "failed":
        rejected.append("the collector run failed before a valid terminal observation")
    if observation.get("stopped_at_max_pages"):
        rejected.append("max_pages limit reached before an accepted termination condition")
    satisfied = bool(matched) and status != "failed" and not observation.get("stopped_at_max_pages")
    if observation.get("last_page_empty"):
        stop_reason = "saw_empty_page"
    elif observation.get("last_page_short"):
        stop_reason = "partial_page_lt_limit"
    elif observation.get("keyset_reached_max"):
        stop_reason = "exhausted_by_max_key"
    elif observation.get("stopped_at_max_pages"):
        stop_reason = "max_pages_limit"
    elif status == "failed":
        stop_reason = "collector_failed"
    else:
        stop_reason = "unknown"
    return {
        "satisfied": satisfied,
        "declared_conditions": allowed,
        "matched_conditions": matched,
        "observed_but_not_declared": observed_but_not_declared,
        "rejected_because": rejected,
        "stop_reason": stop_reason,
        "observation": observation,
    }
