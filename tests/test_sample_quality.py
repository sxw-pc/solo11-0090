from __future__ import annotations

import json
from pathlib import Path

import pytest

from pagedrift.pipeline import run_demo
from pagedrift.sample_quality import SampleQualityError, inspect_sample

FIXTURE = Path(__file__).resolve().parents[1] / "pagedrift/fixtures/catalog/sample-quality-valid.json"


def test_sample_quality_input_is_real_but_has_no_population_decision(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    safe = tmp_path / "demo/snapshot"
    result = inspect_sample(safe, FIXTURE)
    assert sum(s["population"] for s in result["strata"]) == 30
    assert sum(s["sample"] for s in result["strata"]) == 25
    assert [(s["category"],s["population"],s["sample"],s["observed_defects"]) for s in result["strata"]] == [
        ("alpha",8,8,1),("beta",7,7,0),("delta",8,3,0),("gamma",7,7,0)
    ]
    assert result["naive_sample_rate"] == "1/25"
    assert result["population_upper_bounds"] is None and result["release_decisions"] is None
    assert result == inspect_sample(safe, FIXTURE)
    with pytest.raises(SampleQualityError,match="unsafe"):
        inspect_sample(tmp_path / "demo/offset", FIXTURE)
    tampered = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tampered["observations"][0]["item_id"] = "itm-9999"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(tampered),encoding="utf-8")
    with pytest.raises(SampleQualityError,match="selected sample"):
        inspect_sample(safe,bad)
