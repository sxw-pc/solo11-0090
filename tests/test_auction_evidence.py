from __future__ import annotations

from pathlib import Path

import pytest

from pagedrift.auction_evidence import AuctionEvidenceError, inspect_auction
from pagedrift.pipeline import run_demo

FIXTURES = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "auction"


def test_safe_listing_run_binds_bundle_bid_receipts(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    result = inspect_auction(tmp_path / "demo" / "snapshot", FIXTURES / "bids.json")
    assert result["resource_item_ids"] == ["itm-0001", "itm-0002"]
    assert [(b["bid_id"], b["bundle"], b["amount_cents"]) for b in result["bids"]] == [
        ("A", ["itm-0001", "itm-0002"], 10000),
        ("B", ["itm-0001"], 7000),
        ("C", ["itm-0002"], 6000)]
    assert result["welfare_optimum"] is None
    assert result["winner_set"] is None
    assert result["settlements"] is None
    assert result == inspect_auction(tmp_path / "demo" / "snapshot", FIXTURES / "bids.json")
    with pytest.raises(AuctionEvidenceError, match="unsafe"):
        inspect_auction(tmp_path / "demo" / "offset", FIXTURES / "bids.json")


def test_auction_rejects_unknown_listing(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    with pytest.raises(AuctionEvidenceError, match="resource contract"):
        inspect_auction(tmp_path / "demo" / "snapshot", FIXTURES / "bids-bad-resource.json")
    shared = inspect_auction(tmp_path / "demo" / "snapshot", FIXTURES / "bids-shared-owner.json")
    assert [b["owner_id"] for b in shared["bids"]] == ["ORG-A", "ORG-B", "ORG-B"]
    assert shared["winner_set"] is None
