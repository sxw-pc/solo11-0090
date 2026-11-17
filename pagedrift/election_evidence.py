"""Verify fictional precinct rows against a safe pagination run.

The preview gives a simple national list allocation before direct seats. It
does not settle either synthetic chamber contract.
"""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .evidence import verify_run


class ElectionEvidenceError(ValueError):
    pass


def inspect_election_run(run_dir: str | Path, contract_path: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    try:
        audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (root / "records.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        file = Path(contract_path).resolve()
        contract = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ElectionEvidenceError("election inputs unreadable") from exc
    if audit.get("summary", {}).get("verdict") != "safe" or verify_run(root, None)["status"] != "OK":
        raise ElectionEvidenceError("unsafe or broken pagination run")
    ids = [str(row.get("id", "")) for row in rows]
    if len(ids) != 30 or len(set(ids)) != 30 or not all(ids):
        raise ElectionEvidenceError("run has wrong precinct identities")
    precincts = contract.get("precincts")
    direct = contract.get("certified_direct_winners")
    parties = contract.get("parties")
    if (contract.get("schema_version") != 1
            or contract.get("jurisdiction") != "fictional-seven-rivers"
            or parties != ["A", "B", "C"]
            or contract.get("base_house_seats") != 10
            or contract.get("constituency_seats") != 8
            or contract.get("party_list_rule") != "d_hondt_exact_fraction"
            or contract.get("tie_break") != "party_id_ascending"
            or contract.get("contracts") != ["fixed_house", "overhang_expansion"]
            or not isinstance(precincts, list) or len(precincts) != 30
            or not isinstance(direct, list) or len(direct) != 8):
        raise ElectionEvidenceError("unsupported fictional election contract")
    seen = set()
    totals = {party: 0 for party in parties}
    districts = {f"D{i}" for i in range(1, 9)}
    for precinct in precincts:
        item_id = precinct.get("item_id") if isinstance(precinct, dict) else None
        votes = precinct.get("list_votes") if isinstance(precinct, dict) else None
        if (item_id not in ids or item_id in seen or precinct.get("district") not in districts
                or not isinstance(votes, dict) or set(votes) != set(parties)
                or any(type(value) is not int or value < 0 for value in votes.values())):
            raise ElectionEvidenceError("invalid or duplicate precinct row")
        seen.add(item_id)
        for party in parties:
            totals[party] += votes[party]
    if seen != set(ids):
        raise ElectionEvidenceError("precinct fixture omits safe-run identity")
    direct_seen = set()
    direct_counts = {party: 0 for party in parties}
    for row in direct:
        if (not isinstance(row, dict) or row.get("district") not in districts
                or row["district"] in direct_seen or row.get("winner") not in parties):
            raise ElectionEvidenceError("invalid direct winner")
        direct_seen.add(row["district"])
        direct_counts[row["winner"]] += 1
    if direct_seen != districts:
        raise ElectionEvidenceError("direct district missing")
    return {
        "run_id": audit["run_id"],
        "audit_fingerprint": audit["audit_fingerprint"],
        "contract_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        "jurisdiction": contract["jurisdiction"],
        "parties": parties,
        "list_vote_totals": totals,
        "direct_seat_counts": direct_counts,
        "base_house_seats": 10,
        "contracts": contract["contracts"],
        "fixed_house_decision": None,
        "overhang_expansion_decision": None,
    }


def preview_national_list_seats(evidence: dict[str, Any], seats: int) -> dict[str, int]:
    if not isinstance(seats, int) or seats < 0:
        raise ValueError("invalid seat count")
    totals = evidence["list_vote_totals"]
    allocation = {party: 0 for party in evidence["parties"]}
    for _ in range(seats):
        party = max(evidence["parties"], key=lambda name: (Fraction(totals[name], allocation[name] + 1),
                                                        -evidence["parties"].index(name)))
        allocation[party] += 1
    return allocation
