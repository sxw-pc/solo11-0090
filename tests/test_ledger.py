from __future__ import annotations
import json
import shutil
import sqlite3
from pathlib import Path
import pytest
from pagedrift.evidence import verify_run
from pagedrift.store.sqlite_ledger import Ledger
from helpers import read_json, read_jsonl

def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()

def test_ledger_has_required_tables_and_rows(demo_bundle: dict) -> None:
    root = demo_bundle["root"]
    ledger_path = root / "ledger.sqlite"
    tables = _table_names(ledger_path)
    assert {"runs", "page_fetches", "audit_findings", "strategy_summary", "audit_runs"} <= tables
    with Ledger(ledger_path) as ledger:
        summary_rows = ledger.summary_rows()
        assert len(summary_rows) == 4
        audit_count = ledger.conn.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]
        run_count = ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert audit_count == run_count == 4
        offset = next(row for row in summary_rows if row["strategy"] == "offset")
        assert offset["verdict"] == "unsafe"
        assert offset["skip_count"] == 2

def test_ledger_show_returns_pages_findings_and_summary(demo_bundle: dict) -> None:
    root = demo_bundle["root"]
    run = read_json(root / "offset" / "run.json")
    with Ledger(root / "ledger.sqlite") as ledger:
        payload = ledger.show(run["run_id"])
        hashes = ledger.page_hashes(run["run_id"])
    assert payload["run"]["strategy"] == "offset"
    assert len(payload["pages"]) == 5
    assert payload["pages"][0]["sha256"] == hashes[1]
    assert sum(1 for item in payload["findings"] if item["code"] == "DRIFT_TAIL_TRUNC") == 1
    assert payload["summary"]["coverage_ratio"] == 0.580645
    assert len(payload["audits"]) == 1
    with pytest.raises(KeyError):
        with Ledger(root / "ledger.sqlite") as ledger:
            ledger.show("run-does-not-exist")

def test_evidence_verify_ok_with_and_without_ledger(demo_bundle: dict) -> None:
    root = demo_bundle["root"]
    run_dir = root / "snapshot"
    without = verify_run(run_dir)
    with_ledger = verify_run(run_dir, root / "ledger.sqlite")
    assert without["status"] == "OK"
    assert with_ledger["status"] == "OK"
    assert without["checked_pages"] == 6
    assert with_ledger["ledger_pages"] == 6

def test_evidence_tamper_is_detected(demo_bundle: dict, tmp_path: Path) -> None:
    root = demo_bundle["root"]
    copied = tmp_path / "offset-tampered"
    shutil.copytree(root / "offset", copied)
    requests = read_jsonl(copied / "requests.jsonl")
    page_path = copied / requests[0]["evidence_file"]
    page_path.write_bytes(page_path.read_bytes().replace(b"itm-0001", b"itm-9999", 1))
    result = verify_run(copied, root / "ledger.sqlite")
    codes = {issue["code"] for issue in result["issues"]}
    assert result["status"] == "MISMATCH"
    assert "PAGE_SHA_MISMATCH" in codes
    assert "LEDGER_SHA_MISMATCH" in codes or "LEDGER_ROW_MISSING" in codes

def test_ledger_recording_is_idempotent(demo_bundle: dict, tmp_path: Path) -> None:
    root = demo_bundle["root"]
    copied_ledger = tmp_path / "copy.sqlite"
    shutil.copyfile(root / "ledger.sqlite", copied_ledger)
    run_dir = root / "offset"
    run = read_json(run_dir / "run.json")
    audit = read_json(run_dir / "audit.json")
    with Ledger(copied_ledger) as ledger:
        before = ledger.conn.execute("SELECT COUNT(*) FROM audit_findings").fetchone()[0]
        ledger.record_run(run, run_dir)
        ledger.record_audit_file(audit, run_dir)
        after = ledger.conn.execute("SELECT COUNT(*) FROM audit_findings").fetchone()[0]
        runs = ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert before == after
    assert runs == 4
    payload = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == run["run_id"]
