from __future__ import annotations

import copy
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from pagedrift.catalog_intake import intake_catalog
from pagedrift.origin_evidence import inspect_origin_contract, preview_origin_assignment
from pagedrift.origin_quote import (
    OriginQuoteError,
    _simplex_max,
    quote_origin,
    verify_origin_quote,
)
from pagedrift.pipeline import run_demo

FIXTURE = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "origin_content"
MAIN_CONTRACT = FIXTURE / "origin-contract.json"


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("origin-demo")
    run_demo(root / "demo")
    return root / "demo"


@pytest.fixture(scope="module")
def catalog(demo_run: Path) -> dict:
    return intake_catalog(demo_run / "snapshot", FIXTURE / "offers.json")


def _offers_variant(tmp_path: Path, name: str, mutate) -> Path:
    data = json.loads((FIXTURE / "offers.json").read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_main_contract_bounds_quotes_and_proofs(catalog: dict) -> None:
    quote = quote_origin(catalog, MAIN_CONTRACT)
    assert quote["feasibility"] == "multiple"
    assert quote["origin_share_bounds"] == {"min": "7/20", "max": "1/2"}
    assert quote["cyclic_components"] == [["itm-0004", "itm-0005"], ["itm-0006", "itm-0007"]]
    assert quote["finished_price_cny"] == "90.00"
    guaranteed = quote["duty_quotes"]["guaranteed_origin_quote"]
    plausible = quote["duty_quotes"]["plausible_origin_quote"]
    assert guaranteed["quantifier"] == "for_all_feasible_assignments"
    assert guaranteed["preference_eligible"] is False
    assert (guaranteed["applied_rate"], guaranteed["duty_cny"]) == ("1/10", "9.00")
    assert plausible["quantifier"] == "exists_feasible_assignment"
    assert plausible["preference_eligible"] is True
    assert (plausible["applied_rate"], plausible["duty_cny"]) == ("3/100", "2.70")
    assert plausible["pending_origin_proof"] is True
    assert plausible["customs_document"] is False
    assert quote["internal_scenario_only"] is True and quote["publishable"] is False
    # The 100% cycles are unanchored: X and Z move freely between the two bounds.
    assert quote["bound_assignments"]["min"]["itm-0004"] == "1/4"
    assert quote["bound_assignments"]["max"]["itm-0004"] == "3/4"
    for side in ("min", "max"):
        assert quote["bound_proofs"][side]["verified"] is True
        assert quote["bound_proofs"][side]["multipliers"]
    # The LP assignments are exactly the assignments the preview accepts.
    evidence = inspect_origin_contract(catalog, MAIN_CONTRACT)
    for side in ("min", "max"):
        preview = preview_origin_assignment(evidence, quote["bound_assignments"][side])
        assert preview["valid"] is True
        assert preview["finished_regional_share"] == quote["origin_share_bounds"][side]
    # Output is bound to the verified identities and is deterministic.
    assert quote["run_id"] == catalog["run_id"]
    assert quote["audit_fingerprint"] == catalog["audit_fingerprint"]
    assert quote["catalog_fingerprint"] == catalog["catalog_fingerprint"]
    assert len(quote["contract_fingerprint"]) == len(quote["quote_fingerprint"]) == 64
    assert quote == quote_origin(catalog, MAIN_CONTRACT)
    relations = {row["item_id"]: row for row in quote["fixed_point_relations"]}
    assert relations["itm-0001"]["expression"] == (
        "1/5*itm-0002 + 1/5*itm-0003 + 3/10*itm-0004 + 3/10*itm-0006"
    )
    assert relations["itm-0002"] == {"item_id": "itm-0002", "kind": "terminal", "share": "1"}


def test_nonsingular_cycle_has_unique_solution(catalog: dict) -> None:
    quote = quote_origin(catalog, FIXTURE / "contract-nonsingular-cycle.json")
    assert quote["feasibility"] == "unique"
    assert quote["origin_share_bounds"] == {"min": "3/5", "max": "3/5"}
    assert quote["bound_assignments"]["min"] == quote["bound_assignments"]["max"]
    assignment = quote["bound_assignments"]["min"]
    assert assignment["itm-0004"] == "2/3" and assignment["itm-0005"] == "1/3"
    guaranteed = quote["duty_quotes"]["guaranteed_origin_quote"]
    assert guaranteed["preference_eligible"] is True
    assert (guaranteed["applied_rate"], guaranteed["duty_cny"]) == ("3/100", "2.70")


def test_contradictory_independent_evidence_is_infeasible(catalog: dict) -> None:
    quote = quote_origin(catalog, FIXTURE / "contract-contradictory.json")
    assert quote["feasibility"] == "infeasible"
    assert quote["origin_share_bounds"] is None
    assert quote["bound_assignments"] is None
    assert quote["duty_quotes"] is None


def test_joint_bound_tightens_upper_bound(catalog: dict) -> None:
    quote = quote_origin(catalog, FIXTURE / "contract-joint-tight.json")
    assert quote["feasibility"] == "multiple"
    assert quote["origin_share_bounds"] == {"min": "7/20", "max": "19/50"}
    # With the joint bound X+Z<=3/5 no assignment reaches 9/20, so both
    # contracts fall back to the standard rate.
    for name in ("guaranteed_origin_quote", "plausible_origin_quote"):
        row = quote["duty_quotes"][name]
        assert row["preference_eligible"] is False
        assert (row["applied_rate"], row["duty_cny"]) == ("1/10", "9.00")


def test_threshold_equality_qualifies_only_plausible(catalog: dict) -> None:
    quote = quote_origin(catalog, FIXTURE / "contract-threshold-edge.json")
    assert quote["origin_share_bounds"]["max"] == "9/20"
    guaranteed = quote["duty_quotes"]["guaranteed_origin_quote"]
    plausible = quote["duty_quotes"]["plausible_origin_quote"]
    assert guaranteed["preference_eligible"] is False
    assert (guaranteed["applied_rate"], guaranteed["duty_cny"]) == ("1/10", "9.00")
    assert plausible["preference_eligible"] is True
    assert (plausible["applied_rate"], plausible["duty_cny"]) == ("3/100", "2.70")


def test_invalid_origin_graphs_rejected(catalog: dict) -> None:
    with pytest.raises(OriginQuoteError, match="sum to one"):
        quote_origin(catalog, FIXTURE / "contract-bad-weights.json")
    with pytest.raises(OriginQuoteError, match="unknown item"):
        quote_origin(catalog, FIXTURE / "contract-unknown-item.json")


def test_duplicate_offer_identity_rejected(demo_run: Path, tmp_path: Path) -> None:
    def duplicate(data: dict) -> None:
        extra = dict(data["offers"][0])
        extra["sku"] = "SYN-FINISHED-COPY"
        data["offers"].append(extra)

    manifest = _offers_variant(tmp_path, "offers-dup.json", duplicate)
    catalog = intake_catalog(demo_run / "snapshot", manifest)
    with pytest.raises(OriginQuoteError, match="duplicate item identity"):
        quote_origin(catalog, MAIN_CONTRACT)


def test_catalog_identity_drift_rejected(catalog: dict) -> None:
    tampered = copy.deepcopy(catalog)
    tampered["offers"][0]["price_cny"] = "91.00"
    with pytest.raises(OriginQuoteError, match="drift"):
        quote_origin(tampered, MAIN_CONTRACT)


def test_price_update_forces_requote(demo_run: Path, catalog: dict, tmp_path: Path) -> None:
    def reprice(data: dict) -> None:
        data["offers"][0]["price_cny"] = "95.00"

    manifest = _offers_variant(tmp_path, "offers-repriced.json", reprice)
    updated = intake_catalog(demo_run / "snapshot", manifest)
    assert updated["catalog_fingerprint"] != catalog["catalog_fingerprint"]
    quote = quote_origin(updated, MAIN_CONTRACT)
    assert quote["finished_price_cny"] == "95.00"
    assert quote["duty_quotes"]["guaranteed_origin_quote"]["duty_cny"] == "9.50"
    assert quote["duty_quotes"]["plausible_origin_quote"]["duty_cny"] == "2.85"
    stale = quote_origin(catalog, MAIN_CONTRACT)
    with pytest.raises(OriginQuoteError, match="stale"):
        verify_origin_quote(stale, updated, MAIN_CONTRACT)


def test_stale_contract_or_run_reuse_rejected(demo_run: Path, catalog: dict, tmp_path: Path) -> None:
    quote = quote_origin(catalog, MAIN_CONTRACT)
    assert verify_origin_quote(quote, catalog, MAIN_CONTRACT) == quote
    with pytest.raises(OriginQuoteError, match="stale"):
        verify_origin_quote(quote, catalog, FIXTURE / "contract-joint-tight.json")
    run_demo(tmp_path / "other-demo", seed_override=7)
    other = intake_catalog(tmp_path / "other-demo" / "snapshot", FIXTURE / "offers.json")
    assert other["run_id"] != catalog["run_id"]
    with pytest.raises(OriginQuoteError, match="stale"):
        verify_origin_quote(quote, other, MAIN_CONTRACT)


def test_amount_rounding_boundary(demo_run: Path, tmp_path: Path) -> None:
    def half_cent(data: dict) -> None:
        data["offers"][0]["price_cny"] = "0.05"

    catalog = intake_catalog(demo_run / "snapshot", _offers_variant(tmp_path, "offers-half.json", half_cent))
    quote = quote_origin(catalog, MAIN_CONTRACT)
    # 0.05 * 1/10 = 0.005 rounds half-up to one cent.
    assert quote["duty_quotes"]["guaranteed_origin_quote"]["duty_cny"] == "0.01"
    # 0.05 * 3/100 = 0.0015 stays below the half-cent boundary.
    assert quote["duty_quotes"]["plausible_origin_quote"]["duty_cny"] == "0.00"

    def below_half(data: dict) -> None:
        data["offers"][0]["price_cny"] = "0.04"

    catalog = intake_catalog(demo_run / "snapshot", _offers_variant(tmp_path, "offers-below.json", below_half))
    quote = quote_origin(catalog, MAIN_CONTRACT)
    assert quote["duty_quotes"]["guaranteed_origin_quote"]["duty_cny"] == "0.00"


def test_solver_distinguishes_unbounded() -> None:
    # max x subject to -x <= 0, x >= 0 is unbounded; the public quote path
    # bounds every share in [0, 1] so this branch is solver-level only.
    status, value, point, duals = _simplex_max([Fraction(1)], [[Fraction(-1)]], [Fraction(0)])
    assert status == "unbounded"
    assert value is None and point is None and duals is None
    status, *_ = _simplex_max([Fraction(1)], [[Fraction(1)], [Fraction(-1)]], [Fraction(1), Fraction(-2)])
    assert status == "infeasible"


def test_origin_quote_cli(demo_run: Path, tmp_path: Path) -> None:
    output = tmp_path / "origin-quote.json"
    process = subprocess.run(
        [sys.executable, "-m", "pagedrift", "origin-quote",
         "--run-dir", str(demo_run / "snapshot"),
         "--manifest", str(FIXTURE / "offers.json"),
         "--contract", str(MAIN_CONTRACT),
         "--out", str(output)],
        capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert "feasibility=multiple" in process.stdout
    assert "bounds=7/20..1/2" in process.stdout
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["duty_quotes"]["plausible_origin_quote"]["duty_cny"] == "2.70"
    unsafe = subprocess.run(
        [sys.executable, "-m", "pagedrift", "origin-quote",
         "--run-dir", str(demo_run / "offset"),
         "--manifest", str(FIXTURE / "offers.json"),
         "--contract", str(MAIN_CONTRACT),
         "--out", str(tmp_path / "unsafe.json")],
        capture_output=True, text=True, check=False,
    )
    assert unsafe.returncode == 2
    assert "not safe" in unsafe.stderr
