"""Offline next-day replenishment decision from censored Poisson evidence.

This module turns the evidence produced by :mod:`pagedrift.demand_evidence`
into a point estimate and two contract-specific replenishment quantities. It
never collects data itself: every number is derived from

* a safe pagination run and a verified catalog (catalog intake), and
* a history whose exact and right-censored daily observations were already
  validated and fingerprinted.

Identification assumptions
--------------------------
The synthetic observation contract ``iid_poisson_min_sales`` assumes the
latent daily demands are independent draws ``D ~ Poisson(lambda)``. On a day
without a stockout receipt, ``D == sold`` is an exact observation; on a
stockout day only ``D >= sold`` is known (right censoring). The estimator is
the joint maximum-likelihood estimator over both kinds of observations. It is
therefore conditional on that contract and must not be presented as an
assumption-free estimate of real-world demand.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from .catalog_intake import intake_catalog
from .demand_evidence import inspect_demand_evidence
from .ids import canonical_json, sha256_bytes

#: Observation contract under which the Poisson estimator below is identified.
SUPPORTED_CONTRACT = "iid_poisson_min_sales"

#: Width of the deterministic coarse grid used as independent precision evidence.
GRID_STEP = 0.001
#: Hard upper bound on the search bracket; the fixture data stay far below it.
MAX_BRACKET = 1024.0
#: Maximum number of deterministic grid cells evaluated per fit.
MAX_GRID_POINTS = 100_000
#: Bisection tolerance on lambda.
BISECTION_TOLERANCE = 1e-12
#: Maximum refinement iterations for either solver.
MAX_ITERATIONS = 1000
#: Absolute truncation tolerance for Poisson probability sums.
PROB_TAIL = 1e-15
#: Number of decimals retained in serialized floating-point evidence.
ROUND_DIGITS = 12


class ForecastInputError(ValueError):
    """Raised when evidence cannot support an identified replenishment decision."""


def _r(value: float) -> float:
    """Round evidence floats so repeated runs serialize byte-identically."""
    return round(float(value), ROUND_DIGITS)


def poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass P(D = k), computed in log space."""
    if k < 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def poisson_log_pmf(k: int, lam: float) -> float:
    """log P(D = k)."""
    return -lam + k * math.log(lam) - math.lgamma(k + 1)


def poisson_cdf(k: int, lam: float) -> float:
    """Cumulative probability P(D <= k); returns 0.0 for negative k."""
    if k < 0:
        return 0.0
    term = math.exp(-lam)
    total = term
    for j in range(1, k + 1):
        term *= lam / j
        total += term
    return total


def poisson_log_survival_ge(k: int, lam: float) -> float:
    """log P(D >= k) = log(1 - P(D <= k-1)) for a right-censored day."""
    lower = poisson_cdf(k - 1, lam)
    survival = 1.0 - lower
    if survival <= 0.0:
        raise ForecastInputError(
            "censored tail probability underflowed; data do not support the Poisson contract"
        )
    if lower > 0.5:
        # log1p preserves precision when the survival probability is small.
        return math.log1p(-lower)
    return math.log(survival)


def _negative_log_likelihood(lam: float, days: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in days:
        sold = int(row["sold_units"])
        if row["stockout"]:
            total -= poisson_log_survival_ge(sold, lam)
        else:
            total -= poisson_log_pmf(sold, lam)
    return total


def _score(lam: float, days: list[dict[str, Any]]) -> float:
    """First derivative of the log likelihood with respect to lambda.

    Exact day (D = k):  d/dlam log pmf(k; lam) = k/lam - 1.
    Censored day (D >= k):
        d/dlam log P(D >= k) = pmf(k-1; lam) / P(D >= k),
    because P(D >= k) = 1 - P(D <= k-1) and the derivative of a Poisson CDF
    with respect to its rate is pmf(k-1).
    """
    total = 0.0
    for row in days:
        sold = int(row["sold_units"])
        if row["stockout"]:
            total += poisson_pmf(sold - 1, lam) / (1.0 - poisson_cdf(sold - 1, lam))
        else:
            total += sold / lam - 1.0
    return total


def fit_lambda(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Joint MLE of lambda from exact and right-censored observations.

    The negative log likelihood is strictly convex when at least one exact
    observation exists, so the score has exactly one positive root. A
    deterministic grid records the grid-cell optimum as independent precision
    evidence, and bisection on the analytic score then converges to a fixed
    tolerance. A golden-section fallback on the likelihood itself is used if
    no score sign change can be bracketed.
    """
    if not days:
        raise ForecastInputError("no daily observations available for estimation")
    if all(int(row["sold_units"]) == 0 for row in days):
        # Nothing positive was ever observed: the likelihood peaks at the
        # boundary lambda = 0 and no replenishment model is identified.
        raise ForecastInputError("zero observed sales cannot identify a positive Poisson rate")

    nll = lambda value: _negative_log_likelihood(value, days)

    # Deterministically bracket a sign change of the score by doubling, capped
    # so a degenerate score cannot expand the search without bound.
    lo = GRID_STEP
    hi = 1.0
    while _score(hi, days) > 0.0 and hi < MAX_BRACKET:
        hi *= 2.0
    method = "score_bisection"
    iterations = 0
    if _score(lo, days) <= 0.0 or _score(hi, days) >= 0.0:
        # The root is not inside the bracket (boundary MLE or degenerate
        # score): fall back to gradient-free golden-section minimization.
        method = "golden_section"
        a, b = lo, hi
        golden = (math.sqrt(5.0) + 1.0) / 2.0
        c = b - (b - a) / golden
        d = a + (b - a) / golden
        while abs(b - a) > BISECTION_TOLERANCE and iterations < MAX_ITERATIONS:
            if nll(c) < nll(d):
                b = d
            else:
                a = c
            c = b - (b - a) / golden
            d = a + (b - a) / golden
            iterations += 1
        lam_hat = (a + b) / 2.0
        grid_lo, grid_hi = lo, hi
    else:
        grid_lo, grid_hi = lo, hi
        a, b = lo, hi
        for iterations in range(1, MAX_ITERATIONS + 1):
            mid = (a + b) / 2.0
            if _score(mid, days) > 0.0:
                a = mid
            else:
                b = mid
            if b - a <= BISECTION_TOLERANCE:
                break
        lam_hat = (a + b) / 2.0

    # Independent deterministic-grid precision evidence over the bracket. The
    # cell count is capped defensively; the documented fixtures use ~16k cells.
    point_count = min(MAX_GRID_POINTS, max(1, int((grid_hi - grid_lo) / GRID_STEP) + 1))
    effective_step = (grid_hi - grid_lo) / point_count
    points = [grid_lo + i * effective_step for i in range(point_count + 1)]
    grid_best_lam = points[0]
    grid_best_nll = nll(grid_best_lam)
    for point in points[1:]:
        value = nll(point)
        if value < grid_best_nll:
            grid_best_nll = value
            grid_best_lam = point

    h = 1e-7
    finite_difference_score = -(nll(lam_hat + h) - nll(lam_hat - h)) / (2.0 * h)
    return {
        "lambda_hat": lam_hat,
        "negative_log_likelihood": nll(lam_hat),
        "solver": {
            "method": method,
            "iterations": iterations,
            "bisection_tolerance": BISECTION_TOLERANCE,
            "grid_step": GRID_STEP,
            "effective_grid_step": effective_step,
            "grid_bracket": [grid_lo, grid_hi],
            "grid_points": len(points),
            "grid_best_lambda": grid_best_lam,
            "grid_best_negative_log_likelihood": grid_best_nll,
            "grid_gap": abs(lam_hat - grid_best_lam),
            "analytic_score_at_estimate": _score(lam_hat, days),
            "finite_difference_score": finite_difference_score,
        },
    }


def service_level_quantity(lam: float, target: float) -> dict[str, Any]:
    """Smallest non-negative integer q with P(D <= q) >= target."""
    if not 0.0 < target < 1.0:
        raise ForecastInputError("service level target must lie strictly between 0 and 1")
    q = 0
    probability = poisson_cdf(0, lam)
    while probability < target:
        q += 1
        probability = poisson_cdf(q, lam)
    return {
        "quantity": q,
        "achieved_probability": probability,
        "previous_probability": poisson_cdf(q - 1, lam),
        "target_probability": target,
    }


def _pmf_table(lam: float) -> list[float]:
    """Poisson pmf values up to where the remaining tail is negligible."""
    cap = max(10, int(math.ceil(lam)) * 4)
    while 1.0 - poisson_cdf(cap, lam) > PROB_TAIL:
        cap += 25
    table: list[float] = []
    mass = math.exp(-lam)
    for d in range(cap + 1):
        table.append(mass)
        mass *= lam / (d + 1)
    return table


def evaluate_cost_at_quantity(
    q: int,
    lam: float,
    holding_cost: Decimal,
    lost_margin_rate: Decimal,
    catalog_price: Decimal,
) -> dict[str, Any]:
    """Cost and the two expectation components for a fixed integer q."""
    table = _pmf_table(lam)
    leftover = 0.0
    unmet = 0.0
    for d, probability in enumerate(table):
        if d < q:
            leftover += (q - d) * probability
        elif d > q:
            unmet += (d - q) * probability
    lost_unit_cost = float(lost_margin_rate * catalog_price)
    holding = float(holding_cost)
    return {
        "quantity": q,
        "expected_leftover": leftover,
        "expected_unmet": unmet,
        "expected_cost": holding * leftover + lost_unit_cost * unmet,
    }


def expected_cost_quantity(
    lam: float,
    holding_cost: Decimal,
    lost_margin_rate: Decimal,
    catalog_price: Decimal,
) -> dict[str, Any]:
    """Minimize h E[max(q-D,0)] + (r * price) E[max(D-q,0)] over q >= 0.

    Ties resolve toward the smaller integer. The lost-sales unit cost is the
    contract rate times the catalog price, so a price change flows into the
    decision rather than only into a report fingerprint.
    """
    if holding_cost <= 0 or lost_margin_rate <= 0:
        raise ForecastInputError("cost contract parameters must be positive")
    if catalog_price <= 0:
        raise ForecastInputError("catalog price must be positive")
    holding = float(holding_cost)
    lost_unit_cost = float(lost_margin_rate * catalog_price)

    def expectations(q: int) -> tuple[float, float]:
        evaluation = evaluate_cost_at_quantity(q, lam, holding_cost, lost_margin_rate, catalog_price)
        return evaluation["expected_leftover"], evaluation["expected_unmet"]

    best: dict[str, Any] | None = None
    q = 0
    while True:
        leftover, unmet = expectations(q)
        cost = holding * leftover + lost_unit_cost * unmet
        if best is None or cost < best["expected_cost"]:  # strict: ties keep smaller q
            best = {
                "quantity": q,
                "expected_cost": cost,
                "expected_leftover": leftover,
                "expected_unmet": unmet,
                "holding_unit_cost": holding,
                "lost_sales_unit_cost": lost_unit_cost,
            }
        # Cost is eventually convex and grows without bound in q; stop once we
        # are past the minimum and no unmet-demand mass remains in the tail.
        if q > best["quantity"] and cost > best["expected_cost"] and unmet < PROB_TAIL:
            break
        q += 1
    return best  # type: ignore[return-value]


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ForecastInputError(f"decision parameter {field} is not numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ForecastInputError(f"decision parameter {field} must be positive and finite")
    return parsed


def decide_replenishment(run_dir: str, catalog_manifest: str, history_path: str) -> dict[str, Any]:
    """Verify all sources, fit lambda jointly, and evaluate both contracts."""
    evidence = inspect_demand_evidence(run_dir, catalog_manifest, history_path)
    if evidence.get("observation_contract") != SUPPORTED_CONTRACT:
        raise ForecastInputError("observation contract does not identify a Poisson demand model")
    days = evidence["days"]
    exact_days = sum(1 for row in days if not row["stockout"])
    censored_days = len(days) - exact_days
    if exact_days == 0:
        raise ForecastInputError("every day is censored: lambda is not identified without exact days")

    fit = fit_lambda(days)
    lam = fit["lambda_hat"]

    catalog = intake_catalog(run_dir, catalog_manifest)
    offer = next(row for row in catalog["offers"] if row["sku"] == evidence["sku"])
    price = Decimal(offer["price_cny"])

    per_day: list[dict[str, Any]] = []
    for row in days:
        sold = int(row["sold_units"])
        offered = int(row["offered_units"])
        if row["stockout"]:
            tail = 1.0 - poisson_cdf(sold - 1, lam)
            log_tail = poisson_log_survival_ge(sold, lam)
            per_day.append({
                "day": row["day"],
                "kind": "right_censored",
                "sold_units": sold,
                "offered_units": offered,
                "constraint": f"D >= {sold}",
                "tail_probability": _r(tail),
                "log_tail_probability": _r(log_tail),
                "log_likelihood_contribution": _r(log_tail),
            })
        else:
            log_pmf = poisson_log_pmf(sold, lam)
            per_day.append({
                "day": row["day"],
                "kind": "exact",
                "sold_units": sold,
                "offered_units": offered,
                "constraint": f"D = {sold}",
                "point_probability": _r(poisson_pmf(sold, lam)),
                "log_point_probability": _r(log_pmf),
                "log_likelihood_contribution": _r(log_pmf),
            })
    per_day.sort(key=lambda row: row["day"])

    contracts: list[dict[str, Any]] = []
    for decision in evidence["decisions"]:
        kind = decision.get("kind")
        contract_id = str(decision.get("contract_id", kind))
        if kind == "service_level":
            target = _positive_decimal(decision.get("target"), "target")
            if not Decimal(0) < target < Decimal(1):
                raise ForecastInputError("service level target must lie strictly between 0 and 1")
            outcome = service_level_quantity(lam, float(target))
            contracts.append({
                "contract_id": contract_id,
                "kind": kind,
                "rule": "smallest non-negative integer q with P(D <= q) >= target",
                "target": format(target, "f"),
                "recommended_units": outcome["quantity"],
                "objective_value": _r(outcome["achieved_probability"]),
                "achieved_probability": _r(outcome["achieved_probability"]),
                "previous_probability": _r(outcome["previous_probability"]),
            })
        elif kind == "expected_cost":
            holding = _positive_decimal(decision.get("holding_cost"), "holding_cost")
            rate = _positive_decimal(decision.get("lost_margin_rate"), "lost_margin_rate")
            outcome = expected_cost_quantity(lam, holding, rate, price)
            contracts.append({
                "contract_id": contract_id,
                "kind": kind,
                "rule": (
                    "minimize holding_cost * E[max(q-D,0)] + "
                    "(lost_margin_rate * catalog_price) * E[max(D-q,0)]; "
                    "equal costs resolve to the smaller q"
                ),
                "holding_cost": format(holding, "f"),
                "lost_margin_rate": format(rate, "f"),
                "lost_sales_unit_cost_cny": format(
                    Decimal(str(outcome["lost_sales_unit_cost"])).quantize(Decimal("0.000000000001")), "f"
                ),
                "recommended_units": outcome["quantity"],
                "objective_value": _r(outcome["expected_cost"]),
                "expected_leftover_units": _r(outcome["expected_leftover"]),
                "expected_unmet_units": _r(outcome["expected_unmet"]),
            })
        else:
            raise ForecastInputError(f"unknown decision contract kind: {kind!r}")
    contracts.sort(key=lambda row: row["contract_id"])
    if len({row["contract_id"] for row in contracts}) != len(contracts):
        raise ForecastInputError("decision contract ids must be unique")

    result = {
        "schema_version": 1,
        "item_id": offer["item_id"],
        "sku": evidence["sku"],
        "catalog_fingerprint": evidence["catalog_fingerprint"],
        "evidence_fingerprint": evidence["evidence_fingerprint"],
        "catalog_price_cny": offer["price_cny"],
        "observation_contract": SUPPORTED_CONTRACT,
        "identification_assumption": (
            "latent daily demands are iid Poisson(lambda); exact days observe "
            "D = sold and stockout days only observe D >= sold. Estimates are "
            "conditional on this synthetic contract and are not assumption-free "
            "measurements of real demand."
        ),
        "observation_counts": {
            "total_days": len(days),
            "exact_days": exact_days,
            "right_censored_days": censored_days,
        },
        "estimate": {
            "lambda_hat": _r(lam),
            "negative_log_likelihood": _r(fit["negative_log_likelihood"]),
            "solver": {
                **fit["solver"],
                "effective_grid_step": _r(fit["solver"]["effective_grid_step"]),
                "grid_best_lambda": _r(fit["solver"]["grid_best_lambda"]),
                "grid_best_negative_log_likelihood": _r(
                    fit["solver"]["grid_best_negative_log_likelihood"]
                ),
                "grid_gap": _r(fit["solver"]["grid_gap"]),
                "analytic_score_at_estimate": _r(fit["solver"]["analytic_score_at_estimate"]),
                "finite_difference_score": _r(fit["solver"]["finite_difference_score"]),
                "bisection_tolerance": BISECTION_TOLERANCE,
                "grid_step": GRID_STEP,
            },
            "per_day": per_day,
        },
        "contracts": contracts,
    }
    result["forecast_fingerprint"] = sha256_bytes(canonical_json(result))
    return result
