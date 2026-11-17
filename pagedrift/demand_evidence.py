"""Bind a synthetic daily sales log to a verified catalog without estimating demand."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .catalog_intake import intake_catalog
from .ids import canonical_json, sha256_bytes


class DemandInputError(ValueError):
    pass


def inspect_demand_evidence(run_dir: str | Path, catalog_manifest: str | Path, history_path: str | Path) -> dict[str, Any]:
    catalog = intake_catalog(run_dir, catalog_manifest)
    history = json.loads(Path(history_path).read_text(encoding="utf-8"))
    if history.get("schema_version") != 1 or history.get("observation_contract") != "iid_poisson_min_sales":
        raise DemandInputError("unknown observation contract")
    sku = history.get("sku")
    offer_by_sku = {offer["sku"]: offer for offer in catalog["offers"]}
    if sku not in offer_by_sku:
        raise DemandInputError("history sku is not in verified catalog")
    days = history.get("days")
    if not isinstance(days, list) or not days:
        raise DemandInputError("history has no daily observations")
    validated = []
    seen = set()
    for row in days:
        try:
            day = date.fromisoformat(row["day"]).isoformat()
            offered = int(row["offered_units"])
            sold = int(row["sold_units"])
            stockout = row["stockout"]
        except (KeyError, ValueError, TypeError) as exc:
            raise DemandInputError("invalid daily observation") from exc
        if day in seen or offered <= 0 or sold < 0 or sold > offered or type(stockout) is not bool:
            raise DemandInputError("daily observation violates inventory bounds")
        if stockout and sold != offered:
            raise DemandInputError("stockout receipt conflicts with sales")
        seen.add(day)
        validated.append({"day": day, "offered_units": offered, "sold_units": sold, "stockout": stockout})
    validated.sort(key=lambda row: row["day"])
    decisions = history.get("decisions")
    if not isinstance(decisions, list) or {d.get("kind") for d in decisions} != {"service_level", "expected_cost"}:
        raise DemandInputError("missing decision contracts")
    for decision in decisions:
        for key in ("target", "holding_cost", "lost_margin_rate"):
            if key in decision and (not Decimal(str(decision[key])).is_finite() or Decimal(str(decision[key])) <= 0):
                raise DemandInputError("invalid decision parameter")
    result = {
        "catalog_fingerprint": catalog["catalog_fingerprint"],
        "sku": sku,
        "catalog_price_cny": offer_by_sku[sku]["price_cny"],
        "observation_contract": history["observation_contract"],
        "days": validated,
        "decisions": decisions,
        "estimate": None,
        "recommended_units": None,
    }
    result["evidence_fingerprint"] = sha256_bytes(canonical_json(result))
    return result
