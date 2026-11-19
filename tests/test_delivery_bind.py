"""Acceptance tests for consumption-cycle binding (deliver-bind).

Everything here is offline and deterministic: demos and double-safe runs are
synthesized in tmp directories, and no assertion depends on wall-clock time.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pagedrift.delivery import (
    DeliveryError,
    bind_cycle,
    load_contract,
    load_manifest,
)
from pagedrift.config import load_plan
from pagedrift.fixtures import dataset_from_plan
from pagedrift.ids import canonical_json, sha256_bytes
from pagedrift.pipeline import DEFAULT_FROZEN_TIME, DEFAULT_PLAN, collect_single, run_demo
from pagedrift.store.sqlite_ledger import Ledger

from helpers import execute_strategy

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CONTRACT = ROOT / "pagedrift" / "plans" / "consumer_sample.yaml"
# Shipped synthetic fixture: same plan and mutation schedule, 4 initial
# records, 2 per page, one beta record inserted before the second request.
# keyset and snapshot both audit safe, but only the keyset delivery contains
# the inserted record.
DOUBLE_SAFE_PLAN = ROOT / "pagedrift" / "plans" / "binding_double_safe.yaml"
INSERTED_BETA_ID = "itm-0901"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_contract(path: Path, consumer_id: str, categories: list[str], minimum: int) -> Path:
    path.write_text(
        yaml.safe_dump(
            {"consumer_id": consumer_id, "categories": categories, "minimum_records": minimum},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_manifest(
    path: Path,
    *,
    cycle_id: str,
    consumer_id: str,
    run_id: str,
    inputs: dict[str, str] | None = None,
    audit_sha256: str | None = None,
) -> Path:
    doc: dict[str, object] = {
        "schema_version": 1,
        "cycle_id": cycle_id,
        "consumer_id": consumer_id,
        "run_id": run_id,
    }
    if inputs:
        doc["inputs"] = inputs
    if audit_sha256:
        doc["audit_sha256"] = audit_sha256
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _binding_row(ledger: Ledger, consumer_id: str, cycle_id: str, scope: str):
    return ledger.conn.execute(
        "SELECT * FROM cycle_bindings WHERE consumer_id=? AND cycle_id=? AND scope=?",
        (consumer_id, cycle_id, scope),
    ).fetchone()


@pytest.fixture(scope="module")
def bind_demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Independent default demo: snapshot safe, offset/pagenum/keyset unsafe."""
    root = tmp_path_factory.mktemp("bind-demo")
    run_demo(root, plan_path=DEFAULT_PLAN, frozen_time=DEFAULT_FROZEN_TIME)
    return Path(root)


@pytest.fixture()
def demo(tmp_path: Path, bind_demo: Path) -> Path:
    """Function-scoped, fully isolated copy of the default demo bundle."""
    target = tmp_path / "demo"
    shutil.copytree(bind_demo, target)
    # The ledger stores absolute run_dir paths; relocate them into the copy so
    # the bundle (ledger + run directories) is self-contained per test.
    with Ledger(target / "ledger.sqlite") as ledger:
        rows = ledger.conn.execute("SELECT run_id, run_dir FROM runs").fetchall()
        for row in rows:
            try:
                relative = Path(row["run_dir"]).relative_to(bind_demo)
            except ValueError:
                continue
            ledger.conn.execute(
                "UPDATE runs SET run_dir = ? WHERE run_id = ?",
                (str((target / relative).resolve()), row["run_id"]),
            )
        ledger.conn.commit()
    return target


@pytest.fixture()
def double_safe(tmp_path: Path) -> dict:
    """keyset+snapshot both audit safe; keyset content includes the beta insert."""
    plan = load_plan(DOUBLE_SAFE_PLAN)
    records, _info = dataset_from_plan(plan)
    ledger_path = tmp_path / "ledger.sqlite"
    runs: dict[str, dict] = {}
    with Ledger(ledger_path) as ledger:
        for strategy in ("keyset", "snapshot"):
            result = execute_strategy(plan, strategy, records, tmp_path / "runs")
            ledger.record_run(result["run"], result["run_dir"])
            ledger.record_audit_file(result["audit"], result["run_dir"])
            runs[strategy] = result
    contract_path = _write_contract(tmp_path / "contract.yaml", "beta-consumer", ["alpha", "beta"], 2)
    return {
        "ledger": ledger_path,
        "runs": runs,
        "contract": load_contract(contract_path),
        "tmp": tmp_path,
    }


def _run_id(bundle: Path, strategy: str) -> str:
    return json.loads((bundle / strategy / "run.json").read_text(encoding="utf-8"))["run_id"]


# ---------------------------------------------------------------------------
# Default demo: strict/assisted divergence
# ---------------------------------------------------------------------------

def test_default_demo_strict_pending_assisted_infers_snapshot(demo: Path, tmp_path: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    snapshot_run = _run_id(demo, "snapshot")
    ledger_path = demo / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        strict = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d1", scope="strict")
        assert strict["status"] == "pending"
        assert strict["reason"] == "manifest_required"
        assert strict["authorization"] is None
        assert strict["run_id"] is None
        assert strict["receipt_sha256"] is None

        assisted = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d1", scope="assisted")
        assert assisted["status"] == "ready"
        assert assisted["authorization"] == "inferred"
        assert assisted["run_id"] == snapshot_run
        assert assisted["receipt_sha256"]
        assert assisted["audit_sha256"]
        # The inference is labelled, never written as a source-side declaration.
        assert "not an explicit declaration by the collection source" in assisted["authorization_note"]
        # Input fingerprints of the bound run are part of the decision.
        run_doc = json.loads((demo / "snapshot" / "run.json").read_text(encoding="utf-8"))
        assert assisted["inputs"]["universe_sha256"] == run_doc["universe_sha256"]
        assert assisted["inputs"]["plan_sha256"] == run_doc["plan_sha256"]
        # Every demo run is a candidate; only snapshot is eligible.
        assert {c["strategy"] for c in assisted["candidates"]} == {"offset", "pagenum", "keyset", "snapshot"}
        eligible = [c for c in assisted["candidates"] if c["eligible"]]
        assert [c["strategy"] for c in eligible] == ["snapshot"]
        offset = next(c for c in assisted["candidates"] if c["strategy"] == "offset")
        assert offset["verdict"] == "unsafe"
        assert "audit_unsafe" in offset["reasons"]
        # The two scopes are independent rows for the same cycle.
        assert _binding_row(ledger, contract.consumer_id, "cycle-d1", "strict")["status"] == "pending"
        assert _binding_row(ledger, contract.consumer_id, "cycle-d1", "assisted")["status"] == "ready"


# ---------------------------------------------------------------------------
# Double safe with different content
# ---------------------------------------------------------------------------

def test_double_safe_assisted_must_conflict(double_safe: dict) -> None:
    with Ledger(double_safe["ledger"]) as ledger:
        decision = bind_cycle(
            ledger=ledger, contract=double_safe["contract"], cycle_id="cycle-x1", scope="assisted"
        )
    assert decision["status"] == "conflict"
    assert decision["reason"] == "ambiguous_candidates"
    assert decision["authorization"] is None
    assert decision["receipt_sha256"] is None
    eligible = {c["strategy"]: c for c in decision["candidates"] if c["eligible"]}
    assert set(eligible) == {"keyset", "snapshot"}
    # Both candidates are safe and satisfy the contract, but their record sets differ.
    assert INSERTED_BETA_ID in eligible["keyset"]["selected_ids"]
    assert INSERTED_BETA_ID not in eligible["snapshot"]["selected_ids"]
    # The conflict preserves provenance and the record difference per candidate.
    assert eligible["keyset"]["distinctive_selected_ids"] == [INSERTED_BETA_ID]
    assert eligible["snapshot"]["distinctive_selected_ids"] == []
    assert eligible["keyset"]["records_sha256"] != eligible["snapshot"]["records_sha256"]
    assert eligible["keyset"]["run_dir"] != eligible["snapshot"]["run_dir"]
    # No receipt was written for a conflicted cycle.
    with Ledger(double_safe["ledger"]) as ledger:
        count = ledger.conn.execute("SELECT COUNT(*) AS n FROM delivery_receipts").fetchone()["n"]
    assert count == 0


def test_double_safe_strict_manifest_binds_snapshot_only(double_safe: dict, tmp_path: Path) -> None:
    snapshot_run = double_safe["runs"]["snapshot"]["run"]["run_id"]
    keyset_run = double_safe["runs"]["keyset"]["run"]["run_id"]
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-x2",
        consumer_id="beta-consumer",
        run_id=snapshot_run,
    )
    manifest = load_manifest(manifest_path)
    with Ledger(double_safe["ledger"]) as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=double_safe["contract"],
            cycle_id="cycle-x2",
            scope="strict",
            manifest=manifest,
            manifest_sha256=sha256_bytes(manifest_path.read_bytes()),
        )
        assert decision["status"] == "ready"
        assert decision["authorization"] == "explicit"
        assert decision["run_id"] == snapshot_run
        assert decision["run_id"] != keyset_run
        # The explicit delivery contains no inserted record.
        receipt = ledger.conn.execute(
            "SELECT receipt_json FROM delivery_receipts WHERE run_id=? AND consumer_id=?",
            (snapshot_run, "beta-consumer"),
        ).fetchone()
        delivered = json.loads(receipt["receipt_json"])
        assert delivered["status"] == "ready"
        assert INSERTED_BETA_ID not in {row["id"] for row in delivered["records"]}
        # keyset was eligible but not substituted for the manifest-bound run.
        keyset_candidate = next(c for c in decision["candidates"] if c["strategy"] == "keyset")
        assert keyset_candidate["eligible"] is True


# ---------------------------------------------------------------------------
# Manifest / evidence conflicts: never substitute another safe run
# ---------------------------------------------------------------------------

def test_manifest_input_fingerprint_mismatch_blocks(double_safe: dict, tmp_path: Path) -> None:
    snapshot_run = double_safe["runs"]["snapshot"]["run"]["run_id"]
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-x3",
        consumer_id="beta-consumer",
        run_id=snapshot_run,
        inputs={"universe_sha256": "0" * 64},
    )
    with Ledger(double_safe["ledger"]) as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=double_safe["contract"],
            cycle_id="cycle-x3",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
        assert decision["status"] == "blocked"
        assert decision["reason"] == "input_fingerprint_mismatch"
        assert decision["run_id"] == snapshot_run
        mismatch = decision["manifest_check"]["pin_mismatches"][0]
        assert mismatch["field"] == "universe_sha256"
        assert mismatch["expected"] == "0" * 64
        # Another safe run exists but was not substituted.
        assert any(c["strategy"] == "keyset" and c["eligible"] for c in decision["candidates"])
        assert _binding_row(ledger, "beta-consumer", "cycle-x3", "strict")["status"] == "blocked"
        count = ledger.conn.execute("SELECT COUNT(*) AS n FROM delivery_receipts").fetchone()["n"]
        assert count == 0


def test_manifest_audit_sha_mismatch_blocks(double_safe: dict, tmp_path: Path) -> None:
    snapshot_run = double_safe["runs"]["snapshot"]["run"]["run_id"]
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-x4",
        consumer_id="beta-consumer",
        run_id=snapshot_run,
        audit_sha256="f" * 64,
    )
    with Ledger(double_safe["ledger"]) as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=double_safe["contract"],
            cycle_id="cycle-x4",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
    assert decision["status"] == "blocked"
    assert decision["reason"] == "audit_sha_mismatch"
    assert decision["manifest_check"]["pin_mismatches"][0]["field"] == "audit_sha256"


def test_manifest_pointing_to_unsafe_run_blocks(demo: Path, tmp_path: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    offset_run = _run_id(demo, "offset")
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-d2",
        consumer_id=contract.consumer_id,
        run_id=offset_run,
    )
    with Ledger(demo / "ledger.sqlite") as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=contract,
            cycle_id="cycle-d2",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
        assert decision["status"] == "blocked"
        assert decision["reason"] == "audit_unsafe"
        # snapshot is safe and eligible, yet the cycle stays blocked.
        snapshot = next(c for c in decision["candidates"] if c["strategy"] == "snapshot")
        assert snapshot["eligible"] is True
        count = ledger.conn.execute("SELECT COUNT(*) AS n FROM delivery_receipts").fetchone()["n"]
        assert count == 0


def test_manifest_contract_unsatisfied_blocks(demo: Path, tmp_path: Path) -> None:
    contract_path = _write_contract(tmp_path / "contract.yaml", "sample-consumer", ["alpha", "beta"], 999)
    contract = load_contract(contract_path)
    snapshot_run = _run_id(demo, "snapshot")
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-d3",
        consumer_id=contract.consumer_id,
        run_id=snapshot_run,
    )
    with Ledger(demo / "ledger.sqlite") as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=contract,
            cycle_id="cycle-d3",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
    assert decision["status"] == "blocked"
    assert decision["reason"] == "insufficient_records"
    target = next(c for c in decision["candidates"] if c["run_id"] == snapshot_run)
    assert target["safe"] is True
    assert target["contract_satisfied"] is False


def test_manifest_page_evidence_mismatch_blocks(demo: Path, tmp_path: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    snapshot_run = _run_id(demo, "snapshot")
    requests = [
        json.loads(line)
        for line in (demo / "snapshot" / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    page = demo / "snapshot" / requests[0]["evidence_file"]
    page.write_bytes(page.read_bytes() + b"tamper")
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-d4",
        consumer_id=contract.consumer_id,
        run_id=snapshot_run,
    )
    with Ledger(demo / "ledger.sqlite") as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=contract,
            cycle_id="cycle-d4",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
        assert decision["status"] == "blocked"
        assert decision["reason"] == "page_evidence_mismatch"
        # The untampered unsafe runs were not silently promoted either.
        assert decision["receipt_sha256"] is None
        count = ledger.conn.execute("SELECT COUNT(*) AS n FROM delivery_receipts").fetchone()["n"]
        assert count == 0


def test_manifest_unknown_run_blocks(double_safe: dict, tmp_path: Path) -> None:
    manifest_path = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-x5",
        consumer_id="beta-consumer",
        run_id="run-does-not-exist",
    )
    with Ledger(double_safe["ledger"]) as ledger:
        decision = bind_cycle(
            ledger=ledger,
            contract=double_safe["contract"],
            cycle_id="cycle-x5",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
    assert decision["status"] == "blocked"
    assert decision["reason"] == "run_not_in_ledger"


# ---------------------------------------------------------------------------
# Immutability, repeat stability, ledger/JSON consistency
# ---------------------------------------------------------------------------

def test_new_run_after_binding_does_not_change_delivered_cycle(demo: Path, tmp_path: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    ledger_path = demo / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        first = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d5", scope="assisted")
    assert first["status"] == "ready"
    # A new, independently safe run appears in the same ledger afterwards.
    collect_single(
        out=tmp_path / "extra-snapshot",
        plan_path=DEFAULT_PLAN,
        strategy="snapshot",
        seed_override=7,
        ledger_path=ledger_path,
    )
    with Ledger(ledger_path) as ledger:
        runs = ledger.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        assert runs == 5
        second = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d5", scope="assisted")
        # The delivered cycle is frozen: identical decision, same run, same receipt.
        assert second == first
        assert second["run_id"] == first["run_id"]
        rows = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM cycle_bindings WHERE consumer_id=? AND cycle_id=? AND scope='assisted'",
            (contract.consumer_id, "cycle-d5"),
        ).fetchone()["n"]
        assert rows == 1


def test_ready_binding_ignores_later_manifest(double_safe: dict, tmp_path: Path) -> None:
    snapshot_run = double_safe["runs"]["snapshot"]["run"]["run_id"]
    keyset_run = double_safe["runs"]["keyset"]["run"]["run_id"]
    first_manifest = _write_manifest(
        tmp_path / "m1.yaml", cycle_id="cycle-x6", consumer_id="beta-consumer", run_id=snapshot_run
    )
    with Ledger(double_safe["ledger"]) as ledger:
        first = bind_cycle(
            ledger=ledger,
            contract=double_safe["contract"],
            cycle_id="cycle-x6",
            scope="strict",
            manifest=load_manifest(first_manifest),
        )
        assert first["status"] == "ready" and first["run_id"] == snapshot_run
        second_manifest = _write_manifest(
            tmp_path / "m2.yaml", cycle_id="cycle-x6", consumer_id="beta-consumer", run_id=keyset_run
        )
        second = bind_cycle(
            ledger=ledger,
            contract=double_safe["contract"],
            cycle_id="cycle-x6",
            scope="strict",
            manifest=load_manifest(second_manifest),
        )
        # The already delivered cycle keeps its original explicit binding.
        assert second == first
        assert second["run_id"] == snapshot_run


def test_repeat_execution_is_stable(demo: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    ledger_path = demo / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        first = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d6", scope="assisted")
        second = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d6", scope="assisted")
        assert first == second
        assert first["receipt_sha256"] == second["receipt_sha256"]
        bindings = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM cycle_bindings WHERE consumer_id=? AND cycle_id=?",
            (contract.consumer_id, "cycle-d6"),
        ).fetchone()["n"]
        receipts = ledger.conn.execute("SELECT COUNT(*) AS n FROM delivery_receipts").fetchone()["n"]
        assert bindings == 1
        assert receipts == 1


def test_pending_cycle_can_be_bound_later_by_manifest(demo: Path, tmp_path: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    snapshot_run = _run_id(demo, "snapshot")
    ledger_path = demo / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        pending = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d7", scope="strict")
        assert pending["status"] == "pending"
        manifest_path = _write_manifest(
            tmp_path / "manifest.yaml",
            cycle_id="cycle-d7",
            consumer_id=contract.consumer_id,
            run_id=snapshot_run,
        )
        bound = bind_cycle(
            ledger=ledger,
            contract=contract,
            cycle_id="cycle-d7",
            scope="strict",
            manifest=load_manifest(manifest_path),
        )
        assert bound["status"] == "ready"
        assert bound["authorization"] == "explicit"
        rows = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM cycle_bindings WHERE consumer_id=? AND cycle_id=? AND scope='strict'",
            (contract.consumer_id, "cycle-d7"),
        ).fetchone()["n"]
        assert rows == 1
        assert _binding_row(ledger, contract.consumer_id, "cycle-d7", "strict")["status"] == "ready"


def test_ledger_and_json_decision_are_consistent(demo: Path, tmp_path: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    ledger_path = demo / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        decision = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d8", scope="assisted")
        row = _binding_row(ledger, contract.consumer_id, "cycle-d8", "assisted")
        assert row is not None
        # The stored JSON document is exactly the returned decision.
        assert json.loads(row["decision_json"]) == decision
        assert row["decision_sha256"] == sha256_bytes(canonical_json(decision))
        # Atomic columns mirror the decision fields.
        assert row["consumer_id"] == contract.consumer_id
        assert row["cycle_id"] == "cycle-d8"
        assert row["scope"] == "assisted"
        assert row["status"] == decision["status"] == "ready"
        assert row["authorization"] == decision["authorization"] == "inferred"
        assert row["run_id"] == decision["run_id"]
        assert row["contract_sha256"] == decision["contract_sha256"]
        assert row["audit_sha256"] == decision["audit_sha256"]
        assert row["receipt_sha256"] == decision["receipt_sha256"]
        receipt = ledger.conn.execute(
            "SELECT receipt_sha256 FROM delivery_receipts WHERE run_id=? AND consumer_id=?",
            (decision["run_id"], contract.consumer_id),
        ).fetchone()
        assert receipt["receipt_sha256"] == decision["receipt_sha256"]
    # The --out style JSON document matches the ledger content as well.
    out_doc = json.loads(json.dumps(decision, sort_keys=True))
    assert out_doc == decision


def test_binding_does_not_modify_collection_directories(demo: Path) -> None:
    contract = load_contract(SAMPLE_CONTRACT)
    before = {strategy: _tree_digest(demo / strategy) for strategy in ("offset", "pagenum", "keyset", "snapshot")}
    with Ledger(demo / "ledger.sqlite") as ledger:
        bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d9", scope="assisted")
        bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-d9", scope="strict")
    after = {strategy: _tree_digest(demo / strategy) for strategy in ("offset", "pagenum", "keyset", "snapshot")}
    assert before == after


def test_candidate_set_changes_attributable_outcome(tmp_path: Path) -> None:
    """Different candidate run sets change what the cycle can be attributed to."""
    plan = load_plan(DOUBLE_SAFE_PLAN)
    records, _info = dataset_from_plan(plan)
    contract_path = _write_contract(tmp_path / "contract.yaml", "beta-consumer", ["alpha", "beta"], 2)
    contract = load_contract(contract_path)
    ledger_path = tmp_path / "ledger.sqlite"
    snapshot_only = execute_strategy(plan, "snapshot", records, tmp_path / "set-a")
    with Ledger(ledger_path) as ledger:
        # Only the snapshot run exists: the cycle is attributable (inferred).
        ledger.record_run(snapshot_only["run"], snapshot_only["run_dir"])
        ledger.record_audit_file(snapshot_only["audit"], snapshot_only["run_dir"])
        one = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-s1", scope="assisted")
        assert one["status"] == "ready"
        assert one["run_id"] == snapshot_only["run"]["run_id"]
        # A second safe run with different content arrives: a fresh cycle now conflicts.
        keyset = execute_strategy(plan, "keyset", records, tmp_path / "set-b")
        ledger.record_run(keyset["run"], keyset["run_dir"])
        ledger.record_audit_file(keyset["audit"], keyset["run_dir"])
        two = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-s2", scope="assisted")
        assert two["status"] == "conflict"
        # The cycle delivered before the new run arrived is untouched.
        again = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-s1", scope="assisted")
        assert again == one


def test_double_safe_fixture_demo_assisted_conflict(tmp_path: Path) -> None:
    """run_demo's multi-strategy runs flow into the binding flow: all four runs
    are safe on the fixture plan, but the snapshot content differs, so the
    cycle cannot be attributed without an explicit manifest."""
    demo_dir = tmp_path / "double-safe-demo"
    summary = run_demo(demo_dir, plan_path=DOUBLE_SAFE_PLAN, frozen_time=DEFAULT_FROZEN_TIME)
    assert summary["overall_verdict"] == "safe"
    contract_path = _write_contract(tmp_path / "contract.yaml", "beta-consumer", ["alpha", "beta"], 2)
    contract = load_contract(contract_path)
    with Ledger(demo_dir / "ledger.sqlite") as ledger:
        decision = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-f1", scope="assisted")
        assert decision["status"] == "conflict"
        assert decision["reason"] == "ambiguous_candidates"
        eligible = [c for c in decision["candidates"] if c["eligible"]]
        assert len(eligible) == 4
        with_insert = [c for c in eligible if INSERTED_BETA_ID in c["selected_ids"]]
        without_insert = [c for c in eligible if INSERTED_BETA_ID not in c["selected_ids"]]
        assert {c["strategy"] for c in without_insert} == {"snapshot"}
        assert len(with_insert) == 3
        # Input fingerprints prove all candidates share one collection identity.
        identities = {tuple(sorted(c["inputs"].items())) for c in eligible}
        assert len(identities) == 1
        # strict stays pending without a manifest even though runs are safe.
        strict = bind_cycle(ledger=ledger, contract=contract, cycle_id="cycle-f1", scope="strict")
        assert strict["status"] == "pending"


def test_manifest_validation_errors(double_safe: dict, tmp_path: Path) -> None:
    snapshot_run = double_safe["runs"]["snapshot"]["run"]["run_id"]
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"cycle_id": "c", "consumer_id": "beta-consumer", "run_id": snapshot_run, "bogus": 1}))
    with pytest.raises(DeliveryError, match="unknown keys"):
        load_manifest(bad)
    other_cycle = _write_manifest(
        tmp_path / "other.yaml", cycle_id="cycle-other", consumer_id="beta-consumer", run_id=snapshot_run
    )
    with Ledger(double_safe["ledger"]) as ledger:
        with pytest.raises(DeliveryError, match="does not match"):
            bind_cycle(
                ledger=ledger,
                contract=double_safe["contract"],
                cycle_id="cycle-x7",
                scope="strict",
                manifest=load_manifest(other_cycle),
            )
        with pytest.raises(DeliveryError, match="assisted scope does not accept"):
            bind_cycle(
                ledger=ledger,
                contract=double_safe["contract"],
                cycle_id="cycle-x8",
                scope="assisted",
                manifest=load_manifest(other_cycle),
            )


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

def _cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pagedrift", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_deliver_bind_cli_end_to_end(tmp_path: Path) -> None:
    demo_dir = tmp_path / "demo"
    assert _cli("demo", "--out", str(demo_dir)).returncode == 0
    contract = tmp_path / "contract.yaml"
    shutil.copyfile(SAMPLE_CONTRACT, contract)
    ledger = demo_dir / "ledger.sqlite"
    snapshot_run = _run_id(demo_dir, "snapshot")

    assisted_out = tmp_path / "assisted.json"
    assisted = _cli(
        "deliver-bind", "--ledger", str(ledger), "--contract", str(contract),
        "--cycle", "cycle-cli-1", "--scope", "assisted", "--out", str(assisted_out),
    )
    assert assisted.returncode == 0, assisted.stderr
    assert "status=ready" in assisted.stdout
    assert "authorization=inferred" in assisted.stdout
    decision = json.loads(assisted_out.read_text(encoding="utf-8"))
    assert decision["status"] == "ready"
    assert decision["run_id"] == snapshot_run

    strict_out = tmp_path / "strict.json"
    strict = _cli(
        "deliver-bind", "--ledger", str(ledger), "--contract", str(contract),
        "--cycle", "cycle-cli-1", "--scope", "strict", "--out", str(strict_out),
    )
    assert strict.returncode == 1
    assert "status=pending" in strict.stdout
    assert "reason=manifest_required" in strict.stdout
    strict_decision = json.loads(strict_out.read_text(encoding="utf-8"))
    assert strict_decision["status"] == "pending"

    manifest = _write_manifest(
        tmp_path / "manifest.yaml",
        cycle_id="cycle-cli-1",
        consumer_id="sample-consumer",
        run_id=snapshot_run,
    )
    strict_bound_out = tmp_path / "strict-bound.json"
    bound = _cli(
        "deliver-bind", "--ledger", str(ledger), "--contract", str(contract),
        "--cycle", "cycle-cli-1", "--scope", "strict", "--manifest", str(manifest),
        "--out", str(strict_bound_out),
    )
    assert bound.returncode == 0, bound.stderr
    assert "authorization=explicit" in bound.stdout
    bound_decision = json.loads(strict_bound_out.read_text(encoding="utf-8"))
    assert bound_decision["run_id"] == snapshot_run

    # Repeat CLI execution produces byte-identical output files.
    assisted_out2 = tmp_path / "assisted2.json"
    again = _cli(
        "deliver-bind", "--ledger", str(ledger), "--contract", str(contract),
        "--cycle", "cycle-cli-1", "--scope", "assisted", "--out", str(assisted_out2),
    )
    assert again.returncode == 0
    assert assisted_out2.read_bytes() == assisted_out.read_bytes()

    # A manifest that disagrees with the requested cycle is a hard error.
    wrong = _write_manifest(
        tmp_path / "wrong.yaml", cycle_id="cycle-other", consumer_id="sample-consumer", run_id=snapshot_run
    )
    failed = _cli(
        "deliver-bind", "--ledger", str(ledger), "--contract", str(contract),
        "--cycle", "cycle-cli-2", "--scope", "strict", "--manifest", str(wrong),
        "--out", str(tmp_path / "never.json"),
    )
    assert failed.returncode == 2
    assert "does not match" in failed.stderr
