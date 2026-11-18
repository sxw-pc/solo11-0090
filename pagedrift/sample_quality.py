"""Bounded, explainable release decisions from a reproducible category sample.

The sample-quality gate re-checks a safe pagination run (safe audit verdict
plus byte-exact page evidence), replays the fixed ``srswor_by_category``
sampling design (seeded simple random sampling without replacement per
category), and requires the human inspection receipts to match the selected
items exactly. Any drift between the receipts and the fixed design aborts the
gate instead of annotating a stale decision.

From the validated receipts the gate derives finite-population quality bounds
instead of quoting the naive sample rate:

* a fully inspected category contributes its exact known defect total;
* a partially sampled category inverts the one-sided 5% hypergeometric
  rejection region over integer defect totals K = 0..N (never a binomial /
  infinite-population approximation), yielding a conservative one-sided 95%
  upper confidence bound for the category defect count.

The overall bound is the sum of the category bounds, so a real defect in a
fully inspected category can never be washed out by clean samples elsewhere,
and uninspected items are never assumed defect-free. Two release contracts are
evaluated on the same evidence but different scopes: ``overall_max`` bounds
the whole-catalog defect rate, ``each_category_max`` bounds every category
rate individually. Both decisions are reported with their reasons; neither
scope is silently substituted for the other.
"""

from __future__ import annotations

import json
import random
from fractions import Fraction
from math import comb
from pathlib import Path
from typing import Any

from .evidence import verify_run
from .ids import canonical_json, sha256_bytes

ALPHA = Fraction(1, 20)  # one-sided 5% rejection region -> 95% confidence
CONFIDENCE_LEVEL = "0.95"
ALPHA_LABEL = "0.05"
CONTRACT_KINDS = ("overall_max", "each_category_max")
BOUND_METHOD_FULL = "exact_full_inspection"
BOUND_METHOD_INVERSION = "hypergeometric_inversion"
DECISION_PUBLISHABLE = "publishable"
DECISION_BLOCKED = "blocked"


class SampleQualityError(ValueError):
    pass


def _hypergeom_tail(defects: int, population: int, sample: int, observed: int) -> Fraction:
    """P(X <= observed) for X ~ Hypergeometric(N=population, K=defects, n=sample)."""
    lower = max(0, sample - (population - defects))
    upper = min(observed, defects)
    numerator = 0
    for drawn in range(lower, upper + 1):
        numerator += comb(defects, drawn) * comb(population - defects, sample - drawn)
    return Fraction(numerator, comb(population, sample))


def _upper_defect_bound(population: int, sample: int, observed: int) -> tuple[int, Fraction, Fraction | None]:
    """One-sided 95% upper bound for the total defect count K.

    A candidate total K is rejected when P(X <= observed | K) <= ALPHA; the
    bound is the largest non-rejected integer K in 0..N. Fully inspected
    categories collapse to the exact observed total. Returns the bound plus
    the tail probabilities at the bound and one above it (the inversion
    witness: the bound is accepted, bound + 1 is rejected).
    """
    bound = observed
    for candidate in range(population, observed - 1, -1):
        if _hypergeom_tail(candidate, population, sample, observed) > ALPHA:
            bound = candidate
            break
    tail_at = _hypergeom_tail(bound, population, sample, observed)
    tail_above = None
    if bound < population:
        tail_above = _hypergeom_tail(bound + 1, population, sample, observed)
    return bound, tail_at, tail_above


def _decimal(value: Fraction, places: int = 6) -> str:
    """Deterministic decimal rendering of an exact fraction."""
    quotient, remainder = divmod(value.numerator * 10**places, value.denominator)
    if remainder * 2 >= value.denominator:
        quotient += 1
    whole, fraction = divmod(quotient, 10**places)
    return f"{whole}.{fraction:0{places}d}"


def _tail_entry(defects: int, probability: Fraction) -> dict[str, Any]:
    return {
        "defects": defects,
        "p_le_observed": str(probability),
        "p_le_observed_decimal": _decimal(probability),
    }


def _parse_threshold(value: Any) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SampleQualityError("invalid contract threshold")
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise SampleQualityError("invalid contract threshold") from exc
    if rate < 0 or rate > 1:
        raise SampleQualityError("invalid contract threshold")
    return rate


def _parse_contracts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    contracts = manifest.get("release_contracts")
    if not isinstance(contracts, list) or len(contracts) != len(CONTRACT_KINDS):
        raise SampleQualityError("missing release contracts")
    parsed = []
    seen_ids: set[str] = set()
    seen_kinds: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, dict):
            raise SampleQualityError("invalid release contract")
        contract_id = contract.get("contract_id")
        kind = contract.get("kind")
        if (
            not isinstance(contract_id, str)
            or not contract_id
            or contract_id in seen_ids
            or kind not in CONTRACT_KINDS
            or kind in seen_kinds
        ):
            raise SampleQualityError("invalid release contract")
        threshold = _parse_threshold(contract.get("max_rate"))
        seen_ids.add(contract_id)
        seen_kinds.add(kind)
        parsed.append(
            {
                "contract_id": contract_id,
                "kind": kind,
                "max_rate_raw": contract.get("max_rate"),
                "threshold": threshold,
            }
        )
    if seen_kinds != set(CONTRACT_KINDS):
        raise SampleQualityError("missing release contracts")
    return parsed


def _category_bound(category: str, population: int, sample: int, observed: int) -> dict[str, Any]:
    upper, tail_at, tail_above = _upper_defect_bound(population, sample, observed)
    rate = Fraction(upper, population)
    return {
        "category": category,
        "population": population,
        "sample": sample,
        "observed_defects": observed,
        "bound_method": BOUND_METHOD_FULL if sample == population else BOUND_METHOD_INVERSION,
        "upper_defects": upper,
        "upper_rate": str(rate),
        "upper_rate_decimal": _decimal(rate),
        "tail_at_bound": _tail_entry(upper, tail_at),
        "tail_above_bound": _tail_entry(upper + 1, tail_above) if tail_above is not None else None,
    }


def _decide_overall(contract: dict[str, Any], overall: dict[str, Any]) -> dict[str, Any]:
    rate = Fraction(overall["upper_defects"], overall["population"])
    publishable = rate <= contract["threshold"]
    comparison = "does not exceed" if publishable else "exceeds"
    return {
        "contract_id": contract["contract_id"],
        "kind": contract["kind"],
        "max_rate": contract["max_rate_raw"],
        "scope": "overall",
        "decision": DECISION_PUBLISHABLE if publishable else DECISION_BLOCKED,
        "measured": dict(overall),
        "blocking_categories": [],
        "reason": (
            f"overall upper bound {overall['upper_defects']}/{overall['population']} "
            f"(rate {overall['upper_rate']} = {overall['upper_rate_decimal']}) "
            f"{comparison} contract max {contract['max_rate_raw']}"
        ),
    }


def _decide_each_category(contract: dict[str, Any], by_category: list[dict[str, Any]]) -> dict[str, Any]:
    blocking = [
        entry
        for entry in by_category
        if Fraction(entry["upper_defects"], entry["population"]) > contract["threshold"]
    ]
    publishable = not blocking
    if publishable:
        reason = (
            f"all {len(by_category)} category upper bounds do not exceed "
            f"contract max {contract['max_rate_raw']}"
        )
    else:
        details = ", ".join(
            f"{entry['category']} {entry['upper_defects']}/{entry['population']} "
            f"(rate {entry['upper_rate']} = {entry['upper_rate_decimal']})"
            for entry in blocking
        )
        reason = f"category upper bounds exceed contract max {contract['max_rate_raw']}: {details}"
    return {
        "contract_id": contract["contract_id"],
        "kind": contract["kind"],
        "max_rate": contract["max_rate_raw"],
        "scope": "each_category",
        "decision": DECISION_PUBLISHABLE if publishable else DECISION_BLOCKED,
        "measured": None,
        "blocking_categories": [
            {
                "category": entry["category"],
                "population": entry["population"],
                "upper_defects": entry["upper_defects"],
                "upper_rate": entry["upper_rate"],
                "upper_rate_decimal": entry["upper_rate_decimal"],
            }
            for entry in blocking
        ],
        "reason": reason,
    }


def inspect_sample(run_dir: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
    if audit.get("summary", {}).get("verdict") != "safe" or verify_run(root, None)["status"] != "OK":
        raise SampleQualityError("pagination evidence is unsafe")
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (root / "records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise SampleQualityError("pagination run has no records")
    groups: dict[str, list[str]] = {}
    all_ids: set[str] = set()
    for row in rows:
        item_id = str(row.get("id", ""))
        category = str(row.get("category", ""))
        if not item_id or not category or item_id in all_ids:
            raise SampleQualityError("invalid or duplicate pagination item")
        all_ids.add(item_id)
        groups.setdefault(category, []).append(item_id)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("sampling_design") != "srswor_by_category":
        raise SampleQualityError("unknown sample design")
    seed = manifest.get("seed")
    sizes = manifest.get("sample_sizes")
    if type(seed) is not int or not isinstance(sizes, dict) or set(sizes) != set(groups):
        raise SampleQualityError("sample design does not cover categories")
    expected: set[str] = set()
    for category, ids in groups.items():
        n = sizes[category]
        if type(n) is not int or n < 1 or n > len(ids):
            raise SampleQualityError("sample size outside category")
        expected.update(random.Random(seed).sample(sorted(ids), n))
    observations = manifest.get("observations")
    if not isinstance(observations, list):
        raise SampleQualityError("missing sample observations")
    found: dict[str, bool] = {}
    for row in observations:
        item_id = row.get("item_id") if isinstance(row, dict) else None
        defect = row.get("defect") if isinstance(row, dict) else None
        if not isinstance(item_id, str) or type(defect) is not bool or item_id in found:
            raise SampleQualityError("invalid or duplicate inspection receipt")
        found[item_id] = defect
    if set(found) != expected:
        raise SampleQualityError("inspection receipts do not match selected sample")
    contracts = _parse_contracts(manifest)
    strata = []
    for category, ids in sorted(groups.items()):
        sample_ids = sorted(set(ids) & expected)
        strata.append({"category": category, "population": len(ids), "sample": len(sample_ids),
                       "observed_defects": sum(found[i] for i in sample_ids)})
    by_category = [
        _category_bound(s["category"], s["population"], s["sample"], s["observed_defects"])
        for s in strata
    ]
    total_population = sum(s["population"] for s in strata)
    total_upper = sum(entry["upper_defects"] for entry in by_category)
    overall_rate = Fraction(total_upper, total_population)
    overall = {
        "population": total_population,
        "upper_defects": total_upper,
        "upper_rate": str(overall_rate),
        "upper_rate_decimal": _decimal(overall_rate),
    }
    decisions = []
    for contract in contracts:
        if contract["kind"] == "overall_max":
            decisions.append(_decide_overall(contract, overall))
        else:
            decisions.append(_decide_each_category(contract, by_category))
    result = {
        "run_id": audit["run_id"],
        "universe_sha256": run.get("universe_sha256"),
        "audit_fingerprint": audit["audit_fingerprint"],
        "sampling_design": manifest["sampling_design"],
        "seed": seed,
        "strata": strata,
        "contracts": manifest["release_contracts"],
        "naive_sample_rate": f"{sum(found.values())}/{len(found)}",
        "population_upper_bounds": {
            "confidence_level": CONFIDENCE_LEVEL,
            "alpha": ALPHA_LABEL,
            "method": (
                "one-sided hypergeometric inversion over integer defect totals "
                "for srswor_by_category; fully inspected categories are exact"
            ),
            "by_category": by_category,
            "overall": overall,
        },
        "release_decisions": decisions,
    }
    result["sample_fingerprint"] = sha256_bytes(canonical_json(result))
    return result
