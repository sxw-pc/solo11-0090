"""Offline handoff of audited collection records to named consumers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .audit.extras import read_jsonl
from .evidence import verify_run
from .ids import canonical_json, sha256_bytes, sha256_file
from .store.sqlite_ledger import Ledger


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConsumerContract:
    consumer_id: str
    categories: tuple[str, ...]
    minimum_records: int


def load_contract(path: str | Path) -> ConsumerContract:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise DeliveryError("contract must be a mapping")
    consumer_id = raw.get("consumer_id")
    categories = raw.get("categories")
    minimum = raw.get("minimum_records")
    if not isinstance(consumer_id, str) or not consumer_id.strip():
        raise DeliveryError("consumer_id must be nonempty")
    if not isinstance(categories, list) or not categories or any(
        not isinstance(category, str) or not category.strip() for category in categories
    ):
        raise DeliveryError("categories must be a nonempty list")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise DeliveryError("minimum_records must be positive")
    return ConsumerContract(consumer_id, tuple(sorted(set(categories))), minimum)


def deliver(
    run_dir: str | Path,
    ledger: Ledger,
    contract: ConsumerContract,
) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    audit_path = root / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    run_id = str(audit["run_id"])
    row = ledger.conn.execute(
        "SELECT r.run_dir, a.audit_sha256 FROM runs r JOIN audit_runs a USING (run_id) "
        "WHERE r.run_id = ?", (run_id,)
    ).fetchone()
    if row is None or Path(row["run_dir"]).resolve() != root:
        raise DeliveryError("run is not recorded in this ledger")
    if row["audit_sha256"] != sha256_file(audit_path):
        raise DeliveryError("audit file differs from recorded evidence")
    verification = verify_run(root, str(ledger.path))
    if verification["status"] != "OK":
        raise DeliveryError("page evidence verification failed")
    records = read_jsonl(root / "records.jsonl")
    selected = sorted(
        (record for record in records if record.get("category") in contract.categories),
        key=lambda record: (str(record["id"]), int(record["sort_key"])),
    )
    distinct = {str(record["id"]) for record in selected}
    safe = audit["summary"]["verdict"] == "safe"
    sufficient = len(distinct) >= contract.minimum_records
    status = "ready" if safe and sufficient else "blocked"
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "consumer_id": contract.consumer_id,
        "categories": list(contract.categories),
        "minimum_records": contract.minimum_records,
        "status": status,
        "reason": "" if status == "ready" else (
            "audit_unsafe" if not safe else "insufficient_records"
        ),
        "audit_sha256": row["audit_sha256"],
        "records": selected if status == "ready" else [],
    }
    payload = canonical_json(receipt)
    digest = sha256_bytes(payload)
    ledger.conn.execute(
        "CREATE TABLE IF NOT EXISTS delivery_receipts ("
        "run_id TEXT NOT NULL, consumer_id TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, "
        "receipt_json TEXT NOT NULL, PRIMARY KEY (run_id, consumer_id))"
    )
    existing = ledger.conn.execute(
        "SELECT receipt_sha256 FROM delivery_receipts WHERE run_id=? AND consumer_id=?",
        (run_id, contract.consumer_id),
    ).fetchone()
    if existing and existing["receipt_sha256"] != digest:
        raise DeliveryError("consumer receipt is immutable for this run")
    if not existing:
        ledger.conn.execute(
            "INSERT INTO delivery_receipts VALUES (?, ?, ?, ?)",
            (run_id, contract.consumer_id, digest, payload.decode("utf-8")),
        )
        ledger.conn.commit()
    return {**receipt, "receipt_sha256": digest}
