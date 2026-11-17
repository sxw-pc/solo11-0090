"""Validate catalog-backed warehouse opportunities without promising any order."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def inspect_warehouse_promise(catalog: dict[str, Any], manifest_path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("contracts") != ["local_only", "transfer_authorized"]:
        raise ValueError("unknown warehouse promise contract")
    offers = [row for row in catalog.get("offers", []) if row.get("sku") == manifest["catalog_sku"]]
    if len(offers) != 1 or not catalog.get("catalog_fingerprint") or not catalog.get("audit_fingerprint"):
        raise ValueError("unverified or ambiguous catalog sku")
    batches = manifest["batches"]
    orders = manifest["orders"]
    lanes = manifest["transfer_lanes"]
    if len({b["batch_id"] for b in batches}) != len(batches) or len({o["order_id"] for o in orders}) != len(orders):
        raise ValueError("duplicate batch or order identity")
    if sum(b["units"] for b in batches) != offers[0]["available_units"]:
        raise ValueError("batch stock differs from catalog offer")
    claims = manifest.get("claim_attempts", [])
    if len({c["claim_id"] for c in claims}) != len(claims) or any(c["order_id"] not in {o["order_id"] for o in orders} or c["read_epoch"] != 1 for c in claims):
        raise ValueError("invalid claim attempt identity")
    arcs = []
    for order in orders:
        for batch in batches:
            if batch["warehouse"] == order["warehouse"]:
                handoff = order["ready_day"]
                if handoff <= min(order["deadline_day"], batch["expires_day"]):
                    arcs.append({"order_id":order["order_id"],"batch_id":batch["batch_id"],"route":"local","handoff_day":handoff})
            for lane in lanes:
                if lane["from"] == batch["warehouse"] and lane["to"] == order["warehouse"] and lane["depart_day"] >= order["ready_day"]:
                    handoff = lane["depart_day"] + lane["transit_days"]
                    if handoff <= min(order["deadline_day"], batch["expires_day"]):
                        arcs.append({"order_id":order["order_id"],"batch_id":batch["batch_id"],"route":f'{lane["from"]}->{lane["to"]}',"handoff_day":handoff})
    return {"run_id":catalog["run_id"],"audit_fingerprint":catalog["audit_fingerprint"],"catalog_fingerprint":catalog["catalog_fingerprint"],"sku":manifest["catalog_sku"],"batches":batches,"orders":orders,"transfer_lanes":lanes,"claim_attempts":claims,"eligible_assignments":arcs,"contracts":manifest["contracts"],"promise_decisions":None,"reservation_receipts":None}

