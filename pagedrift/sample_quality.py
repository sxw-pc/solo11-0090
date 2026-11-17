"""Validate a reproducible category sample from a safe pagination run.

The baseline reports only observed sample frequency. It does not infer finite
population quality or make a release decision.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .evidence import verify_run
from .ids import canonical_json, sha256_bytes


class SampleQualityError(ValueError):
    pass


def inspect_sample(run_dir: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
    if audit.get("summary", {}).get("verdict") != "safe" or verify_run(root, None)["status"] != "OK":
        raise SampleQualityError("pagination evidence is unsafe")
    rows = [json.loads(line) for line in (root / "records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
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
    contracts = manifest.get("release_contracts")
    if not isinstance(contracts, list) or {c.get("kind") for c in contracts} != {"overall_max", "each_category_max"}:
        raise SampleQualityError("missing release contracts")
    strata = []
    for category, ids in sorted(groups.items()):
        sample_ids = sorted(set(ids) & expected)
        strata.append({"category": category, "population": len(ids), "sample": len(sample_ids),
                       "observed_defects": sum(found[i] for i in sample_ids)})
    result = {
        "run_id": audit["run_id"],
        "audit_fingerprint": audit["audit_fingerprint"],
        "sampling_design": manifest["sampling_design"],
        "seed": seed,
        "strata": strata,
        "contracts": contracts,
        "naive_sample_rate": f"{sum(found.values())}/{len(found)}",
        "population_upper_bounds": None,
        "release_decisions": None,
    }
    result["sample_fingerprint"] = sha256_bytes(canonical_json(result))
    return result
