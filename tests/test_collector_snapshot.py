from __future__ import annotations
import json
import shutil
from pathlib import Path
from pagedrift.audit.engine import audit_run
from helpers import execute_strategy, make_plan, make_records, read_jsonl
MUTATE_AROUND_SNAPSHOT = """
version: 1
before_request:
  - at: 2
    mutations:
      - type: insert
        id: itm-0901
        sort_key: 14
        title: Late item
        category: inserted
      - type: delete
        id: itm-0002
  - at: 3
    mutations:
      - type: update_sortkey
        id: itm-0001
        sort_key: 60
  - at: 4
    mutations:
      - type: delete
        id: itm-0012
"""

def test_snapshot_token_is_pinned_and_all_pages_are_safe(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="snapshot-pinned", schedule_text=MUTATE_AROUND_SNAPSHOT)
    result = execute_strategy(plan, "snapshot", make_records(12), tmp_path / "snapshot-pinned")
    requests = read_jsonl(result["run_dir"] / "requests.jsonl")
    run = result["run"]
    assert requests[0]["query"]["snapshot"] == "1"
    assert all(request["query"]["snapshot"] == "snap-0001" for request in requests[1:])
    assert run["snapshot_tokens"] == ["snap-0001"] * 4
    assert result["audit"]["snapshot_integrity"]["consistent"] is True
    assert result["audit"]["summary"]["verdict"] == "safe"
    assert result["audit"]["summary"]["coverage_ratio"] == 1.0
    assert result["audit"]["summary"]["findings_by_code"]["DRIFT_DUP"] == 0
    assert len(read_jsonl(result["run_dir"] / "records.jsonl")) == 12

def test_inconsistent_snapshot_token_is_resp_invalid(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="snapshot-bad-token")
    result = execute_strategy(plan, "snapshot", make_records(9), tmp_path / "snapshot-bad-token")
    copied = tmp_path / "snapshot-bad-token-copy"
    shutil.copytree(result["run_dir"], copied)
    run_path = copied / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["snapshot_tokens"] = ["snap-0001", "snap-0002"]
    run_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    audit = audit_run(copied)
    assert audit["snapshot_integrity"]["consistent"] is False
    assert audit["summary"]["findings_by_code"]["RESP_INVALID"] == 1
    assert audit["summary"]["verdict"] == "inconclusive"
    assert audit["integrity_findings"][0]["code"] == "RESP_INVALID"

def test_snapshot_ignores_live_inserted_extras(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="snapshot-extra", schedule_text=MUTATE_AROUND_SNAPSHOT)
    result = execute_strategy(plan, "snapshot", make_records(12), tmp_path / "snapshot-extra")
    ids = [row["id"] for row in read_jsonl(result["run_dir"] / "records.jsonl")]
    assert "itm-0901" not in ids
    assert result["audit"]["summary"]["findings_by_code"]["DRIFT_INSERT_OBSERVED"] == 0
