"""Offline catalog intake from a verified pagination run.

The intake establishes source identity and offer facts. It deliberately makes no
inventory allocation, checkout, or publication decision.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .evidence import verify_run
from .ids import canonical_json, sha256_bytes


class CatalogInputError(ValueError):
    pass


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogInputError(f"cannot read catalog input {path.name}: {exc}") from exc


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise CatalogInputError(f"cannot read pagination records: {exc}") from exc


def intake_catalog(run_dir: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    audit = _json(root / "audit.json")
    if audit.get("summary", {}).get("verdict") != "safe":
        raise CatalogInputError("pagination run is not safe for catalog intake")
    evidence = verify_run(root, None)
    if evidence["status"] != "OK":
        raise CatalogInputError("pagination page evidence is not intact")
    rows = _jsonl(root / "records.jsonl")
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("id", ""))
        if not key or key in seen:
            raise CatalogInputError("pagination records contain duplicate or empty item id")
        seen[key] = row
    manifest = _json(Path(manifest_path))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("offers"), list):
        raise CatalogInputError("catalog manifest has no versioned offers")
    offers = []
    sku_seen = set()
    for offer in manifest["offers"]:
        if not isinstance(offer, dict):
            raise CatalogInputError("catalog offer must be an object")
        item_id = str(offer.get("item_id", ""))
        sku = str(offer.get("sku", ""))
        if item_id not in seen or not sku or sku in sku_seen:
            raise CatalogInputError("catalog offer has unknown item or duplicate sku")
        sku_seen.add(sku)
        try:
            price = Decimal(str(offer["price_cny"]))
            quantity = int(offer["available_units"])
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise CatalogInputError("catalog offer has invalid price or quantity") from exc
        if not price.is_finite() or price <= 0 or quantity < 0:
            raise CatalogInputError("catalog offer has invalid price or quantity")
        offers.append({
            "item_id": item_id,
            "sku": sku,
            "price_cny": format(price, "f"),
            "available_units": quantity,
            "category": seen[item_id]["category"],
            "payload_sha8": seen[item_id]["payload_sha8"],
        })
    offers.sort(key=lambda row: (row["sku"], row["item_id"]))
    result = {
        "schema_version": 1,
        "run_id": audit["run_id"],
        "audit_fingerprint": audit["audit_fingerprint"],
        "offers": offers,
    }
    result["catalog_fingerprint"] = sha256_bytes(canonical_json(result))
    return result
