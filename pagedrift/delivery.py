"""Offline handoff of audited collection records to named consumers.

Two layers live here:

- ``deliver``: run-scoped immutable receipts (the original behaviour).
- ``bind_cycle``: consumption-cycle attribution. A cycle is bound to one
  specific audited run either by an explicit local binding manifest
  (``strict`` scope, authorization ``explicit``) or by the unique-eligible-
  candidate rule (``assisted`` scope, authorization ``inferred``). An inferred
  attribution is never written as an explicit declaration of the collection
  source. Conclusions (ready/pending/conflict/blocked) are persisted in the
  SQLite ledger and as JSON; a delivered cycle is immutable and later runs
  never silently change it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
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


def contract_digest(contract: ConsumerContract) -> str:
    """Stable digest of the contract terms that shape a delivery."""
    return sha256_bytes(
        canonical_json(
            {
                "consumer_id": contract.consumer_id,
                "categories": list(contract.categories),
                "minimum_records": contract.minimum_records,
            }
        )
    )


def _select_records(records: list[dict[str, Any]], contract: ConsumerContract) -> list[dict[str, Any]]:
    return sorted(
        (record for record in records if record.get("category") in contract.categories),
        key=lambda record: (str(record["id"]), int(record["sort_key"])),
    )


RECEIPT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS delivery_receipts ("
    "run_id TEXT NOT NULL, consumer_id TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, "
    "receipt_json TEXT NOT NULL, PRIMARY KEY (run_id, consumer_id))"
)


def _receipt_tx(root: Path, ledger: Ledger, contract: ConsumerContract) -> dict[str, Any]:
    """Build and persist the immutable receipt inside the caller's transaction."""
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
    selected = _select_records(records, contract)
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
    ledger.conn.execute(RECEIPT_SCHEMA)
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
    return {**receipt, "receipt_sha256": digest}


def deliver(
    run_dir: str | Path,
    ledger: Ledger,
    contract: ConsumerContract,
) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    result = _receipt_tx(root, ledger, contract)
    ledger.conn.commit()
    return result


# ---------------------------------------------------------------------------
# Consumption-cycle binding
# ---------------------------------------------------------------------------

BINDING_SCOPES = ("strict", "assisted")
AUTHORIZATION_EXPLICIT = "explicit"
AUTHORIZATION_INFERRED = "inferred"
EXPLICIT_NOTE = "bound from an explicit local binding manifest (operator-declared)"
INFERRED_NOTE = (
    "inferred by the unique eligible candidate rule under the shared input identity; "
    "not an explicit declaration by the collection source"
)
INPUT_PIN_FIELDS = ("plan_sha256", "universe_sha256", "schedule_sha256")
MANIFEST_KEYS = {"schema_version", "cycle_id", "consumer_id", "run_id", "inputs", "audit_sha256"}

BINDING_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cycle_bindings ("
    "consumer_id TEXT NOT NULL, cycle_id TEXT NOT NULL, scope TEXT NOT NULL, "
    "status TEXT NOT NULL, authorization TEXT, run_id TEXT, reason TEXT NOT NULL, "
    "contract_sha256 TEXT NOT NULL, audit_sha256 TEXT, receipt_sha256 TEXT, "
    "decision_sha256 TEXT NOT NULL, decision_json TEXT NOT NULL, "
    "PRIMARY KEY (consumer_id, cycle_id, scope))"
)


@dataclass(frozen=True)
class BindingManifest:
    """Explicit local declaration binding one cycle/consumer to one run."""

    cycle_id: str
    consumer_id: str
    run_id: str
    inputs: dict[str, str] = field(default_factory=dict)
    audit_sha256: str | None = None


def load_manifest(path: str | Path) -> BindingManifest:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise DeliveryError("binding manifest must be a mapping")
    unknown = sorted(set(raw) - MANIFEST_KEYS)
    if unknown:
        raise DeliveryError(f"binding manifest has unknown keys: {', '.join(unknown)}")
    values: dict[str, str] = {}
    for key in ("cycle_id", "consumer_id", "run_id"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DeliveryError(f"binding manifest requires a nonempty {key}")
        values[key] = value.strip()
    inputs_raw = raw.get("inputs", {})
    if not isinstance(inputs_raw, dict):
        raise DeliveryError("binding manifest inputs must be a mapping")
    inputs: dict[str, str] = {}
    for key, value in inputs_raw.items():
        if key not in INPUT_PIN_FIELDS:
            raise DeliveryError(f"binding manifest inputs has unknown pin {key!r}")
        if not isinstance(value, str) or not value.strip():
            raise DeliveryError(f"binding manifest input pin {key} must be a nonempty string")
        inputs[key] = value.strip()
    audit_sha256 = raw.get("audit_sha256")
    if audit_sha256 is not None and (not isinstance(audit_sha256, str) or not audit_sha256.strip()):
        raise DeliveryError("binding manifest audit_sha256 must be a nonempty string")
    return BindingManifest(
        cycle_id=values["cycle_id"],
        consumer_id=values["consumer_id"],
        run_id=values["run_id"],
        inputs=inputs,
        audit_sha256=audit_sha256.strip() if isinstance(audit_sha256, str) else None,
    )


def _candidate_from_row(ledger: Ledger, row: Any, contract: ConsumerContract) -> dict[str, Any]:
    """Evaluate one ledger-recorded run as a delivery candidate.

    Eligibility is driven by the audit verdict computed by ``audit_run`` and by
    the page-evidence verification; both gate the binding conclusion.
    """
    run_dir = Path(str(row["run_dir"])).resolve()
    candidate: dict[str, Any] = {
        "run_id": str(row["run_id"]),
        "strategy": str(row["strategy"]),
        "run_dir": str(run_dir),
        "inputs": {
            "plan_sha256": str(row["plan_sha256"]),
            "universe_sha256": str(row["universe_sha256"]),
            "schedule_sha256": str(row["schedule_sha256"]),
        },
        "audit_sha256": None,
        "verdict": None,
        "evidence_ok": False,
        "safe": False,
        "contract_satisfied": False,
        "selected_count": 0,
        "selected_ids": [],
        "records_sha256": "",
        "distinctive_selected_ids": [],
        "eligible": False,
        "reasons": [],
    }
    reasons: list[str] = candidate["reasons"]
    audit: dict[str, Any] | None = None
    audit_path = run_dir / "audit.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        candidate["audit_sha256"] = sha256_file(audit_path)
    except (OSError, json.JSONDecodeError):
        audit = None
    if audit is None or candidate["audit_sha256"] != row["audit_sha256"]:
        reasons.append("audit_sha_mismatch")
    verification = verify_run(run_dir, str(ledger.path))
    if verification["status"] != "OK":
        reasons.append("page_evidence_mismatch")
    candidate["evidence_ok"] = not reasons
    if audit is not None:
        verdict = str((audit.get("summary") or {}).get("verdict", ""))
        candidate["verdict"] = verdict
        candidate["safe"] = verdict == "safe"
        if not candidate["safe"]:
            reasons.append("audit_unsafe")
    records = read_jsonl(run_dir / "records.jsonl")
    selected = _select_records(records, contract)
    selected_ids = sorted({str(record["id"]) for record in selected})
    candidate["selected_count"] = len(selected_ids)
    candidate["selected_ids"] = selected_ids
    candidate["records_sha256"] = sha256_bytes(canonical_json(selected))
    candidate["contract_satisfied"] = len(selected_ids) >= contract.minimum_records
    if not candidate["contract_satisfied"]:
        reasons.append("insufficient_records")
    candidate["eligible"] = not reasons
    return candidate


def evaluate_candidates(ledger: Ledger, contract: ConsumerContract) -> list[dict[str, Any]]:
    """All ledger-recorded runs evaluated against evidence, audit and contract."""
    rows = ledger.conn.execute(
        "SELECT r.run_id, r.strategy, r.run_dir, r.plan_sha256, r.universe_sha256, "
        "r.schedule_sha256, a.audit_sha256 FROM runs r "
        "LEFT JOIN audit_runs a USING (run_id) ORDER BY r.strategy, r.run_id"
    ).fetchall()
    return [_candidate_from_row(ledger, row, contract) for row in rows]


def _mark_distinctive_ids(candidates: list[dict[str, Any]], eligible: list[dict[str, Any]]) -> None:
    """Preserve per-candidate record differences for a conflict decision."""
    id_sets = {candidate["run_id"]: set(candidate["selected_ids"]) for candidate in eligible}
    for candidate in eligible:
        others: set[str] = set()
        for run_id, ids in id_sets.items():
            if run_id != candidate["run_id"]:
                others |= ids
        candidate["distinctive_selected_ids"] = sorted(id_sets[candidate["run_id"]] - others)
    for candidate in candidates:
        candidate.setdefault("distinctive_selected_ids", [])


def _finish_ready(
    decision: dict[str, Any],
    target: dict[str, Any],
    ledger: Ledger,
    contract: ConsumerContract,
    authorization: str,
    note: str,
) -> None:
    receipt = _receipt_tx(Path(target["run_dir"]), ledger, contract)
    decision.update(
        {
            "status": "ready",
            "authorization": authorization,
            "authorization_note": note,
            "reason": "",
            "run_id": target["run_id"],
            "inputs": target["inputs"],
            "audit_sha256": target["audit_sha256"],
            "receipt_sha256": receipt["receipt_sha256"],
        }
    )


def _apply_strict(
    decision: dict[str, Any],
    manifest: BindingManifest,
    candidates: list[dict[str, Any]],
    ledger: Ledger,
    contract: ConsumerContract,
) -> None:
    pins: dict[str, str] = dict(sorted(manifest.inputs.items()))
    if manifest.audit_sha256:
        pins["audit_sha256"] = manifest.audit_sha256
    decision["manifest_check"] = {"run_id": manifest.run_id, "pins": pins, "pin_mismatches": []}
    target = next((c for c in candidates if c["run_id"] == manifest.run_id), None)
    if target is None:
        decision.update({"status": "blocked", "reason": "run_not_in_ledger"})
        return
    decision["run_id"] = target["run_id"]
    decision["audit_sha256"] = target["audit_sha256"]
    mismatches = []
    for field_name, expected in sorted(manifest.inputs.items()):
        actual = target["inputs"].get(field_name)
        if actual != expected:
            mismatches.append({"field": field_name, "expected": expected, "actual": actual})
    if manifest.audit_sha256 is not None and manifest.audit_sha256 != target["audit_sha256"]:
        mismatches.append(
            {"field": "audit_sha256", "expected": manifest.audit_sha256, "actual": target["audit_sha256"]}
        )
    decision["manifest_check"]["pin_mismatches"] = mismatches
    if mismatches:
        code = (
            "input_fingerprint_mismatch"
            if any(item["field"] != "audit_sha256" for item in mismatches)
            else "audit_sha_mismatch"
        )
        # Never substitute another safe run for the manifest-bound run.
        decision.update({"status": "blocked", "reason": code})
        return
    if not target["eligible"]:
        decision.update({"status": "blocked", "reason": target["reasons"][0]})
        return
    _finish_ready(decision, target, ledger, contract, AUTHORIZATION_EXPLICIT, EXPLICIT_NOTE)


def _apply_assisted(
    decision: dict[str, Any],
    candidates: list[dict[str, Any]],
    ledger: Ledger,
    contract: ConsumerContract,
) -> None:
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    if not eligible:
        decision.update({"status": "pending", "reason": "no_eligible_candidate"})
        return
    if len(eligible) > 1:
        # Never pick by strategy name, recency or record count.
        _mark_distinctive_ids(candidates, eligible)
        decision.update({"status": "conflict", "reason": "ambiguous_candidates"})
        return
    _finish_ready(decision, eligible[0], ledger, contract, AUTHORIZATION_INFERRED, INFERRED_NOTE)


def bind_cycle(
    *,
    ledger: Ledger,
    contract: ConsumerContract,
    cycle_id: str,
    scope: str,
    manifest: BindingManifest | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Attribute one consumption cycle to one audited run and persist the decision.

    The decision (ready/pending/conflict/blocked plus candidate evidence) is
    written to the ``cycle_bindings`` table and returned for the JSON output.
    A ``ready`` decision is frozen: once a cycle is delivered, re-evaluation
    returns the stored decision and later runs cannot silently change it.
    """
    cycle = str(cycle_id).strip()
    if not cycle:
        raise DeliveryError("cycle_id must be nonempty")
    if scope not in BINDING_SCOPES:
        raise DeliveryError(f"scope must be one of {BINDING_SCOPES}")
    if scope == "assisted" and manifest is not None:
        raise DeliveryError("assisted scope does not accept a binding manifest")
    if manifest is not None:
        if manifest.cycle_id != cycle:
            raise DeliveryError("manifest cycle_id does not match the requested cycle")
        if manifest.consumer_id != contract.consumer_id:
            raise DeliveryError("manifest consumer_id does not match the contract")
    ledger.conn.execute(BINDING_SCHEMA)
    ledger.conn.execute(RECEIPT_SCHEMA)
    existing = ledger.conn.execute(
        "SELECT status, decision_sha256, decision_json FROM cycle_bindings "
        "WHERE consumer_id=? AND cycle_id=? AND scope=?",
        (contract.consumer_id, cycle, scope),
    ).fetchone()
    if existing is not None and existing["status"] == "ready":
        # Delivered cycles are immutable; new runs never rewrite them.
        return json.loads(existing["decision_json"])
    candidates = evaluate_candidates(ledger, contract)
    decision: dict[str, Any] = {
        "schema_version": 1,
        "kind": "cycle_binding_decision",
        "consumer_id": contract.consumer_id,
        "cycle_id": cycle,
        "scope": scope,
        "status": "",
        "authorization": None,
        "authorization_note": "",
        "reason": "",
        "run_id": None,
        "inputs": None,
        "contract_sha256": contract_digest(contract),
        "contract": {
            "consumer_id": contract.consumer_id,
            "categories": list(contract.categories),
            "minimum_records": contract.minimum_records,
        },
        "manifest_sha256": manifest_sha256,
        "audit_sha256": None,
        "receipt_sha256": None,
        "candidates": candidates,
    }
    if scope == "strict":
        if manifest is None:
            decision.update({"status": "pending", "reason": "manifest_required"})
        else:
            _apply_strict(decision, manifest, candidates, ledger, contract)
    else:
        _apply_assisted(decision, candidates, ledger, contract)
    payload = canonical_json(decision)
    digest = sha256_bytes(payload)
    if existing is None or existing["decision_sha256"] != digest:
        ledger.conn.execute(
            "INSERT OR REPLACE INTO cycle_bindings "
            "(consumer_id, cycle_id, scope, status, authorization, run_id, reason, "
            "contract_sha256, audit_sha256, receipt_sha256, decision_sha256, decision_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                contract.consumer_id,
                cycle,
                scope,
                decision["status"],
                decision["authorization"],
                decision["run_id"],
                decision["reason"],
                decision["contract_sha256"],
                decision["audit_sha256"],
                decision["receipt_sha256"],
                digest,
                payload.decode("utf-8"),
            ),
        )
    # One commit covers the receipt insert (ready) and the binding row: the
    # formal binding is recorded atomically.
    ledger.conn.commit()
    return decision
