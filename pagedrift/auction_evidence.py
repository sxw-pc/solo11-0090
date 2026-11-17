"""Verify a sealed-bid bundle auction against a safe local pagination run.

The baseline validates listings, bid identities and payment contracts. It does
not select winners or calculate clearing charges.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .evidence import verify_run


class AuctionEvidenceError(ValueError):
    pass


def inspect_auction(run_dir: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
    if audit.get("summary", {}).get("verdict") != "safe" or verify_run(root, None)["status"] != "OK":
        raise AuctionEvidenceError("pagination listing evidence is unsafe")
    listed = set()
    for line in (root / "records.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        item_id = row.get("id")
        if not isinstance(item_id, str) or item_id in listed:
            raise AuctionEvidenceError("duplicate or invalid listing identity")
        listed.add(item_id)
    raw = Path(manifest_path).read_bytes()
    manifest = json.loads(raw)
    resources = manifest.get("resource_item_ids")
    capacity = manifest.get("resource_capacity")
    if (manifest.get("schema_version") != 1 or not isinstance(manifest.get("auction_id"), str)
            or not isinstance(resources, list) or not resources or len(set(resources)) != len(resources)
            or not set(resources) <= listed or not isinstance(capacity, dict) or set(capacity) != set(resources)
            or any(value != 1 for value in capacity.values())
            or manifest.get("winner_limit_per_owner") != 1):
        raise AuctionEvidenceError("auction resource contract differs from verified listings")
    bids = manifest.get("bids")
    if not isinstance(bids, list) or not bids:
        raise AuctionEvidenceError("no bid receipts")
    bid_ids = set()
    for bid in bids:
        if not isinstance(bid, dict):
            raise AuctionEvidenceError("invalid bid receipt")
        bid_id, owner, bundle, amount = (bid.get("bid_id"), bid.get("owner_id"),
                                         bid.get("bundle"), bid.get("amount_cents"))
        if (not isinstance(bid_id, str) or not bid_id or bid_id in bid_ids
                or not isinstance(owner, str) or not owner
                or not isinstance(bundle, list) or not bundle or len(set(bundle)) != len(bundle)
                or not set(bundle) <= set(resources) or type(amount) is not int or amount <= 0):
            raise AuctionEvidenceError("invalid bid identity, bundle, or amount")
        bid_ids.add(bid_id)
    contracts = manifest.get("settlement_contracts")
    if (not isinstance(contracts, list)
            or {row.get("kind") for row in contracts} != {"pay_as_bid", "vcg_externality"}):
        raise AuctionEvidenceError("missing payment contracts")
    return {
        "run_id": audit["run_id"], "audit_fingerprint": audit["audit_fingerprint"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "auction_id": manifest["auction_id"], "resource_item_ids": resources,
        "resource_capacity": capacity, "winner_limit_per_owner": 1,
        "bids": bids, "settlement_contracts": contracts,
        "welfare_optimum": None, "winner_set": None, "settlements": None,
    }
