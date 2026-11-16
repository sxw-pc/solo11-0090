from pathlib import Path

import pytest

from pagedrift.delivery import ConsumerContract, DeliveryError, deliver, load_contract
from pagedrift.store.sqlite_ledger import Ledger


def test_audited_consumer_handoff_is_immutable(demo_bundle):
    root = demo_bundle["root"]
    contract = load_contract(Path(__file__).parents[1] / "pagedrift/plans/consumer_sample.yaml")
    with Ledger(root / "ledger.sqlite") as ledger:
        ready = deliver(root / "snapshot", ledger, contract)
        assert ready["status"] == "ready"
        assert ready["records"]
        assert {row["category"] for row in ready["records"]} <= set(contract.categories)
        assert deliver(root / "snapshot", ledger, contract)["receipt_sha256"] == ready["receipt_sha256"]

        blocked = deliver(root / "offset", ledger, contract)
        assert blocked["status"] == "blocked"
        assert blocked["reason"] == "audit_unsafe"
        assert blocked["records"] == []

        revised = ConsumerContract(contract.consumer_id, ("gamma",), 1)
        with pytest.raises(DeliveryError, match="immutable"):
            deliver(root / "snapshot", ledger, revised)
