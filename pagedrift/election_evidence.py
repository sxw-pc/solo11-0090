"""Verify fictional precinct rows against a safe pagination run.

The preview gives a simple national list allocation before direct seats. It
does not settle either synthetic chamber contract; that job lives in
:mod:`pagedrift.seat_settlement`.

Everything here is a fully synthetic contract for the fictional
``fictional-seven-rivers`` jurisdiction. It deliberately does not model any
real election system or political fact.
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


def _is_nonneg_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def inspect_election_run(run_dir: str | Path, contract_path: str | Path) -> dict[str, Any]:
    """Lock a fictional election fixture to a verified safe pagination run.

    The safe run's unique item ids must exactly cover the fixture precinct
    identities. Bad pagination, duplicated or missing precincts and unknown
    parties all block the evidence.
    """
    root = Path(run_dir).resolve()
    try:
        audit = json.loads((root / "audit.json").read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in (root / "records.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        file = Path(contract_path).resolve()
        contract = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ElectionEvidenceError("election inputs unreadable") from exc
    if audit.get("summary", {}).get("verdict") != "safe" or verify_run(root, None)["status"] != "OK":
        raise ElectionEvidenceError("unsafe or broken pagination run")
    ids = [str(row.get("id", "")) for row in rows]
    if not ids or len(ids) != len(set(ids)) or not all(ids):
        raise ElectionEvidenceError("run has wrong precinct identities")
    precincts = contract.get("precincts")
    direct = contract.get("certified_direct_winners")
    parties = contract.get("parties")
    base_house_seats = contract.get("base_house_seats")
    constituency_seats = contract.get("constituency_seats")
    if (
        contract.get("schema_version") != 1
        or contract.get("jurisdiction") != "fictional-seven-rivers"
        or not isinstance(parties, list)
        or not parties
        or any(not isinstance(name, str) or not name for name in parties)
        or len(set(parties)) != len(parties)
        or not _is_nonneg_int(base_house_seats)
        or base_house_seats < 1
        or not _is_nonneg_int(constituency_seats)
        or constituency_seats > base_house_seats
        or contract.get("party_list_rule") != "d_hondt_exact_fraction"
        or contract.get("tie_break") != "party_id_ascending"
        or contract.get("contracts") != ["fixed_house", "overhang_expansion"]
        or not isinstance(precincts, list)
        or not precincts
        or len(precincts) != len(ids)
        or not isinstance(direct, list)
        or len(direct) != constituency_seats
    ):
        raise ElectionEvidenceError("unsupported fictional election contract")
    run_identities = set(ids)
    seen: set[str] = set()
    totals = {party: 0 for party in parties}
    for precinct in precincts:
        item_id = precinct.get("item_id") if isinstance(precinct, dict) else None
        district = precinct.get("district") if isinstance(precinct, dict) else None
        votes = precinct.get("list_votes") if isinstance(precinct, dict) else None
        if (
            item_id not in run_identities
            or item_id in seen
            or not isinstance(district, str)
            or not district
            or not isinstance(votes, dict)
            or set(votes) != set(parties)
            or any(not _is_nonneg_int(value) for value in votes.values())
        ):
            raise ElectionEvidenceError("invalid or duplicate precinct row")
        seen.add(item_id)
        for party in parties:
            totals[party] += votes[party]
    if seen != run_identities:
        raise ElectionEvidenceError("precinct fixture omits safe-run identity")
    direct_seen: set[str] = set()
    direct_counts = {party: 0 for party in parties}
    winner_rows: list[dict[str, str]] = []
    for row in direct:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("district"), str)
            or not row["district"]
            or row["district"] in direct_seen
            or row.get("winner") not in parties
        ):
            raise ElectionEvidenceError("invalid direct winner")
        direct_seen.add(row["district"])
        direct_counts[row["winner"]] += 1
        winner_rows.append({"district": row["district"], "winner": row["winner"]})
    winner_rows.sort(key=lambda row: row["district"])
    winner_identities = [
        {"seat_index": index, "district": row["district"], "winner": row["winner"]}
        for index, row in enumerate(winner_rows)
    ]
    fixture_sha = hashlib.sha256(file.read_bytes()).hexdigest()
    return {
        "run_id": audit["run_id"],
        "audit_fingerprint": audit["audit_fingerprint"],
        # Both names are kept: contract_sha256 is the historical field,
        # fixture_sha256 is what settlement receipts bind to.
        "contract_sha256": fixture_sha,
        "fixture_sha256": fixture_sha,
        "jurisdiction": contract["jurisdiction"],
        "parties": parties,
        "party_list_rule": contract["party_list_rule"],
        "tie_break": contract["tie_break"],
        "list_vote_totals": totals,
        "direct_seat_counts": direct_counts,
        "certified_direct_winners": winner_identities,
        "base_house_seats": base_house_seats,
        "constituency_seats": constituency_seats,
        "contracts": contract["contracts"],
        "fixed_house_decision": None,
        "overhang_expansion_decision": None,
    }


def preview_national_list_seats(evidence: dict[str, Any], seats: int) -> dict[str, int]:
    """Plain national D'Hondt list preview using exact :class:`Fraction` values.

    Direct seats are not merged here, so this preview can contradict the
    certified direct lower bounds; the settlement contracts reconcile that.
    """
    if not isinstance(seats, int) or seats < 0:
        raise ValueError("invalid seat count")
    totals = evidence["list_vote_totals"]
    parties = evidence["parties"]
    allocation = {party: 0 for party in parties}
    for _ in range(seats):
        party = max(
            parties,
            key=lambda name: (
                Fraction(totals[name], allocation[name] + 1),
                -parties.index(name),
            ),
        )
        allocation[party] += 1
    return allocation
