from pathlib import Path
from pagedrift.catalog_intake import intake_catalog
from pagedrift.pipeline import run_demo
from pagedrift.warehouse_promise import inspect_warehouse_promise

FIXTURES = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "warehouse"


def test_warehouse_promise_uses_safe_catalog_and_bounded_routes(tmp_path):
    run_demo(tmp_path / "demo")
    catalog = intake_catalog(tmp_path / "demo" / "snapshot", FIXTURES / "offers.json")
    result = inspect_warehouse_promise(catalog, FIXTURES / "promise-valid.json")
    assert result["sku"] == "SYN-ALPHA"
    assert {(a["order_id"],a["batch_id"],a["route"]) for a in result["eligible_assignments"]} == {
        ("O1","BX","local"),("O2","BX","local"),("O2","BY","east->west"),
        ("O3","BY","local"),("O3","BZ","local")}
    assert {(x["claim_id"], x["order_id"], x["read_epoch"]) for x in result["claim_attempts"]} == {("C2","O2",1),("C3","O3",1)}
    assert result["promise_decisions"] is None
    assert result["reservation_receipts"] is None



def test_expiry_removes_cross_warehouse_promise_edge(tmp_path):
    run_demo(tmp_path / "demo")
    catalog = intake_catalog(tmp_path / "demo" / "snapshot", FIXTURES / "offers.json")
    result = inspect_warehouse_promise(catalog, FIXTURES / "promise-expired.json")
    assert ("O2", "BY", "east->west") not in {(a["order_id"],a["batch_id"],a["route"]) for a in result["eligible_assignments"]}
    assert result["promise_decisions"] is None
