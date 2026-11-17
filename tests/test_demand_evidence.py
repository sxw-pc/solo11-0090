from pathlib import Path

import pytest

from pagedrift.demand_evidence import DemandInputError, inspect_demand_evidence
from pagedrift.pipeline import run_demo

FIXTURES = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "catalog"


def test_censored_demand_evidence_is_real_and_not_estimated(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    run_dir = tmp_path / "demo" / "snapshot"
    good = inspect_demand_evidence(run_dir, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    assert good["sku"] == "SYN-ALPHA"
    assert len(good["days"]) == 6
    assert sum(row["stockout"] for row in good["days"]) == 3
    assert good["estimate"] is None and good["recommended_units"] is None
    assert good["catalog_price_cny"] == "90.00"
    assert good == inspect_demand_evidence(run_dir, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    cheaper = inspect_demand_evidence(run_dir, FIXTURES / "offers-price-low.json", FIXTURES / "demand-valid.json")
    assert cheaper["catalog_price_cny"] == "20.00"
    assert cheaper["evidence_fingerprint"] != good["evidence_fingerprint"]
    with pytest.raises(DemandInputError, match="inventory bounds"):
        inspect_demand_evidence(run_dir, FIXTURES / "offers-valid.json", FIXTURES / "demand-invalid.json")
