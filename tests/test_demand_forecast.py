from __future__ import annotations

import json
import math
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from pagedrift.catalog_intake import CatalogInputError
from pagedrift.demand_evidence import DemandInputError
from pagedrift.demand_forecast import (
    ForecastInputError,
    decide_replenishment,
    evaluate_cost_at_quantity,
    expected_cost_quantity,
    fit_lambda,
    poisson_cdf,
    poisson_log_survival_ge,
    poisson_pmf,
    service_level_quantity,
)
from pagedrift.pipeline import run_demo

FIXTURES = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "catalog"


@pytest.fixture()
def snapshot_run(tmp_path: Path) -> Path:
    run_demo(tmp_path / "demo")
    return tmp_path / "demo" / "snapshot"


def _contract(result: dict, contract_id: str) -> dict:
    return next(row for row in result["contracts"] if row["contract_id"] == contract_id)


def test_joint_censored_mle_matches_expected_rate(snapshot_run: Path) -> None:
    result = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    assert result["sku"] == "SYN-ALPHA"
    assert result["item_id"] == "itm-0001"
    assert result["observation_counts"] == {
        "total_days": 6,
        "exact_days": 3,
        "right_censored_days": 3,
    }
    # Spec-anchored MLE for the six-day censored sample.
    assert result["estimate"]["lambda_hat"] == pytest.approx(3.709, abs=0.01)
    # The naive sold-units mean is explicitly not the estimator.
    mean_sold = sum(row["sold_units"] for row in result["estimate"]["per_day"]) / 6
    assert mean_sold == pytest.approx(2.833333, abs=1e-5)
    assert abs(result["estimate"]["lambda_hat"] - mean_sold) > 0.8
    solver = result["estimate"]["solver"]
    assert solver["method"] == "score_bisection"
    assert abs(solver["analytic_score_at_estimate"]) < 1e-9
    assert abs(solver["finite_difference_score"]) < 1e-6
    # Deterministic grid independently brackets the same optimum.
    assert solver["grid_gap"] <= solver["effective_grid_step"]
    assert solver["grid_best_lambda"] == pytest.approx(
        result["estimate"]["lambda_hat"], abs=solver["effective_grid_step"]
    )


def test_per_day_likelihood_matches_joint_value(snapshot_run: Path) -> None:
    result = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    lam = result["estimate"]["lambda_hat"]
    summed = 0.0
    censored = 0
    for row in result["estimate"]["per_day"]:
        if row["kind"] == "exact":
            assert row["constraint"] == f"D = {row['sold_units']}"
            assert math.log(row["point_probability"]) == pytest.approx(
                row["log_point_probability"], abs=1e-9
            )
        else:
            censored += 1
            assert row["constraint"] == f"D >= {row['sold_units']}"
            assert math.exp(row["log_tail_probability"]) == pytest.approx(row["tail_probability"], abs=1e-9)
            # Censored constraint must agree with the explicit tail formula.
            assert row["log_tail_probability"] == pytest.approx(
                poisson_log_survival_ge(row["sold_units"], lam), abs=1e-9
            )
        summed += row["log_likelihood_contribution"]
    assert censored == 3
    assert summed == pytest.approx(-result["estimate"]["negative_log_likelihood"], abs=1e-8)


def test_both_contracts_are_valid_but_disagree(snapshot_run: Path) -> None:
    result = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    service = _contract(result, "service_95")
    cost = _contract(result, "cost_2_margin")
    assert service["recommended_units"] == 7
    # 7 is the *smallest* q meeting 0.95; q-1 must fall short.
    assert service["achieved_probability"] >= 0.95
    assert service["previous_probability"] < 0.95
    assert cost["recommended_units"] == 5
    assert cost["lost_sales_unit_cost_cny"] == "9.000000000000"
    # Both contracts answer the same question and both are legitimate.
    assert service["recommended_units"] != cost["recommended_units"]


def test_catalog_price_enters_the_cost_decision(snapshot_run: Path) -> None:
    baseline = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    cheap = decide_replenishment(snapshot_run, FIXTURES / "offers-price-low.json", FIXTURES / "demand-valid.json")
    # Sales evidence is unchanged, so the demand estimate must be identical.
    assert cheap["estimate"]["lambda_hat"] == baseline["estimate"]["lambda_hat"]
    assert cheap["evidence_fingerprint"] != baseline["evidence_fingerprint"]
    assert cheap["forecast_fingerprint"] != baseline["forecast_fingerprint"]
    assert cheap["catalog_price_cny"] == "20.00"
    assert _contract(cheap, "service_95")["recommended_units"] == 7
    cost = _contract(cheap, "cost_2_margin")
    assert cost["recommended_units"] == 4
    assert cost["lost_sales_unit_cost_cny"] == "2.000000000000"
    # q=4 must be at least as cheap as its neighbours at the low price.
    lam = cheap["estimate"]["lambda_hat"]
    neighbours = {
        q: evaluate_cost_at_quantity(
            q, lam, Decimal("2"), Decimal("0.1"), Decimal("20.00")
        )["expected_cost"]
        for q in (3, 4, 5)
    }
    assert neighbours[4] <= neighbours[3] and neighbours[4] < neighbours[5]


def test_repeated_runs_are_byte_stable(snapshot_run: Path) -> None:
    first = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    second = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    assert first == second


def test_forecast_refuses_unsafe_or_unknown_sources(snapshot_run: Path, tmp_path: Path) -> None:
    # An unsafe (non-snapshot) run must be rejected via catalog intake.
    run_demo(tmp_path / "demo2")
    with pytest.raises(CatalogInputError, match="not safe"):
        decide_replenishment(
            tmp_path / "demo2" / "offset",
            FIXTURES / "offers-valid.json",
            FIXTURES / "demand-valid.json",
        )
    # Unknown SKU cannot be bound to a catalog offer.
    with pytest.raises(DemandInputError, match="not in verified catalog"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-unknown-sku.json"
        )
    # A manifest referencing an item absent from pagination records.
    with pytest.raises(CatalogInputError, match="unknown item"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-invalid.json", FIXTURES / "demand-valid.json"
        )


def test_forecast_refuses_conflicting_and_unidentified_inputs(snapshot_run: Path) -> None:
    with pytest.raises(DemandInputError, match="inventory bounds"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-invalid.json"
        )
    with pytest.raises(DemandInputError, match="inventory bounds"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-duplicate-day.json"
        )
    with pytest.raises(DemandInputError, match="stockout receipt conflicts"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-stockout-conflict.json"
        )
    # Right-censoring alone cannot identify lambda without exact days.
    with pytest.raises(ForecastInputError, match="not identified"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-all-censored.json"
        )
    with pytest.raises(ForecastInputError, match="target"):
        decide_replenishment(
            snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-bad-target.json"
        )
    # No pre-agreed decision contract means no decision can be rendered.
    with pytest.raises(DemandInputError, match="missing decision contracts"):
        decide_replenishment(
            snapshot_run,
            FIXTURES / "offers-valid.json",
            FIXTURES / "demand-missing-contracts.json",
        )
    # Non-numeric cost parameters fail validation before any estimate is built.
    with pytest.raises(DemandInputError, match="invalid decision parameter holding_cost"):
        decide_replenishment(
            snapshot_run,
            FIXTURES / "offers-valid.json",
            FIXTURES / "demand-bad-cost.json",
        )


def test_forecast_does_not_publish_without_evidence(snapshot_run: Path) -> None:
    with pytest.raises(FileNotFoundError):
        decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "missing.json")
    result = decide_replenishment(snapshot_run, FIXTURES / "offers-valid.json", FIXTURES / "demand-valid.json")
    # Every estimate carries its source fingerprints and the assumption text.
    assert len(result["catalog_fingerprint"]) == 64
    assert len(result["evidence_fingerprint"]) == 64
    assert len(result["forecast_fingerprint"]) == 64
    assert "Poisson" in result["identification_assumption"]
    assert "not assumption-free" in result["identification_assumption"]


def test_pure_statistics_units() -> None:
    days = [
        {"sold_units": 3, "stockout": True},
        {"sold_units": 2, "stockout": False},
        {"sold_units": 3, "stockout": True},
        {"sold_units": 4, "stockout": False},
        {"sold_units": 2, "stockout": True},
        {"sold_units": 3, "stockout": False},
    ]
    fit = fit_lambda(days)
    assert fit["lambda_hat"] == pytest.approx(3.709, abs=0.01)
    assert fit["solver"]["grid_gap"] <= fit["solver"]["effective_grid_step"]
    # pmf normalization and cdf monotonicity.
    assert sum(poisson_pmf(k, 1.7) for k in range(0, 60)) == pytest.approx(1.0, abs=1e-12)
    service = service_level_quantity(fit["lambda_hat"], 0.95)
    assert service["quantity"] == 7
    assert poisson_cdf(6, fit["lambda_hat"]) < 0.95 <= poisson_cdf(7, fit["lambda_hat"])
    assert service["achieved_probability"] >= 0.95 > service["previous_probability"]
    # A target below the mass at zero is met by ordering nothing.
    zero = service_level_quantity(fit["lambda_hat"], 0.01)
    assert zero["quantity"] == 0


def test_expected_cost_ties_pick_smaller_quantity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Baseline fixture: q=5 is the unique optimum and strictly beats neighbours.
    outcome = expected_cost_quantity(
        3.709, Decimal("2"), Decimal("0.1"), Decimal("90.00")
    )
    assert outcome["quantity"] == 5

    # Force a genuine tie inside the real solver: a two-point distribution
    # D in {0, 1} with equal mass, holding=1, lost unit cost=1 yields
    # cost(0)=cost(1)=0.5 while cost(2)=1.0. The strict `<` incumbent rule
    # must retain the smaller q.
    # The trailing zero-mass point represents the truncated tail.
    monkeypatch.setattr("pagedrift.demand_forecast._pmf_table", lambda lam: [0.5, 0.5, 0.0])
    tied = expected_cost_quantity(1.0, Decimal("1"), Decimal("1"), Decimal("1.00"))
    assert tied["quantity"] == 0
    assert tied["expected_cost"] == pytest.approx(0.5)


def test_fit_refuses_unidentified_samples() -> None:
    with pytest.raises(ForecastInputError, match="no daily observations"):
        fit_lambda([])
    with pytest.raises(ForecastInputError, match="zero observed sales"):
        fit_lambda([{"sold_units": 0, "stockout": False}])


def test_fit_all_exact_recovers_sample_mean_and_fallback_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = [{"sold_units": s, "stockout": False} for s in (2, 4, 3, 5, 2, 4)]
    fit = fit_lambda(exact)
    assert fit["lambda_hat"] == pytest.approx(20 / 6, abs=1e-9)
    assert fit["solver"]["grid_gap"] <= fit["solver"]["effective_grid_step"]
    # If the analytic score cannot be bracketed, golden section still converges.
    monkeypatch.setattr("pagedrift.demand_forecast._score", lambda lam, days: 1.0)
    fallback = fit_lambda(exact)
    assert fallback["solver"]["method"] == "golden_section"
    assert fallback["solver"]["iterations"] <= 1000
    assert fallback["lambda_hat"] == pytest.approx(20 / 6, abs=1e-6)


def test_demand_forecast_cli_writes_decision(snapshot_run: Path, tmp_path: Path) -> None:
    output = tmp_path / "forecast.json"
    proc = subprocess.run(
        [
            sys.executable, "-m", "pagedrift", "demand-forecast",
            "--run-dir", str(snapshot_run),
            "--catalog-manifest", str(FIXTURES / "offers-valid.json"),
            "--history", str(FIXTURES / "demand-valid.json"),
            "--out", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["estimate"]["lambda_hat"] == pytest.approx(3.709, abs=0.01)
    assert "service_95" in proc.stdout and "cost_2_margin" in proc.stdout
    # Rejected inputs surface a non-zero exit and explain the refusal.
    bad = subprocess.run(
        [
            sys.executable, "-m", "pagedrift", "demand-forecast",
            "--run-dir", str(snapshot_run),
            "--catalog-manifest", str(FIXTURES / "offers-valid.json"),
            "--history", str(FIXTURES / "demand-all-censored.json"),
            "--out", str(tmp_path / "bad.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2
    assert "not identified" in bad.stderr
    assert not (tmp_path / "bad.json").exists()
