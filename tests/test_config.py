from __future__ import annotations
from pathlib import Path
import pytest
import yaml
from pagedrift.config import PlanError, load_plan
from helpers import make_plan, write_schedule

def _codes(exc: PlanError) -> set[str]:
    return {item["code"] for item in exc.issues}

def test_valid_plan_normalizes_defaults(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    assert plan.schedule_path.is_file()
    assert len(plan.sha256) == 64
    assert len(plan.schedule_sha256) == 64
    assert plan.source["offset_param"] == "offset"
    assert plan.audit["max_dup_ratio"] == 0.0
    assert set(plan.strategies) == {"offset", "pagenum", "keyset", "snapshot"}
    assert plan.base_url == "http://127.0.0.1:8765"

def test_missing_top_level_fields_are_all_reported(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(PlanError) as caught:
        load_plan(path)
    codes = _codes(caught.value)
    assert "PLAN_MISSING_FIELD" in codes
    paths = {issue["path"] for issue in caught.value.issues}
    assert {"dataset", "source", "strategies", "mutation_schedule", "termination", "audit"} <= paths

def test_unknown_strategy_is_rejected(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    raw = yaml.safe_load(plan.path.read_text(encoding="utf-8"))
    raw["strategies"]["scroll"] = {"limit": 3}
    plan.path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(PlanError) as caught:
        load_plan(plan.path)
    assert "PLAN_UNKNOWN_STRATEGY" in _codes(caught.value)

def test_bad_limit_and_missing_page_size(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    raw = yaml.safe_load(plan.path.read_text(encoding="utf-8"))
    raw["strategies"]["offset"]["limit"] = 0
    raw["strategies"]["pagenum"].pop("page_size")
    plan.path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(PlanError) as caught:
        load_plan(plan.path)
    assert "PLAN_BAD_LIMIT" in _codes(caught.value)
    assert any(issue["path"] == "strategies.pagenum.page_size" for issue in caught.value.issues)

def test_bad_termination_condition(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    raw = yaml.safe_load(plan.path.read_text(encoding="utf-8"))
    raw["termination"]["allow"].append("trust_me")
    plan.path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(PlanError) as caught:
        load_plan(plan.path)
    assert "PLAN_BAD_TERMINATION" in _codes(caught.value)

def test_non_loopback_source_is_rejected_before_collection(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    raw = yaml.safe_load(plan.path.read_text(encoding="utf-8"))
    raw["source"]["base_url"] = "http://target.invalid/items"
    plan.path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(PlanError) as caught:
        load_plan(plan.path)
    issue = next(item for item in caught.value.issues if item["code"] == "PLAN_NON_LOOPBACK_SOURCE")
    assert issue["path"] == "source.base_url"

def test_localhost_and_ipv6_loopback_are_accepted(tmp_path: Path) -> None:
    for index, host in enumerate(("localhost", "127.0.0.1", "[::1]")):
        plan = make_plan(tmp_path, name=f"loopback{index}")
        raw = yaml.safe_load(plan.path.read_text(encoding="utf-8"))
        raw["source"]["base_url"] = f"http://{host}:8765"
        plan.path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        loaded = load_plan(plan.path)
        assert loaded.base_url.endswith(":8765")

def test_missing_schedule_file(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    raw = yaml.safe_load(plan.path.read_text(encoding="utf-8"))
    raw["mutation_schedule"] = "does-not-exist.yaml"
    plan.path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(PlanError) as caught:
        load_plan(plan.path)
    assert any(issue["path"] == "mutation_schedule" for issue in caught.value.issues)

def test_plan_fingerprint_is_raw_file_bytes(tmp_path: Path) -> None:
    schedule = write_schedule(tmp_path)
    plan = make_plan(tmp_path)
    first = load_plan(plan.path)
    second = load_plan(plan.path)
    assert first.sha256 == second.sha256
    plan.path.write_text(plan.path.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
    changed = load_plan(plan.path)
    assert changed.sha256 != first.sha256
    assert schedule.is_file()
