"""Two pre-fixed fictional chamber contracts over one verified evidence base.

Both contracts consume the same list-vote totals and certified direct winners
produced by :func:`pagedrift.election_evidence.inspect_election_run`. They are
synthetic algorithms for the fictional ``fictional-seven-rivers`` jurisdiction
and do not describe any real election system or political fact.

Contract 1, ``fixed_house``:
    The chamber always has exactly ``base_house_seats`` seats. Every certified
    direct winner keeps their seat first; a national list preview that would
    give a party fewer seats is never allowed to revoke a direct seat. The
    remaining seats are filled one at a time using the next D'Hondt quotient of
    each party's *current total* seat count.

Contract 2, ``overhang_expansion``:
    Direct seats are kept but the chamber may grow. Find the smallest house
    size ``N >= base_house_seats`` whose full national D'Hondt allocation over
    ``N`` seats gives every party at least as many seats as its certified
    direct count. Every smaller ``N`` is recorded as proven infeasible. Each
    party's list top-up seats are then the national allocation minus its direct
    lower bound, so direct and national seats are never double counted.

All quotient comparisons use exact :class:`fractions.Fraction` values and the
contract's stable ``party_id_ascending`` tie rule.
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from typing import Any, Sequence

from .ids import canonical_json, sha256_bytes
from .election_evidence import ElectionEvidenceError

TIE_BREAK_PARTY_ID_ASCENDING = "party_id_ascending"
FIXED_HOUSE = "fixed_house"
OVERHANG_EXPANSION = "overhang_expansion"
RECEIPT_SCHEMA_VERSION = 1
# Defensive ceiling only; a feasible N always exists whenever every party that
# holds a direct seat also earned at least one list vote.
MAX_EXPANSION_ROUNDS = 10_000


class SeatSettlementError(ValueError):
    """Raised when an input cannot be settled under the requested contract."""


class NeverFeasibleSettlementError(SeatSettlementError):
    """No finite national allocation can satisfy the direct lower bounds."""

    def __init__(self, message: str, feasibility_trace: list[dict[str, Any]]):
        super().__init__(message)
        self.feasibility_trace = feasibility_trace


def _require_evidence(evidence: dict[str, Any]) -> None:
    """Validate the shape of an inspected-election evidence dictionary."""
    parties = evidence.get("parties")
    totals = evidence.get("list_vote_totals")
    direct = evidence.get("direct_seat_counts")
    winners = evidence.get("certified_direct_winners")
    required = (
        "run_id",
        "audit_fingerprint",
        "fixture_sha256",
        "base_house_seats",
        "constituency_seats",
        "tie_break",
        "party_list_rule",
    )
    if not isinstance(evidence, dict) or any(key not in evidence for key in required):
        raise SeatSettlementError("settlement requires inspected election evidence")
    if (
        not isinstance(parties, list)
        or not parties
        or any(not isinstance(name, str) or not name for name in parties)
        or len(set(parties)) != len(parties)
        or not isinstance(totals, dict)
        or set(totals) != set(parties)
        or any(type(value) is not int or value < 0 for value in totals.values())
        or not isinstance(direct, dict)
        or set(direct) != set(parties)
        or any(type(value) is not int or value < 0 for value in direct.values())
        or not isinstance(winners, list)
        or len(winners) != sum(direct.values())
        or evidence["tie_break"] != TIE_BREAK_PARTY_ID_ASCENDING
        or type(evidence["base_house_seats"]) is not int
        or evidence["base_house_seats"] < 1
        or type(evidence["constituency_seats"]) is not int
        or evidence["constituency_seats"] < 0
        or not isinstance(evidence["run_id"], str)
        or not evidence["run_id"]
        or not isinstance(evidence["audit_fingerprint"], str)
        or not evidence["audit_fingerprint"]
        or not isinstance(evidence["fixture_sha256"], str)
        or len(evidence["fixture_sha256"]) != 64
    ):
        raise SeatSettlementError("settlement evidence has invalid fields")
    winner_count = {party: 0 for party in parties}
    seen_districts: set[str] = set()
    for row in winners:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("district"), str)
            or not row["district"]
            or row["district"] in seen_districts
            or row.get("winner") not in parties
        ):
            raise SeatSettlementError("settlement evidence has invalid direct winner")
        seen_districts.add(row["district"])
        winner_count[row["winner"]] += 1
    if winner_count != direct:
        raise SeatSettlementError("direct winner identities disagree with direct counts")
    if evidence["constituency_seats"] != sum(direct.values()):
        raise SeatSettlementError("constituency seat count disagrees with direct winners")


def _next_quotient(totals: dict[str, int], seats_held: int, party: str) -> Fraction:
    """Exact D'Hondt quotient for the next seat of ``party``."""
    return Fraction(totals[party], seats_held + 1)


def _choose_next(
    parties: Sequence[str],
    totals: dict[str, int],
    seats: dict[str, int],
) -> tuple[str, Fraction, list[dict[str, Any]]]:
    """Pick the party with the highest next quotient; ties resolve by party id.

    The comparison lists *every* party's exact next quotient, flagging when an
    exact tie at the winning quotient is broken.
    """
    quotients = {party: _next_quotient(totals, seats[party], party) for party in parties}
    winner = max(parties, key=lambda name: (quotients[name], -parties.index(name)))
    winning = quotients[winner]
    tied_parties = sorted(party for party in parties if quotients[party] == winning)
    is_tie = len(tied_parties) > 1

    def _order_key(party: str) -> tuple[int, str]:
        # Parties tied at the winning quotient are presented in ascending
        # party-id order; everyone else follows the evidence order.
        return (0, party) if party in tied_parties else (1, party)

    comparison = [
        {
            "party": party,
            "quotient": str(quotients[party]),
            "current_total_seats": seats[party],
            "selected": party == winner,
            "tied_at_winning_quotient": party in tied_parties,
            "tie_break": TIE_BREAK_PARTY_ID_ASCENDING if is_tie else "none",
        }
        for party in sorted(parties, key=_order_key)
    ]
    return winner, winning, comparison


def national_dhondt(
    parties: Sequence[str],
    totals: dict[str, int],
    seats: int,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """One full national D'Hondt allocation with round-by-round evidence."""
    if not isinstance(seats, int) or seats < 0:
        raise SeatSettlementError("national allocation requires a non-negative seat count")
    allocation = {party: 0 for party in parties}
    rounds: list[dict[str, Any]] = []
    for round_index in range(1, seats + 1):
        winner, quotient, tie_rows = _choose_next(parties, totals, allocation)
        allocation[winner] += 1
        rounds.append(
            {
                "round": round_index,
                "party": winner,
                "quotient": str(quotient),
                "tie": tie_rows,
                "allocation_after": dict(allocation),
            }
        )
    return allocation, rounds


def majority_threshold(house_size: int) -> int:
    """Smallest number of seats that is strictly more than half the chamber."""
    return house_size // 2 + 1


def _binding(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": evidence["run_id"],
        "audit_fingerprint": evidence["audit_fingerprint"],
        "fixture_sha256": evidence["fixture_sha256"],
    }


def _base_receipt(contract: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "contract": contract,
        "jurisdiction": evidence.get("jurisdiction", "fictional-seven-rivers"),
        "parties": list(evidence["parties"]),
        "party_list_rule": evidence["party_list_rule"],
        "tie_break": evidence["tie_break"],
        "binding": _binding(evidence),
        "list_vote_totals": dict(evidence["list_vote_totals"]),
        "direct_seat_counts": dict(evidence["direct_seat_counts"]),
        "certified_direct_winners": [dict(row) for row in evidence["certified_direct_winners"]],
    }


def _has_majority(seats: dict[str, int], house_size: int) -> dict[str, Any]:
    threshold = majority_threshold(house_size)
    majority_parties = sorted(party for party, count in seats.items() if count >= threshold)
    return {
        "house_size": house_size,
        "majority_threshold_seats": threshold,
        "majority_parties": majority_parties,
        "single_party_majority": majority_parties[0] if len(majority_parties) == 1 else None,
    }


def settle_fixed_house(evidence: dict[str, Any]) -> dict[str, Any]:
    """Settle the strict fixed-size chamber contract.

    All direct seats are retained first; the remaining seats are filled by the
    next quotient of each party's current total seat count.
    """
    _require_evidence(evidence)
    parties = evidence["parties"]
    totals = evidence["list_vote_totals"]
    direct = evidence["direct_seat_counts"]
    house_size = evidence["base_house_seats"]
    direct_total = sum(direct.values())
    if direct_total > house_size:
        raise SeatSettlementError(
            f"direct seats {direct_total} exceed fixed house size {house_size}"
        )
    seats = {party: direct[party] for party in parties}
    filling_rounds: list[dict[str, Any]] = []
    for round_index in range(1, house_size - direct_total + 1):
        winner, quotient, comparison = _choose_next(parties, totals, seats)
        is_tie = any(
            row["tied_at_winning_quotient"] and not row["selected"] for row in comparison
        )
        reason = "highest next D'Hondt quotient against current total seat count"
        if is_tie:
            reason += "; exact quotient tie resolved by party_id_ascending"
        seats[winner] += 1
        filling_rounds.append(
            {
                "round": round_index,
                "seat_kind": "national_list_top_up",
                "party": winner,
                "quotient": str(quotient),
                "reason": reason,
                "quotient_comparison": comparison,
                "seats_after": dict(seats),
            }
        )
    list_top_ups = {party: seats[party] - direct[party] for party in parties}
    receipt = _base_receipt(FIXED_HOUSE, evidence)
    receipt.update(
        {
            "house_size": house_size,
            "direct_seats_retained": dict(direct),
            "national_list_top_up_seats": list_top_ups,
            "filling_rounds": filling_rounds,
            "final_seats": dict(seats),
            "majority": _has_majority(seats, house_size),
        }
    )
    return _wrap_receipt(receipt)


def _structural_infeasibility(evidence: dict[str, Any]) -> str | None:
    """Return a reason string if no finite N can ever satisfy the bounds."""
    totals = evidence["list_vote_totals"]
    direct = evidence["direct_seat_counts"]
    zero_vote_parties = {party for party, votes in totals.items() if votes == 0}
    positive_vote_parties = {party for party, votes in totals.items() if votes > 0}
    if not positive_vote_parties and sum(direct.values()) > 0:
        return (
            "no party earned list votes, so the national D'Hondt allocation can only "
            "seat parties through the ascending party-id tie rule and cannot match the "
            "certified direct winners"
        )
    stranded = sorted(party for party in zero_vote_parties if direct[party] > 0)
    if stranded:
        return (
            f"zero-vote party/parties {stranded} hold direct seats but a national "
            "D'Hondt allocation can never award a seat to a zero-vote party while any "
            "party has positive votes"
        )
    return None


def settle_overhang_expansion(evidence: dict[str, Any]) -> dict[str, Any]:
    """Settle the overhang expansion contract.

    Search every house size from ``base_house_seats`` upward, keeping proof of
    infeasibility for every smaller N, and stop at the first feasible full
    national D'Hondt allocation.
    """
    _require_evidence(evidence)
    parties = evidence["parties"]
    totals = evidence["list_vote_totals"]
    direct = evidence["direct_seat_counts"]
    base_size = evidence["base_house_seats"]
    feasibility_trace: list[dict[str, Any]] = []
    structural = _structural_infeasibility(evidence)
    if structural is not None:
        raise NeverFeasibleSettlementError(structural, feasibility_trace)
    minimum_n: int | None = None
    national_at_minimum: dict[str, int] | None = None
    rounds_at_minimum: list[dict[str, Any]] | None = None
    for n in range(base_size, base_size + MAX_EXPANSION_ROUNDS + 1):
        allocation, rounds = national_dhondt(parties, totals, n)
        shortfalls = {
            party: direct[party] - allocation[party]
            for party in parties
            if allocation[party] < direct[party]
        }
        trace_row = {
            "house_size": n,
            "national_allocation": dict(allocation),
            "direct_lower_bound": dict(direct),
            "shortfalls": dict(shortfalls),
            "feasible": not shortfalls,
        }
        feasibility_trace.append(trace_row)
        if not shortfalls:
            minimum_n = n
            national_at_minimum = allocation
            rounds_at_minimum = rounds
            break
    if minimum_n is None:
        raise NeverFeasibleSettlementError(
            "no feasible house size found within the expansion safety bound", feasibility_trace
        )
    # List top-ups are defined relative to the direct lower bound, so direct and
    # national seats are never added together.
    list_top_ups = {
        party: national_at_minimum[party] - direct[party] for party in parties
    }
    receipt = _base_receipt(OVERHANG_EXPANSION, evidence)
    receipt.update(
        {
            "base_house_seats": base_size,
            "minimum_feasible_house_size": minimum_n,
            "infeasible_house_sizes": [row["house_size"] for row in feasibility_trace[:-1]],
            "feasibility_trace": feasibility_trace,
            "national_allocation_at_minimum": dict(national_at_minimum),
            "national_dhondt_rounds_at_minimum": rounds_at_minimum,
            "direct_seats_retained": dict(direct),
            "national_list_top_up_seats": list_top_ups,
            "final_seats": dict(national_at_minimum),
            "majority": _has_majority(national_at_minimum, minimum_n),
        }
    )
    return _wrap_receipt(receipt)


def _wrap_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    material = canonical_json(receipt)
    wrapped = dict(receipt)
    wrapped["receipt_sha256"] = sha256_bytes(material)
    return wrapped


def verify_settlement_receipt(
    receipt: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Replay a receipt against the evidence and confirm its fingerprint."""
    if not isinstance(receipt, dict) or "receipt_sha256" not in receipt:
        raise SeatSettlementError("not a settlement receipt")
    stored = receipt["receipt_sha256"]
    material = canonical_json({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    if sha256_bytes(material) != stored:
        raise SeatSettlementError("receipt fingerprint mismatch")
    binding = receipt.get("binding", {})
    if (
        binding.get("run_id") != evidence["run_id"]
        or binding.get("audit_fingerprint") != evidence["audit_fingerprint"]
        or binding.get("fixture_sha256") != evidence["fixture_sha256"]
    ):
        raise SeatSettlementError("receipt is bound to different election evidence")
    contract = receipt.get("contract")
    if contract == FIXED_HOUSE:
        rebuilt = settle_fixed_house(evidence)
    elif contract == OVERHANG_EXPANSION:
        rebuilt = settle_overhang_expansion(evidence)
    else:
        raise SeatSettlementError(f"unknown settlement contract: {contract!r}")
    if rebuilt["receipt_sha256"] != stored:
        raise SeatSettlementError("replayed settlement disagrees with stored receipt")
    return {"status": "OK", "contract": contract, "receipt_sha256": stored}


def settle_election(evidence: dict[str, Any]) -> dict[str, Any]:
    """Run both pre-fixed contracts over the same verified evidence."""
    _require_evidence(evidence)
    fixed = settle_fixed_house(evidence)
    expansion = settle_overhang_expansion(evidence)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "jurisdiction": evidence.get("jurisdiction", "fictional-seven-rivers"),
        "binding": _binding(evidence),
        "parties": list(evidence["parties"]),
        "list_vote_totals": dict(evidence["list_vote_totals"]),
        "direct_seat_counts": dict(evidence["direct_seat_counts"]),
        "fixed_house": fixed,
        "overhang_expansion": expansion,
    }


def write_receipt_bundle(path: str, bundle: dict[str, Any]) -> None:
    """Persist a settlement bundle with stable two-space indentation."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# Re-exported for callers that only want to handle settlement-layer failures.
__all__ = [
    "SeatSettlementError",
    "NeverFeasibleSettlementError",
    "ElectionEvidenceError",
    "settle_fixed_house",
    "settle_overhang_expansion",
    "settle_election",
    "verify_settlement_receipt",
    "national_dhondt",
    "majority_threshold",
    "write_receipt_bundle",
    "FIXED_HOUSE",
    "OVERHANG_EXPANSION",
]


def _build_module_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m pagedrift.seat_settlement",
        description=(
            "local CLI for the two pre-fixed fictional seat settlement contracts "
            "(synthetic data only; no real election is modelled)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    fixed = sub.add_parser("fixed-house", help="settle the strict fixed-size chamber")
    fixed.add_argument("run_dir")
    fixed.add_argument("fixture")
    fixed.add_argument("--out", default=None)
    expansion = sub.add_parser(
        "overhang-expansion", help="settle the minimum-expansion chamber"
    )
    expansion.add_argument("run_dir")
    expansion.add_argument("fixture")
    expansion.add_argument("--out", default=None)
    both = sub.add_parser("both", help="settle both contracts and write the receipt bundle")
    both.add_argument("run_dir")
    both.add_argument("fixture")
    both.add_argument("--out", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from .election_evidence import inspect_election_run

    parser = _build_module_parser()
    args = parser.parse_args(argv)
    try:
        evidence = inspect_election_run(args.run_dir, args.fixture)
        if args.command == "fixed-house":
            receipt = settle_fixed_house(evidence)
        elif args.command == "overhang-expansion":
            receipt = settle_overhang_expansion(evidence)
        else:
            receipt = settle_election(evidence)
            write_receipt_bundle(args.out, receipt)
            print(f"wrote {args.out}")
            return 0
        if args.out:
            write_receipt_bundle(args.out, receipt)
            print(f"wrote {args.out}")
        else:
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    except (ElectionEvidenceError, SeatSettlementError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
