from __future__ import annotations

import json
from pathlib import Path

import pytest

from pagedrift.catalog_intake import intake_catalog
from pagedrift.origin_evidence import OriginEvidenceError, inspect_origin_contract, preview_origin_assignment
from pagedrift.pipeline import run_demo

FIXTURE = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "origin_content"


def _assignment(x: str, z: str, finished: str) -> dict[str, str]:
    return {"itm-0001": finished, "itm-0002": "1", "itm-0003": "0",
            "itm-0004": x, "itm-0005": x, "itm-0006": z, "itm-0007": z}


def test_safe_catalog_and_two_distinct_valid_origin_assignments(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    catalog = intake_catalog(tmp_path / "demo" / "snapshot", FIXTURE / "offers.json")
    evidence = inspect_origin_contract(catalog, FIXTURE / "origin-contract.json")
    assert evidence["finished_item_id"] == "itm-0001"
    assert evidence["finished_price_cny"] == "90.00"
    assert evidence["bill_of_materials"]["itm-0004"] == [{"input": "itm-0005", "fraction": "1"}]
    assert evidence["bill_of_materials"]["itm-0005"] == [{"input": "itm-0004", "fraction": "1"}]
    low = preview_origin_assignment(evidence, _assignment("1/4", "1/4", "7/20"))
    high = preview_origin_assignment(evidence, _assignment("1/2", "1/2", "1/2"))
    assert low["valid"] and high["valid"]
    assert low["finished_regional_share"] == "7/20"
    assert high["finished_regional_share"] == "1/2"
    assert low["publishable"] is high["publishable"] is False
    assert evidence["origin_share_bounds"] is evidence["duty_quotes"] is None


def test_broken_parent_identity_and_inconsistent_assignment_block(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    catalog = intake_catalog(tmp_path / "demo" / "snapshot", FIXTURE / "offers.json")
    data = json.loads((FIXTURE / "origin-contract.json").read_text(encoding="utf-8"))
    data["bill_of_materials"]["itm-0001"][0]["input"] = "unknown"
    file = tmp_path / "origin-contract.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OriginEvidenceError, match="fractions"):
        inspect_origin_contract(catalog, file)
    evidence = inspect_origin_contract(catalog, FIXTURE / "origin-contract.json")
    bad = preview_origin_assignment(evidence, _assignment("1/4", "1/4", "1/2"))
    assert bad["valid"] is False
