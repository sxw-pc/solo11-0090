from __future__ import annotations
import json
import shutil
from pathlib import Path
from pagedrift.audit.compare import build_comparison, write_comparison

def test_demo_comparison_ranks_snapshot_as_safe(demo_bundle: dict) -> None:
    root = demo_bundle["root"]
    result = build_comparison([root / name for name in ("offset", "pagenum", "keyset", "snapshot")])
    assert result["overall_verdict"] == "unsafe"
    assert result["inputs_compatible"] is True
    assert result["snapshot_baseline_defect"] is False
    assert [row["strategy"] for row in result["rows"]] == ["offset", "pagenum", "keyset", "snapshot"]
    by_strategy = {row["strategy"]: row for row in result["rows"]}
    assert by_strategy["snapshot"]["verdict"] == "safe"
    assert by_strategy["offset"]["verdict"] == "unsafe"
    assert by_strategy["offset"]["skip_count"] == 2
    assert by_strategy["offset"]["termination_proof"]["satisfied"] is True
    assert by_strategy["offset"]["skips_per_100_pages"] == 40.0
    for key in ("plan_sha256", "universe_sha256", "schedule_sha256"):
        assert len(result["input_fingerprints"][key]) == 1

def test_comparison_writes_json_and_markdown(demo_bundle: dict, tmp_path: Path) -> None:
    root = demo_bundle["root"]
    result = build_comparison([root / "offset", root / "snapshot"])
    paths = write_comparison(result, tmp_path / "comparison")
    assert Path(paths["json"]).is_file()
    assert Path(paths["markdown"]).is_file()
    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert payload["overall_verdict"] == "unsafe"
    text = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "snapshot" in text
    assert "unsafe" in text

def test_incompatible_fingerprints_are_inconclusive(demo_bundle: dict, tmp_path: Path) -> None:
    root = demo_bundle["root"]
    copied = tmp_path / "offset-copy"
    shutil.copytree(root / "offset", copied)
    audit_path = copied / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["inputs"]["universe_sha256"] = "0" * 64
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    result = build_comparison([copied, root / "snapshot"])
    assert result["inputs_compatible"] is False
    assert result["overall_verdict"] == "inconclusive"
    assert any("fingerprints" in message for message in result["messages"])

def test_unsafe_snapshot_makes_whole_comparison_inconclusive(demo_bundle: dict, tmp_path: Path) -> None:
    root = demo_bundle["root"]
    copied = tmp_path / "snapshot-copy"
    shutil.copytree(root / "snapshot", copied)
    audit_path = copied / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["summary"]["verdict"] = "unsafe"
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    result = build_comparison([root / "offset", copied])
    assert result["snapshot_baseline_defect"] is True
    assert result["overall_verdict"] == "inconclusive"
    assert any("mock or audit itself" in message for message in result["messages"])

def test_comparison_does_not_leak_absolute_run_paths(demo_bundle: dict) -> None:
    root = demo_bundle["root"]
    result = build_comparison([root / "offset", root / "snapshot"])
    for row in result["rows"]:
        assert row["run_dir"] == row["strategy"]
        assert str(root) not in json.dumps(result)
