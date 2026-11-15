from __future__ import annotations
from pathlib import Path
from helpers import execute_strategy, make_plan, make_records, read_jsonl
INSERT_AROUND_CURSOR = """
version: 1
before_request:
  - at: 2
    mutations:
      - type: insert
        id: itm-0901
        sort_key: 14
        title: Before cursor
        category: inserted
      - type: insert
        id: itm-0902
        sort_key: 18
        title: After cursor
        category: inserted
"""
CHANGE_OBSERVED_KEY = """
version: 1
before_request:
  - at: 2
    mutations:
      - type: update_sortkey
        id: itm-0001
        sort_key: 35
"""
DELETE_TAIL = """
version: 1
before_request:
  - at: 3
    mutations:
      - type: delete
        id: itm-0010
      - type: delete
        id: itm-0011
      - type: delete
        id: itm-0012
"""

def test_keyset_cursor_comes_from_last_observed_key(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="keyset-basic")
    result = execute_strategy(plan, "keyset", make_records(12), tmp_path / "keyset-basic")
    requests = read_jsonl(result["run_dir"] / "requests.jsonl")
    assert [request["query"]["after_key"] for request in requests] == ["", 16, 25, 34]
    assert result["audit"]["termination_proof"]["matched_conditions"] == ["exhausted_by_max_key"]
    assert result["audit"]["summary"]["verdict"] == "safe"

def test_insert_below_cursor_does_not_create_a_duplicate(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="keyset-insert", schedule_text=INSERT_AROUND_CURSOR)
    result = execute_strategy(plan, "keyset", make_records(12), tmp_path / "keyset-insert")
    records = read_jsonl(result["run_dir"] / "records.jsonl")
    ids = [row["id"] for row in records]
    audit = result["audit"]
    assert ids.count("itm-0001") == 1
    assert "itm-0901" not in ids
    assert "itm-0902" in ids
    assert audit["summary"]["findings_by_code"]["DRIFT_DUP"] == 0
    extra = next(item for item in audit["findings"] if item["code"] == "DRIFT_INSERT_OBSERVED")
    assert extra["natural_key"] == "itm-0902"
    assert extra["is_defect"] is False

def test_changed_key_on_observed_item_is_duplicate_and_oor(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="keyset-oor", schedule_text=CHANGE_OBSERVED_KEY)
    result = execute_strategy(plan, "keyset", make_records(12), tmp_path / "keyset-oor")
    audit = result["audit"]
    assert audit["summary"]["findings_by_code"]["DRIFT_DUP"] >= 1
    assert audit["summary"]["findings_by_code"]["DRIFT_OOR"] >= 1
    oor = next(item for item in audit["findings"] if item["code"] == "DRIFT_OOR")
    assert "changed sort_key" in oor["detail"]
    assert audit["summary"]["verdict"] == "unsafe"

def test_deleted_tail_stops_keyset_early(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="keyset-tail", schedule_text=DELETE_TAIL)
    result = execute_strategy(plan, "keyset", make_records(12), tmp_path / "keyset-tail")
    audit = result["audit"]
    assert audit["termination_proof"]["stop_reason"] in {"saw_empty_page", "partial_page_lt_limit"}
    assert audit["summary"]["findings_by_code"]["DRIFT_PREMATURE_TERM"] >= 1
    assert audit["summary"]["findings_by_code"]["DRIFT_TAIL_TRUNC"] == 1
    assert audit["summary"]["max_observed_key"] < audit["summary"]["universe_max_key"]
