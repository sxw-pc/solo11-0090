"""Bind a safe catalog to a synthetic regional-content graph.

The preview checks caller-supplied fixed-point assignments. It does not find
the full feasible set or quote a duty under either synthetic contract.
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any


class OriginEvidenceError(ValueError):
    pass


def inspect_origin_contract(catalog: dict[str, Any], path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OriginEvidenceError("invalid origin contract") from exc
    offers = {row["item_id"]: row for row in catalog["offers"]}
    terminals = data.get("terminal_shares")
    bom = data.get("bill_of_materials")
    constraints = data.get("independent_evidence_constraints")
    if (data.get("schema_version") != 1 or len(offers) != 7
            or data.get("finished_item_id") not in offers
            or data.get("regional_threshold") != "9/20"
            or data.get("standard_duty_rate") != "1/10"
            or data.get("preference_duty_rate") != "3/100"
            or data.get("contracts") != ["guaranteed_origin_quote", "plausible_origin_quote"]
            or not isinstance(terminals, dict) or not isinstance(bom, dict)
            or set(terminals) | set(bom) != set(offers)
            or set(terminals) & set(bom)
            or not isinstance(constraints, list) or len(constraints) != 3):
        raise OriginEvidenceError("unsupported origin graph")
    try:
        terminal_values = {key: Fraction(value) for key, value in terminals.items()}
        graph = {}
        for key, inputs in bom.items():
            if not isinstance(inputs, list) or not inputs:
                raise OriginEvidenceError("empty bill of materials")
            pairs = [(row["input"], Fraction(row["fraction"])) for row in inputs]
            if (any(item not in offers or weight <= 0 for item, weight in pairs)
                    or len({item for item, _ in pairs}) != len(pairs)
                    or sum(weight for _, weight in pairs) != 1):
                raise OriginEvidenceError("invalid bill fractions")
            graph[key] = [{"input": item, "fraction": str(weight)} for item, weight in pairs]
        if any(value < 0 or value > 1 for value in terminal_values.values()):
            raise OriginEvidenceError("invalid terminal share")
        if (constraints != [
            {"left": "itm-0004", "op": ">=", "right": "1/4"},
            {"left": "itm-0006", "op": ">=", "right": "1/4"},
            {"terms": [{"item_id": "itm-0004", "coefficient": "1"},
                       {"item_id": "itm-0006", "coefficient": "1"}], "op": "<=", "right": "1"},
        ]):
            raise OriginEvidenceError("independent evidence constraints differ")
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        if isinstance(exc, OriginEvidenceError):
            raise
        raise OriginEvidenceError("invalid origin graph values") from exc
    return {
        "run_id": catalog["run_id"],
        "audit_fingerprint": catalog["audit_fingerprint"],
        "catalog_fingerprint": catalog["catalog_fingerprint"],
        "finished_item_id": data["finished_item_id"],
        "finished_price_cny": offers[data["finished_item_id"]]["price_cny"],
        "regional_threshold": data["regional_threshold"],
        "standard_duty_rate": data["standard_duty_rate"],
        "preference_duty_rate": data["preference_duty_rate"],
        "terminal_shares": {key: str(value) for key, value in terminal_values.items()},
        "bill_of_materials": graph,
        "independent_evidence_constraints": constraints,
        "contracts": data["contracts"],
        "origin_share_bounds": None,
        "duty_quotes": None,
    }


def preview_origin_assignment(evidence: dict[str, Any], assignment: dict[str, str]) -> dict[str, Any]:
    keys = set(evidence["terminal_shares"]) | set(evidence["bill_of_materials"])
    if set(assignment) != keys:
        raise OriginEvidenceError("assignment item identities differ")
    values = {key: Fraction(value) for key, value in assignment.items()}
    if any(value < 0 or value > 1 for value in values.values()):
        raise OriginEvidenceError("assignment share outside unit interval")
    fixed_points = {key: value == Fraction(evidence["terminal_shares"][key])
                    for key, value in values.items() if key in evidence["terminal_shares"]}
    for key, inputs in evidence["bill_of_materials"].items():
        fixed_points[key] = values[key] == sum(Fraction(row["fraction"]) * values[row["input"]]
                                                  for row in inputs)
    independent = (values["itm-0004"] >= Fraction(1, 4)
                   and values["itm-0006"] >= Fraction(1, 4)
                   and values["itm-0004"] + values["itm-0006"] <= 1)
    return {
        "internal_preview_only": True,
        "publishable": False,
        "assignment": {key: str(value) for key, value in values.items()},
        "fixed_point_checks": fixed_points,
        "independent_constraints_met": independent,
        "valid": all(fixed_points.values()) and independent,
        "finished_regional_share": str(values[evidence["finished_item_id"]]),
    }
