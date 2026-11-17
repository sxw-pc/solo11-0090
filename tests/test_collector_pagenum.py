from __future__ import annotations
from pathlib import Path
from helpers import execute_strategy, make_plan, make_records, read_jsonl
DELETE_AFTER_PAGE_ONE = """
version: 1
before_request:
  - at: 2
    mutations:
      - type: delete
        id: itm-0001
"""

def test_pagenum_matches_offset_on_a_stable_universe(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="stable-shape", limit=4)
    records = make_records(11)
    offset = execute_strategy(plan, "offset", records, tmp_path / "stable-shape")
    pagenum = execute_strategy(plan, "pagenum", records, tmp_path / "stable-shape")
    offset_ids = [row["id"] for row in read_jsonl(offset["run_dir"] / "records.jsonl")]
    pagenum_ids = [row["id"] for row in read_jsonl(pagenum["run_dir"] / "records.jsonl")]
    assert offset_ids == pagenum_ids
    assert offset["audit"]["summary"]["verdict"] == "safe"
    assert pagenum["audit"]["summary"]["verdict"] == "safe"

def test_pagenum_queries_are_one_based(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="page-params", limit=3)
    result = execute_strategy(plan, "pagenum", make_records(8), tmp_path / "page-params")
    requests = read_jsonl(result["run_dir"] / "requests.jsonl")
    assert [request["query"]["page"] for request in requests] == [1, 2, 3]
    assert all(request["query"]["page_size"] == 3 for request in requests)
    assert requests[0]["first_key"] == 10

def test_pagenum_delete_before_cursor_produces_skip(tmp_path: Path) -> None:
    plan = make_plan(
        tmp_path,
        name="page-delete",
        schedule_text=DELETE_AFTER_PAGE_ONE,
        limit=3,
    )
    result = execute_strategy(plan, "pagenum", make_records(12), tmp_path / "page-delete")
    audit = result["audit"]
    assert audit["summary"]["skip_count"] >= 1
    assert audit["summary"]["findings_by_code"]["DRIFT_SKIP"] >= 1
    assert audit["summary"]["verdict"] == "unsafe"
    skip = next(item for item in audit["findings"] if item["code"] == "DRIFT_SKIP")
    assert skip["page_seq"] > 0
    assert len(skip["evidence_sha8"]) == 8

def test_pagenum_short_final_page_with_no_later_items_is_not_tail_trunc(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="page-short", limit=4)
    result = execute_strategy(plan, "pagenum", make_records(10), tmp_path / "page-short")
    audit = result["audit"]
    assert audit["termination_proof"]["matched_conditions"] == ["partial_page_lt_limit"]
    assert audit["summary"]["findings_by_code"]["DRIFT_TAIL_TRUNC"] == 0
    assert audit["summary"]["verdict"] == "safe"
