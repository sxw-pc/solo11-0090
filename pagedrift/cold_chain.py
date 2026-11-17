"""Inspect a bounded cold-chain gap without certifying its worst exposure."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any
from .warehouse_promise import inspect_warehouse_promise


def _points(values: list[dict[str, str]]) -> list[tuple[Fraction, Fraction]]:
    points = [(Fraction(str(row["hour"])), Fraction(str(row["temperature_c"]))) for row in values]
    if len(points) < 2 or any(right[0] <= left[0] for left, right in zip(points, points[1:])):
        raise ValueError("temperature times must increase")
    return points


def _excess_area(points: list[tuple[Fraction, Fraction]], cutoff: Fraction) -> Fraction:
    area = Fraction(0)
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        a, b, dt = v0 - cutoff, v1 - cutoff, t1 - t0
        if a <= 0 and b <= 0:
            continue
        if a >= 0 and b >= 0:
            area += (a + b) * dt / 2
        elif a > 0:
            area += a * a * dt / (2 * (a - b))
        else:
            area += b * b * dt / (2 * (b - a))
    return area


def inspect_cold_chain(catalog: dict[str, Any], warehouse_manifest: str | Path,
                       thermal_manifest: str | Path) -> dict[str, Any]:
    warehouse = inspect_warehouse_promise(catalog, warehouse_manifest)
    data = json.loads(Path(thermal_manifest).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("sku") != warehouse["sku"]:
        raise ValueError("thermal sku does not match verified catalog")
    batch = next((row for row in warehouse["batches"] if row["batch_id"] == data["batch_id"]), None)
    if batch is None or data.get("contracts") != ["linear_assumed", "bounded_certified"]:
        raise ValueError("thermal batch or contracts invalid")
    observed = _points(data["observations"])
    witness = _points(data["illustrative_hot_path"])
    if witness[0] != observed[1] or witness[-1] != observed[2]:
        raise ValueError("hot path does not attach to observed gap endpoints")
    rate = Fraction(str(data["max_rate_c_per_hour"]))
    cutoff = Fraction(str(data["exposure_cutoff_c"]))
    if rate <= 0 or any(abs(v1-v0) > rate*(t1-t0) for (t0,v0),(t1,v1) in zip(witness,witness[1:])):
        raise ValueError("illustrative path violates rate bound")
    return {"run_id":warehouse["run_id"],"catalog_fingerprint":warehouse["catalog_fingerprint"],
            "sku":warehouse["sku"],"batch_id":batch["batch_id"],"observations":data["observations"],
            "max_rate_c_per_hour":data["max_rate_c_per_hour"],"cutoff_c":data["exposure_cutoff_c"],
            "max_degree_hours":data["max_degree_hours"],"contracts":data["contracts"],
            "illustrative_hot_path":data["illustrative_hot_path"],
            "illustrative_hot_path_exposure":str(_excess_area(witness,cutoff)),
            "linear_interpolation_exposure":str(_excess_area(observed,cutoff)),
            "candidate_arcs_for_batch":[arc for arc in warehouse["eligible_assignments"] if arc["batch_id"] == batch["batch_id"]],
            "certified_exposure_upper":None,"release_decisions":None}
