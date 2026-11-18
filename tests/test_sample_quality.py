from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pytest

from pagedrift.cli import main as cli_main
from pagedrift.ids import canonical_json
from pagedrift.pipeline import run_demo
from pagedrift.sample_quality import SampleQualityError, inspect_sample

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "pagedrift/fixtures/catalog"
VALID = FIXTURE_DIR / "sample-quality-valid.json"
ALPHA_DEFECTS = FIXTURE_DIR / "sample-quality-alpha-defects.json"
DELTA_DEFECT = FIXTURE_DIR / "sample-quality-delta-defect.json"
INVALID_THRESHOLD = FIXTURE_DIR / "sample-quality-invalid-threshold.json"


def _mutated_manifest(tmp_path: Path, mutate) -> Path:
    doc = json.loads(VALID.read_text(encoding="utf-8"))
    mutate(doc)
    out = tmp_path / "manifest.json"
    out.write_text(json.dumps(doc), encoding="utf-8")
    return out


def _decisions(result: dict) -> dict[str, dict]:
    return {d["contract_id"]: d for d in result["release_decisions"]}


def _by_category(result: dict) -> dict[str, dict]:
    return {c["category"]: c for c in result["population_upper_bounds"]["by_category"]}


def test_valid_sample_produces_bounded_explainable_decisions(demo_bundle: dict) -> None:
    safe = demo_bundle["root"] / "snapshot"
    result = inspect_sample(safe, VALID)
    assert [(s["category"], s["population"], s["sample"], s["observed_defects"]) for s in result["strata"]] == [
        ("alpha", 8, 8, 1), ("beta", 7, 7, 0), ("delta", 8, 3, 0), ("gamma", 7, 7, 0)
    ]
    # the naive rate stays visible as context but is not a population guarantee.
    assert result["naive_sample_rate"] == "1/25"
    bounds = result["population_upper_bounds"]
    assert bounds["confidence_level"] == "0.95" and bounds["alpha"] == "0.05"
    by_cat = _by_category(result)
    alpha = by_cat["alpha"]
    assert alpha["bound_method"] == "exact_full_inspection"
    assert (alpha["upper_defects"], alpha["upper_rate"]) == (1, "1/8")
    delta = by_cat["delta"]
    assert delta["bound_method"] == "hypergeometric_inversion"
    assert (delta["upper_defects"], delta["upper_rate"], delta["upper_rate_decimal"]) == (4, "1/2", "0.500000")
    # inversion witness: K=4 is accepted (1/14 > 0.05), K=5 is rejected (1/56 <= 0.05).
    assert delta["tail_at_bound"] == {
        "defects": 4, "p_le_observed": "1/14", "p_le_observed_decimal": "0.071429"
    }
    assert delta["tail_above_bound"] == {
        "defects": 5, "p_le_observed": "1/56", "p_le_observed_decimal": "0.017857"
    }
    # the 5 uninspected delta items are not assumed defect-free: 1 + 4 = 5.
    assert bounds["overall"] == {
        "population": 30, "upper_defects": 5, "upper_rate": "1/6", "upper_rate_decimal": "0.166667"
    }
    decisions = _decisions(result)
    overall = decisions["overall_18"]
    assert overall["decision"] == "publishable" and overall["scope"] == "overall"
    assert overall["measured"]["upper_rate"] == "1/6"
    each = decisions["each_30"]
    assert each["decision"] == "blocked" and each["scope"] == "each_category"
    # the naive 1/25 rate would have waved both contracts through; the bounded
    # decision blocks each_30 because delta's bound (1/2) exceeds 0.30.
    assert [c["category"] for c in each["blocking_categories"]] == ["delta"]
    assert "delta 4/8" in each["reason"]
    # run / audit / sample fingerprints are all retained.
    assert result["run_id"] and result["universe_sha256"]
    assert result["audit_fingerprint"] and result["sample_fingerprint"]


def test_result_is_byte_stable(demo_bundle: dict) -> None:
    safe = demo_bundle["root"] / "snapshot"
    first = inspect_sample(safe, VALID)
    second = inspect_sample(safe, VALID)
    assert canonical_json(first) == canonical_json(second)
    assert first["sample_fingerprint"] == second["sample_fingerprint"]


def test_full_inspection_defect_is_not_washed_out(demo_bundle: dict) -> None:
    safe = demo_bundle["root"] / "snapshot"
    result = inspect_sample(safe, ALPHA_DEFECTS)
    by_cat = _by_category(result)
    assert by_cat["alpha"]["bound_method"] == "exact_full_inspection"
    assert by_cat["alpha"]["upper_defects"] == 3
    # 3 exact alpha defects + 4 delta bound; clean samples elsewhere wash out nothing.
    assert result["population_upper_bounds"]["overall"]["upper_defects"] == 7
    decisions = _decisions(result)
    assert decisions["overall_18"]["decision"] == "blocked"
    each = decisions["each_30"]
    assert each["decision"] == "blocked"
    assert [c["category"] for c in each["blocking_categories"]] == ["alpha", "delta"]


def test_partial_stratum_with_defect_inverts_hypergeometric(demo_bundle: dict) -> None:
    safe = demo_bundle["root"] / "snapshot"
    result = inspect_sample(safe, DELTA_DEFECT)
    delta = _by_category(result)["delta"]
    assert delta["observed_defects"] == 1
    # K=6 accepted (3/28 > 0.05), K=7 rejected (0 <= 0.05).
    assert (delta["upper_defects"], delta["upper_rate"]) == (6, "3/4")
    assert delta["tail_at_bound"]["p_le_observed"] == "3/28"
    assert delta["tail_above_bound"]["p_le_observed"] == "0"
    assert result["population_upper_bounds"]["overall"]["upper_defects"] == 7
    decisions = _decisions(result)
    assert decisions["overall_18"]["decision"] == "blocked"
    assert decisions["each_30"]["decision"] == "blocked"


def test_contract_threshold_boundary_is_inclusive(demo_bundle: dict, tmp_path: Path) -> None:
    safe = demo_bundle["root"] / "snapshot"
    # overall upper bound is exactly 1/6; an inclusive contract at 1/6 publishes.
    manifest = _mutated_manifest(tmp_path, lambda d: d["release_contracts"][0].update(max_rate="1/6"))
    assert _decisions(inspect_sample(safe, manifest))["overall_18"]["decision"] == "publishable"
    # one notch below the bound blocks the overall contract.
    manifest = _mutated_manifest(tmp_path, lambda d: d["release_contracts"][0].update(max_rate="0.16"))
    assert _decisions(inspect_sample(safe, manifest))["overall_18"]["decision"] == "blocked"
    # delta's bound is exactly 1/2; an inclusive per-category contract at 1/2 publishes.
    manifest = _mutated_manifest(tmp_path, lambda d: d["release_contracts"][1].update(max_rate="1/2"))
    assert _decisions(inspect_sample(safe, manifest))["each_30"]["decision"] == "publishable"


def test_unsafe_run_is_rejected(demo_bundle: dict) -> None:
    with pytest.raises(SampleQualityError, match="unsafe"):
        inspect_sample(demo_bundle["root"] / "offset", VALID)


def test_receipt_identity_mismatch_is_rejected(demo_bundle: dict, tmp_path: Path) -> None:
    safe = demo_bundle["root"] / "snapshot"
    # a receipt for an item that does not exist in the run.
    unknown = _mutated_manifest(tmp_path, lambda d: d["observations"][0].update(item_id="itm-9999"))
    with pytest.raises(SampleQualityError, match="selected sample"):
        inspect_sample(safe, unknown)
    # a receipt for a real delta item that the fixed design did not select.
    unselected = _mutated_manifest(tmp_path, lambda d: d["observations"][0].update(item_id="itm-0009"))
    with pytest.raises(SampleQualityError, match="selected sample"):
        inspect_sample(safe, unselected)


def test_tampered_run_records_are_rejected(demo_bundle: dict, tmp_path: Path) -> None:
    safe = demo_bundle["root"] / "snapshot"
    # a duplicated pagination item in the run records.
    clone = tmp_path / "clone-dup"
    shutil.copytree(safe, clone)
    lines = (clone / "records.jsonl").read_text(encoding="utf-8").splitlines()
    (clone / "records.jsonl").write_text("\n".join(lines + [lines[0]]) + "\n", encoding="utf-8")
    with pytest.raises(SampleQualityError, match="invalid or duplicate pagination item"):
        inspect_sample(clone, VALID)
    # a run whose records lost an entire category no longer matches the design.
    clone2 = tmp_path / "clone-missing"
    shutil.copytree(safe, clone2)
    kept = [l for l in lines if json.loads(l)["category"] != "delta"]
    (clone2 / "records.jsonl").write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises(SampleQualityError, match="does not cover categories"):
        inspect_sample(clone2, VALID)


def test_duplicate_receipt_is_rejected(demo_bundle: dict, tmp_path: Path) -> None:
    manifest = _mutated_manifest(tmp_path, lambda d: d["observations"].append(dict(d["observations"][0])))
    with pytest.raises(SampleQualityError, match="invalid or duplicate inspection receipt"):
        inspect_sample(demo_bundle["root"] / "snapshot", manifest)


def test_category_coverage_is_enforced(demo_bundle: dict, tmp_path: Path) -> None:
    safe = demo_bundle["root"] / "snapshot"
    missing = _mutated_manifest(tmp_path, lambda d: d["sample_sizes"].pop("delta"))
    with pytest.raises(SampleQualityError, match="does not cover categories"):
        inspect_sample(safe, missing)
    extra = _mutated_manifest(tmp_path, lambda d: d["sample_sizes"].update(omega=1))
    with pytest.raises(SampleQualityError, match="does not cover categories"):
        inspect_sample(safe, extra)


@pytest.mark.parametrize("bad_size", [0, 9, "3", True, 3.0])
def test_invalid_sample_size_is_rejected(demo_bundle: dict, tmp_path: Path, bad_size: object) -> None:
    manifest = _mutated_manifest(tmp_path, lambda d: d["sample_sizes"].update(delta=bad_size))
    with pytest.raises(SampleQualityError, match="sample size outside category"):
        inspect_sample(demo_bundle["root"] / "snapshot", manifest)


def test_invalid_threshold_fixture_is_rejected(demo_bundle: dict) -> None:
    with pytest.raises(SampleQualityError, match="invalid contract threshold"):
        inspect_sample(demo_bundle["root"] / "snapshot", INVALID_THRESHOLD)


@pytest.mark.parametrize("bad_rate", ["abc", "-0.1", "1.5", True, None])
def test_invalid_threshold_values_are_rejected(demo_bundle: dict, tmp_path: Path, bad_rate: object) -> None:
    def mutate(doc: dict) -> None:
        if bad_rate is None:
            doc["release_contracts"][0].pop("max_rate")
        else:
            doc["release_contracts"][0]["max_rate"] = bad_rate
    manifest = _mutated_manifest(tmp_path, mutate)
    with pytest.raises(SampleQualityError, match="invalid contract threshold"):
        inspect_sample(demo_bundle["root"] / "snapshot", manifest)


def test_inconsistent_sampling_design_is_rejected(demo_bundle: dict, tmp_path: Path) -> None:
    safe = demo_bundle["root"] / "snapshot"
    design = _mutated_manifest(tmp_path, lambda d: d.update(sampling_design="srs_with_replacement"))
    with pytest.raises(SampleQualityError, match="unknown sample design"):
        inspect_sample(safe, design)
    version = _mutated_manifest(tmp_path, lambda d: d.update(schema_version=2))
    with pytest.raises(SampleQualityError, match="unknown sample design"):
        inspect_sample(safe, version)
    seed = _mutated_manifest(tmp_path, lambda d: d.update(seed="42"))
    with pytest.raises(SampleQualityError, match="does not cover categories"):
        inspect_sample(safe, seed)


def test_duplicate_contract_kind_is_rejected(demo_bundle: dict, tmp_path: Path) -> None:
    safe = demo_bundle["root"] / "snapshot"
    appended = _mutated_manifest(
        tmp_path,
        lambda d: d["release_contracts"].append(
            {"contract_id": "overall_18_b", "kind": "overall_max", "max_rate": "0.20"}
        ),
    )
    with pytest.raises(SampleQualityError, match="release contract"):
        inspect_sample(safe, appended)
    replaced = _mutated_manifest(
        tmp_path,
        lambda d: d["release_contracts"][1].update(kind="overall_max"),
    )
    with pytest.raises(SampleQualityError, match="release contract"):
        inspect_sample(safe, replaced)


def test_run_change_alters_population_selection_and_bounds(demo_bundle: dict, tmp_path: Path) -> None:
    shifted = tmp_path / "shifted"
    run_demo(shifted, seed_override=10091)
    safe = shifted / "snapshot"
    # the fixed-starting-point manifest no longer matches this run's selection:
    # the stale decision is rejected, not annotated.
    with pytest.raises(SampleQualityError, match="selected sample"):
        inspect_sample(safe, VALID)
    # a manifest rebuilt for this run yields different N, selection and bounds.
    rows = [json.loads(line) for line in (safe / "records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row["category"], []).append(row["id"])
    sizes = {"alpha": 8, "beta": 8, "gamma": 7, "delta": 3}
    observations = []
    for category, ids in groups.items():
        for item_id in random.Random(42).sample(sorted(ids), sizes[category]):
            observations.append({"item_id": item_id, "defect": False})
    observations[0]["defect"] = True
    manifest = {
        "schema_version": 1,
        "sampling_design": "srswor_by_category",
        "seed": 42,
        "sample_sizes": sizes,
        "observations": observations,
        "release_contracts": [
            {"contract_id": "overall_18", "kind": "overall_max", "max_rate": "0.18"},
            {"contract_id": "each_30", "kind": "each_category_max", "max_rate": "0.30"},
        ],
    }
    manifest_path = tmp_path / "shifted-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = inspect_sample(safe, manifest_path)
    assert [(s["category"], s["population"], s["sample"], s["observed_defects"]) for s in result["strata"]] == [
        ("alpha", 8, 8, 1), ("beta", 8, 8, 0), ("delta", 7, 3, 0), ("gamma", 7, 7, 0)
    ]
    delta = _by_category(result)["delta"]
    assert (delta["population"], delta["upper_defects"], delta["upper_rate"]) == (7, 3, "3/7")
    assert result["population_upper_bounds"]["overall"]["upper_defects"] == 4
    base = inspect_sample(demo_bundle["root"] / "snapshot", VALID)
    assert result["sample_fingerprint"] != base["sample_fingerprint"]


def test_cli_writes_byte_identical_reports(demo_bundle: dict, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    safe = str(demo_bundle["root"] / "snapshot")
    out1 = tmp_path / "quality-1.json"
    out2 = tmp_path / "quality-2.json"
    assert cli_main(["sample-quality", "--run-dir", safe, "--manifest", str(VALID), "--out", str(out1)]) == 0
    assert cli_main(["sample-quality", "--run-dir", safe, "--manifest", str(VALID), "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    report = json.loads(out1.read_text(encoding="utf-8"))
    decisions = {d["contract_id"]: d["decision"] for d in report["release_decisions"]}
    assert decisions == {"overall_18": "publishable", "each_30": "blocked"}
    stdout = capsys.readouterr().out
    assert "contract=overall_18 decision=publishable" in stdout
    assert "contract=each_30 decision=blocked" in stdout


def test_cli_rejects_unsafe_run(demo_bundle: dict, tmp_path: Path) -> None:
    out = tmp_path / "quality.json"
    code = cli_main([
        "sample-quality",
        "--run-dir", str(demo_bundle["root"] / "offset"),
        "--manifest", str(VALID),
        "--out", str(out),
    ])
    assert code == 2
    assert not out.exists()
