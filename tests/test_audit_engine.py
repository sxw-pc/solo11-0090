from __future__ import annotations
import json
from pathlib import Path
from pagedrift.audit.extras import observed_extras
from helpers import read_json

def _audit(root: Path, strategy: str) -> dict:
    return read_json(root / strategy / "audit.json")

def test_offset_demo_contains_all_five_defect_classes(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "offset")
    counts = audit["summary"]["findings_by_code"]
    assert counts["DRIFT_SKIP"] == 2
    assert counts["DRIFT_DUP"] == 2
    assert counts["DRIFT_OOR"] == 1
    assert counts["DRIFT_PREMATURE_TERM"] == 11
    assert counts["DRIFT_TAIL_TRUNC"] == 1
    assert counts["DRIFT_INSERT_OBSERVED"] == 1
    assert counts["RESP_INVALID"] == 0
    assert audit["summary"]["verdict"] == "unsafe"
    assert audit["termination_proof"]["satisfied"] is True

def test_every_finding_has_page_and_evidence_locator(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "offset")
    for finding in audit["findings"]:
        assert finding["code"]
        assert finding["natural_key"]
        assert isinstance(finding["page_seq"], int) and finding["page_seq"] > 0
        assert len(finding["evidence_sha8"]) == 8
        assert finding["detail"]
    evidence_by_seq = {item["seq"]: item for item in audit["evidence"]}
    for finding in audit["findings"]:
        assert finding["evidence_sha8"] == evidence_by_seq[finding["page_seq"]]["sha256"][:8]

def test_skip_duplicate_and_oor_natural_keys_are_attributed(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "offset")
    codes = {}
    for finding in audit["findings"]:
        codes.setdefault(finding["code"], set()).add(finding["natural_key"])
    assert {"itm-0009", "itm-0015"} <= codes["DRIFT_SKIP"]
    assert {"itm-0001", "itm-0005"} <= codes["DRIFT_DUP"]
    assert "itm-0001" in codes["DRIFT_OOR"]
    assert "itm-0020" in codes["DRIFT_PREMATURE_TERM"]
    assert "itm-0020" in codes["DRIFT_TAIL_TRUNC"]

def test_insert_observed_is_not_a_defect(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "offset")
    extras = [item for item in audit["findings"] if item["code"] == "DRIFT_INSERT_OBSERVED"]
    assert len(extras) == 1
    assert extras[0]["natural_key"] == "itm-0902"
    assert extras[0]["is_defect"] is False
    assert "attributed" in extras[0]["detail"]

def test_snapshot_demo_is_safe_baseline(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "snapshot")
    assert audit["summary"]["verdict"] == "safe"
    assert audit["summary"]["coverage_ratio"] == 1.0
    assert audit["summary"]["skip_count"] == 0
    assert audit["summary"]["dup_ratio"] == 0.0
    assert audit["termination_proof"]["satisfied"] is True
    assert audit["snapshot_integrity"]["consistent"] is True
    assert audit["findings"] == []

def test_coverage_counts_attributed_insert_as_non_penalty(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "offset")
    coverage = audit["coverage"]
    assert coverage["initial_count"] == 30
    assert coverage["observed_initial"] == 17
    assert coverage["included_extras"] == 1
    assert coverage["numerator"] == 18
    assert coverage["denominator"] == 31
    assert audit["summary"]["coverage_ratio"] == round(18 / 31, 6)

def test_unattributed_extra_is_still_reported_without_a_defect() -> None:
    records = [
        {"id": "itm-0001", "sort_key": 10, "source_page_seq": 1},
        {"id": "itm-9000", "sort_key": 20, "source_page_seq": 2},
    ]
    extras = observed_extras(records, {"itm-0001"}, [])
    assert extras[0]["id"] == "itm-9000"
    assert extras[0]["attributed_insert"] is False

def test_audit_inputs_carry_all_fingerprints(demo_bundle: dict) -> None:
    audit = _audit(demo_bundle["root"], "offset")
    inputs = audit["inputs"]
    assert len(inputs["plan_sha256"]) == 64
    assert len(inputs["universe_sha256"]) == 64
    assert len(inputs["schedule_sha256"]) == 64
    assert len(inputs["strategy_input_sha256"]) == 64
    assert audit["audit_fingerprint"]
    json.dumps(audit)
