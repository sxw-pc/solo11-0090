from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pagedrift.catalog_intake import CatalogInputError, intake_catalog
from pagedrift.pipeline import run_demo

FIXTURES = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "catalog"


def test_catalog_intake_uses_safe_pagination_evidence(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    result = intake_catalog(tmp_path / "demo" / "snapshot", FIXTURES / "offers-valid.json")
    assert [offer["sku"] for offer in result["offers"]] == ["SYN-ALPHA", "SYN-BETA", "SYN-GAMMA"]
    assert result["offers"][0]["available_units"] == 3
    assert result == intake_catalog(tmp_path / "demo" / "snapshot", FIXTURES / "offers-valid.json")
    assert len(result["catalog_fingerprint"]) == 64
    with pytest.raises(CatalogInputError, match="not safe"):
        intake_catalog(tmp_path / "demo" / "offset", FIXTURES / "offers-valid.json")
    with pytest.raises(CatalogInputError, match="unknown item"):
        intake_catalog(tmp_path / "demo" / "snapshot", FIXTURES / "offers-invalid.json")
    audit = tmp_path / "demo" / "snapshot" / "audit.json"
    data = json.loads(audit.read_text(encoding="utf-8"))
    data["summary"]["verdict"] = "unsafe"
    audit.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CatalogInputError, match="not safe"):
        intake_catalog(tmp_path / "demo" / "snapshot", FIXTURES / "offers-valid.json")


def test_catalog_intake_cli_writes_output(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    output = tmp_path / "catalog.json"
    process = subprocess.run(
        [sys.executable, "-m", "pagedrift", "catalog-intake", "--run-dir", str(tmp_path / "demo" / "snapshot"),
         "--manifest", str(FIXTURES / "offers-valid.json"), "--out", str(output)],
        capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert "catalog_offers=3" in process.stdout
    assert len(json.loads(output.read_text(encoding="utf-8"))["offers"]) == 3
