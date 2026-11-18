"""Tests for the fictional election evidence and seat settlement contracts.

Every vote, rule and majority conclusion in this file is locally synthesised
for the fictional ``fictional-seven-rivers`` jurisdiction. Nothing here refers
to a real election system or political fact.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from pagedrift.election_evidence import (
    ElectionEvidenceError,
    inspect_election_run,
    preview_national_list_seats,
)
from pagedrift.pipeline import run_demo
from pagedrift.seat_settlement import (
    NeverFeasibleSettlementError,
    SeatSettlementError,
    majority_threshold,
    national_dhondt,
    settle_election,
    settle_fixed_house,
    settle_overhang_expansion,
    verify_settlement_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "pagedrift" / "fixtures" / "fictional_election"
CANONICAL = FIXTURE_DIR / "election.json"
TIE_FIXTURE = FIXTURE_DIR / "election_tie.json"
ZERO_FIXTURE = FIXTURE_DIR / "election_zero_vote.json"
OVERHANG_FIXTURE = FIXTURE_DIR / "election_overhang_rounds.json"


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pagedrift", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _load_contract(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_contract(tmp_path: Path, data: dict, name: str = "election.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _mutated_contract(tmp_path: Path, mutate) -> Path:
    data = _load_contract(CANONICAL)
    mutate(data)
    return _write_contract(tmp_path, data)


def _synthetic_evidence(
    parties,
    totals,
    direct_counts,
    *,
    base_house_seats=10,
    run_id="run-synthetic",
):
    """Build evidence with the same shape as inspect_election_run.

    Party names and numbers are arbitrary so settlement logic cannot rely on
    the canonical A/B/C example.
    """
    if sum(direct_counts.values()) > 0:
        districts = [f"C{i}" for i in range(1, sum(direct_counts.values()) + 1)]
    else:
        districts = []
    winners = []
    cursor = 0
    for party in parties:
        for _ in range(direct_counts[party]):
            winners.append(
                {"seat_index": len(winners), "district": districts[cursor], "winner": party}
            )
            cursor += 1
    return {
        "run_id": run_id,
        "audit_fingerprint": "audit-" + run_id,
        "fixture_sha256": "f" * 64,
        "contract_sha256": "f" * 64,
        "jurisdiction": "fictional-seven-rivers",
        "parties": list(parties),
        "party_list_rule": "d_hondt_exact_fraction",
        "tie_break": "party_id_ascending",
        "list_vote_totals": dict(totals),
        "direct_seat_counts": dict(direct_counts),
        "certified_direct_winners": winners,
        "base_house_seats": base_house_seats,
        "constituency_seats": sum(direct_counts.values()),
        "contracts": ["fixed_house", "overhang_expansion"],
    }


@pytest.fixture()
def safe_snapshot(tmp_path: Path) -> Path:
    run_demo(tmp_path / "demo")
    return tmp_path / "demo" / "snapshot"


@pytest.fixture()
def unsafe_offset(tmp_path: Path) -> Path:
    run_demo(tmp_path / "demo")
    return tmp_path / "demo" / "offset"


# ---------------------------------------------------------------------------
# Evidence inspection and the plain national preview
# ---------------------------------------------------------------------------


def test_safe_precinct_directory_shows_direct_seat_overhang(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    assert evidence["list_vote_totals"] == {"A": 450, "B": 350, "C": 200}
    assert evidence["direct_seat_counts"] == {"A": 6, "B": 2, "C": 0}
    assert preview_national_list_seats(evidence, 10) == {"A": 5, "B": 3, "C": 2}
    assert preview_national_list_seats(evidence, 11) == {"A": 5, "B": 4, "C": 2}
    assert preview_national_list_seats(evidence, 12) == {"A": 6, "B": 4, "C": 2}
    assert evidence["fixed_house_decision"] is evidence["overhang_expansion_decision"] is None
    assert len(evidence["contract_sha256"]) == 64
    assert evidence["fixture_sha256"] == evidence["contract_sha256"]
    assert len(evidence["certified_direct_winners"]) == 8
    assert {row["winner"] for row in evidence["certified_direct_winners"] if row["district"] == "D1"} == {"A"}


def test_unsafe_pagination_run_is_blocked(unsafe_offset: Path) -> None:
    with pytest.raises(ElectionEvidenceError, match="unsafe"):
        inspect_election_run(unsafe_offset, CANONICAL)


def test_duplicate_precinct_identity_blocks_election_input(
    safe_snapshot: Path, tmp_path: Path
) -> None:
    path = _mutated_contract(
        tmp_path, lambda data: data["precincts"][1].__setitem__(
            "item_id", data["precincts"][0]["item_id"]
        )
    )
    with pytest.raises(ElectionEvidenceError, match="duplicate"):
        inspect_election_run(safe_snapshot, path)


def test_missing_precinct_identity_blocks_election_input(
    safe_snapshot: Path, tmp_path: Path
) -> None:
    def mutate(data: dict) -> None:
        data["precincts"][5]["item_id"] = "itm-9999"

    path = _mutated_contract(tmp_path, mutate)
    with pytest.raises(ElectionEvidenceError, match="invalid"):
        inspect_election_run(safe_snapshot, path)


def test_unknown_party_vote_row_blocks_election_input(
    safe_snapshot: Path, tmp_path: Path
) -> None:
    def mutate(data: dict) -> None:
        data["precincts"][0]["list_votes"]["Z"] = data["precincts"][0]["list_votes"].pop("C")

    path = _mutated_contract(tmp_path, mutate)
    with pytest.raises(ElectionEvidenceError, match="invalid"):
        inspect_election_run(safe_snapshot, path)


def test_wrong_direct_winner_party_blocks_election_input(
    safe_snapshot: Path, tmp_path: Path
) -> None:
    def mutate(data: dict) -> None:
        data["certified_direct_winners"][0]["winner"] = "Z"

    path = _mutated_contract(tmp_path, mutate)
    with pytest.raises(ElectionEvidenceError, match="invalid direct winner"):
        inspect_election_run(safe_snapshot, path)


def test_tampered_page_bytes_block_election_evidence(
    safe_snapshot: Path,
) -> None:
    requests = [
        json.loads(line)
        for line in (safe_snapshot / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    page = safe_snapshot / requests[0]["evidence_file"]
    page.write_bytes(page.read_bytes() + b"tamper")
    with pytest.raises(ElectionEvidenceError, match="unsafe"):
        inspect_election_run(safe_snapshot, CANONICAL)


def test_revised_fixture_sha_changes_and_old_receipt_cannot_replay(
    safe_snapshot: Path, tmp_path: Path
) -> None:
    before = inspect_election_run(safe_snapshot, CANONICAL)
    # A vote revision keeps the same 30 run-locked identities, so inspection
    # still succeeds, but the fixture fingerprint must change.
    path = _mutated_contract(
        tmp_path, lambda data: data["precincts"][0]["list_votes"].__setitem__("A", 16)
    )
    after = inspect_election_run(safe_snapshot, path)
    assert after["fixture_sha256"] != before["fixture_sha256"]
    assert after["run_id"] == before["run_id"]
    bundle = settle_election(before)
    # A receipt bound to the original fixture cannot be replayed against the
    # revised one: the fixture fingerprint binding differs even though the run
    # and its audit fingerprint are unchanged.
    with pytest.raises(SeatSettlementError, match="different election evidence"):
        verify_settlement_receipt(bundle["fixed_house"], after)
    with pytest.raises(SeatSettlementError, match="different election evidence"):
        verify_settlement_receipt(bundle["overhang_expansion"], after)
    # The same receipts still replay cleanly against the original evidence.
    assert verify_settlement_receipt(bundle["fixed_house"], before)["status"] == "OK"
    assert verify_settlement_receipt(bundle["overhang_expansion"], before)["status"] == "OK"


def test_preview_matches_full_national_dhondt_routine(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    for seats in range(0, 13):
        preview = preview_national_list_seats(evidence, seats)
        allocation, rounds = national_dhondt(
            evidence["parties"], evidence["list_vote_totals"], seats
        )
        assert preview == allocation
        assert len(rounds) == seats


# ---------------------------------------------------------------------------
# Fixed-house contract on the canonical fictional fixture
# ---------------------------------------------------------------------------


def test_fixed_house_keeps_every_direct_seat_and_fills_two_top_ups(
    safe_snapshot: Path,
) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    receipt = settle_fixed_house(evidence)
    assert receipt["house_size"] == 10
    assert receipt["direct_seats_retained"] == {"A": 6, "B": 2, "C": 0}
    # The national preview would give party A only five seats, but its sixth
    # certified direct seat must never be revoked.
    assert receipt["final_seats"] == {"A": 6, "B": 3, "C": 1}
    assert sum(receipt["final_seats"].values()) == 10
    assert receipt["national_list_top_up_seats"] == {"A": 0, "B": 1, "C": 1}
    assert receipt["majority"] == {
        "house_size": 10,
        "majority_threshold_seats": 6,
        "majority_parties": ["A"],
        "single_party_majority": "A",
    }


def test_fixed_house_filling_order_uses_current_total_quotients(
    safe_snapshot: Path,
) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    receipt = settle_fixed_house(evidence)
    rounds = receipt["filling_rounds"]
    assert [row["party"] for row in rounds] == ["C", "B"]
    first, second = rounds
    # First top-up: next quotients measured against retained direct totals
    # (A=6, B=2, C=0): C wins with 200/1 over B=350/3 and A=450/7.
    assert first["quotient"] == str(Fraction(200, 1))
    seen = {row["party"]: Fraction(row["quotient"]) for row in first["quotient_comparison"]}
    assert seen == {"A": Fraction(450, 7), "B": Fraction(350, 3), "C": Fraction(200, 1)}
    assert max(seen, key=lambda party: seen[party]) == "C"
    # Second top-up uses C's *new* total of one: B wins with 350/3.
    assert second["quotient"] == str(Fraction(350, 3))
    seen_two = {row["party"]: Fraction(row["quotient"]) for row in second["quotient_comparison"]}
    assert seen_two == {"A": Fraction(450, 7), "B": Fraction(350, 3), "C": Fraction(200, 2)}
    assert max(seen_two, key=lambda party: seen_two[party]) == "B"
    assert all(row["seat_kind"] == "national_list_top_up" for row in rounds)
    assert all("tie resolved" not in row["reason"] for row in rounds)
    assert receipt["final_seats"] == {"A": 6, "B": 3, "C": 1}


def test_fixture_with_direct_seats_over_base_house_is_rejected_at_inspection(
    safe_snapshot: Path, tmp_path: Path
) -> None:
    def mutate(data: dict) -> None:
        data["base_house_seats"] = 7  # eight certified direct winners

    path = _mutated_contract(tmp_path, mutate)
    with pytest.raises(ElectionEvidenceError, match="unsupported fictional election contract"):
        inspect_election_run(safe_snapshot, path)


def test_fixed_house_refuses_when_direct_seats_exceed_house_size() -> None:
    # The settlement layer keeps its own guard for evidence assembled directly.
    evidence = _synthetic_evidence(
        ["p1", "p2"], {"p1": 70, "p2": 30}, {"p1": 6, "p2": 5}, base_house_seats=10
    )
    with pytest.raises(SeatSettlementError, match="exceed fixed house size"):
        settle_fixed_house(evidence)


def test_fixed_house_zero_vote_party_keeps_its_direct_seat(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, ZERO_FIXTURE)
    receipt = settle_fixed_house(evidence)
    assert receipt["direct_seats_retained"]["D"] == 1
    assert receipt["final_seats"]["D"] == 1
    assert receipt["national_list_top_up_seats"]["D"] == 0
    assert sum(receipt["final_seats"].values()) == receipt["house_size"] == 10


# ---------------------------------------------------------------------------
# Overhang expansion contract on the canonical fictional fixture
# ---------------------------------------------------------------------------


def test_overhang_expansion_finds_minimum_feasible_house_size(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    receipt = settle_overhang_expansion(evidence)
    assert receipt["infeasible_house_sizes"] == [10, 11]
    assert receipt["minimum_feasible_house_size"] == 12
    trace = receipt["feasibility_trace"]
    assert [row["house_size"] for row in trace] == [10, 11, 12]
    assert [row["feasible"] for row in trace] == [False, False, True]
    # Every smaller N must record the precise lower-bound violation.
    assert trace[0]["national_allocation"] == {"A": 5, "B": 3, "C": 2}
    assert trace[0]["shortfalls"] == {"A": 1}
    assert trace[1]["national_allocation"] == {"A": 5, "B": 4, "C": 2}
    assert trace[1]["shortfalls"] == {"A": 1}
    assert trace[2]["shortfalls"] == {}
    # Top-ups are the national allocation minus the direct lower bound, never
    # direct + national added together.
    assert receipt["national_allocation_at_minimum"] == {"A": 6, "B": 4, "C": 2}
    assert receipt["national_list_top_up_seats"] == {"A": 0, "B": 2, "C": 2}
    assert receipt["final_seats"] == {"A": 6, "B": 4, "C": 2}
    assert sum(receipt["final_seats"].values()) == 12
    assert receipt["majority"] == {
        "house_size": 12,
        "majority_threshold_seats": 7,
        "majority_parties": [],
        "single_party_majority": None,
    }


def test_overhang_contract_rejects_naive_alternatives(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    receipt = settle_overhang_expansion(evidence)
    # A plain single national D'Hondt over 10 seats violates the direct lower
    # bound and must not be accepted.
    plain10, _ = national_dhondt(evidence["parties"], evidence["list_vote_totals"], 10)
    assert plain10 == {"A": 5, "B": 3, "C": 2}
    assert any(plain10[p] < evidence["direct_seat_counts"][p] for p in evidence["parties"])
    # Stopping at N=11 by handing A one ad-hoc overhang seat is also wrong:
    # the full N=11 allocation still gives A only five.
    plain11, _ = national_dhondt(evidence["parties"], evidence["list_vote_totals"], 11)
    assert plain11["A"] == 5
    assert receipt["minimum_feasible_house_size"] != 11
    # No double counting: final seats equal the minimum-N national allocation.
    assert receipt["final_seats"] == receipt["national_allocation_at_minimum"]


def test_two_contracts_reach_opposite_majority_conclusions(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    fixed = settle_fixed_house(evidence)
    expansion = settle_overhang_expansion(evidence)
    assert fixed["majority"]["single_party_majority"] == "A"
    assert expansion["majority"]["single_party_majority"] is None
    # Same verified inputs, two legitimate and opposite majority answers.
    assert fixed["binding"] == expansion["binding"]


def test_zero_vote_direct_seat_makes_expansion_never_feasible(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, ZERO_FIXTURE)
    with pytest.raises(NeverFeasibleSettlementError, match="zero-vote"):
        settle_overhang_expansion(evidence)


def test_multi_round_expansion_proves_each_smaller_n_infeasible(
    safe_snapshot: Path,
) -> None:
    evidence = inspect_election_run(safe_snapshot, OVERHANG_FIXTURE)
    receipt = settle_overhang_expansion(evidence)
    assert receipt["infeasible_house_sizes"] == [10, 11, 12]
    assert receipt["minimum_feasible_house_size"] == 13
    assert receipt["national_allocation_at_minimum"] == {"A": 8, "B": 5}
    assert receipt["national_list_top_up_seats"] == {"A": 0, "B": 5}
    for row in receipt["feasibility_trace"][:-1]:
        assert row["feasible"] is False
        assert row["shortfalls"] == {"A": 8 - row["national_allocation"]["A"]}
    assert receipt["feasibility_trace"][-1]["feasible"] is True


# ---------------------------------------------------------------------------
# Exact quotient ties, generic parties, synthetic edge cases
# ---------------------------------------------------------------------------


def test_exact_quotient_tie_resolves_by_party_id_ascending(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, TIE_FIXTURE)
    allocation, rounds = national_dhondt(evidence["parties"], evidence["list_vote_totals"], 10)
    assert allocation == {"A": 5, "B": 5}
    comparison = rounds[0]["tie"]
    # Every party's exact quotient is recorded; the tied winners carry the
    # ascending party-id tie-break and A (first id) is selected.
    assert [row["party"] for row in comparison] == ["A", "B"]
    assert all(Fraction(row["quotient"]) == Fraction(300, 1) for row in comparison)
    assert comparison[0]["selected"] is True
    assert comparison[1]["selected"] is False
    assert all(row["tie_break"] == "party_id_ascending" for row in comparison)
    # Re-running produces byte-identical round evidence.
    again, rounds_again = national_dhondt(evidence["parties"], evidence["list_vote_totals"], 10)
    assert again == allocation
    assert rounds_again == rounds


def test_synthetic_generic_party_tie_is_stable() -> None:
    parties = ["gamma", "alpha", "beta"]  # deliberately not sorted
    totals = {"gamma": 100, "alpha": 100, "beta": 50}
    direct = {"gamma": 2, "alpha": 2, "beta": 1}
    evidence = _synthetic_evidence(parties, totals, direct)
    receipt = settle_fixed_house(evidence)
    # Five direct seats retained; first filling round has gamma=100/3 and
    # alpha=100/3 exactly tied (beta=50/2=25). The tie resolves by party id
    # ascending, so gamma wins round one; alpha then takes round two.
    first = receipt["filling_rounds"][0]
    assert first["party"] == "gamma"
    tied = [
        row
        for row in first["quotient_comparison"]
        if row["tied_at_winning_quotient"]
    ]
    assert [row["party"] for row in tied] == ["alpha", "gamma"]
    assert all(row["tie_break"] == "party_id_ascending" for row in tied)
    non_tied = [
        row
        for row in first["quotient_comparison"]
        if not row["tied_at_winning_quotient"]
    ]
    assert [row["party"] for row in non_tied] == ["beta"]
    assert [row["party"] for row in receipt["filling_rounds"]] == ["gamma", "alpha", "gamma", "alpha", "beta"]
    assert "tie resolved by party_id_ascending" in receipt["filling_rounds"][0]["reason"]
    assert receipt["final_seats"] == {"gamma": 4, "alpha": 4, "beta": 2}
    assert sum(receipt["final_seats"].values()) == 10


def test_synthetic_zero_vote_party_without_direct_seat_never_wins_a_seat() -> None:
    parties = ["p1", "p2", "quiet"]
    totals = {"p1": 90, "p2": 30, "quiet": 0}
    direct = {"p1": 3, "p2": 1, "quiet": 0}
    evidence = _synthetic_evidence(parties, totals, direct)
    fixed = settle_fixed_house(evidence)
    expansion = settle_overhang_expansion(evidence)
    assert fixed["final_seats"]["quiet"] == 0
    assert expansion["final_seats"]["quiet"] == 0
    assert fixed["final_seats"]["p1"] >= 3


def test_synthetic_zero_vote_party_with_direct_seat_is_never_feasible() -> None:
    parties = ["p1", "p2"]
    totals = {"p1": 100, "p2": 0}
    direct = {"p1": 2, "p2": 1}
    evidence = _synthetic_evidence(parties, totals, direct)
    with pytest.raises(NeverFeasibleSettlementError, match="zero-vote"):
        settle_overhang_expansion(evidence)


def test_synthetic_all_zero_votes_with_direct_seats_is_never_feasible() -> None:
    parties = ["p1", "p2"]
    totals = {"p1": 0, "p2": 0}
    direct = {"p1": 1, "p2": 0}
    evidence = _synthetic_evidence(parties, totals, direct)
    with pytest.raises(NeverFeasibleSettlementError):
        settle_overhang_expansion(evidence)


def test_synthetic_direct_seats_over_base_house_block_fixed_contract() -> None:
    parties = ["p1", "p2"]
    totals = {"p1": 70, "p2": 30}
    direct = {"p1": 6, "p2": 5}
    evidence = _synthetic_evidence(parties, totals, direct, base_house_seats=10)
    with pytest.raises(SeatSettlementError, match="exceed fixed house size"):
        settle_fixed_house(evidence)


def test_synthetic_no_direct_seats_contracts_reduce_to_national_allocation() -> None:
    parties = ["p1", "p2", "p3"]
    totals = {"p1": 50, "p2": 30, "p3": 20}
    direct = {"p1": 0, "p2": 0, "p3": 0}
    evidence = _synthetic_evidence(parties, totals, direct)
    fixed = settle_fixed_house(evidence)
    expansion = settle_overhang_expansion(evidence)
    expected, _ = national_dhondt(parties, totals, 10)
    assert fixed["final_seats"] == expected
    assert expansion["minimum_feasible_house_size"] == 10
    assert expansion["final_seats"] == expected
    assert expansion["infeasible_house_sizes"] == []


def test_malformed_evidence_is_rejected() -> None:
    with pytest.raises(SeatSettlementError, match="inspected election evidence"):
        settle_fixed_house({})
    good = _synthetic_evidence(["p1", "p2"], {"p1": 60, "p2": 40}, {"p1": 1, "p2": 1})
    broken = copy.deepcopy(good)
    broken["fixture_sha256"] = "too-short"
    with pytest.raises(SeatSettlementError, match="invalid fields"):
        settle_fixed_house(broken)
    broken_two = copy.deepcopy(good)
    broken_two["certified_direct_winners"][0]["winner"] = "p2"
    with pytest.raises(SeatSettlementError, match="identities disagree"):
        settle_fixed_house(broken_two)


# ---------------------------------------------------------------------------
# Receipt binding, replay and double-counting guards
# ---------------------------------------------------------------------------


def test_receipts_bind_run_audit_fixture_and_round_evidence(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    bundle = settle_election(evidence)
    for name in ("fixed_house", "overhang_expansion"):
        receipt = bundle[name]
        assert receipt["binding"] == {
            "run_id": evidence["run_id"],
            "audit_fingerprint": evidence["audit_fingerprint"],
            "fixture_sha256": evidence["fixture_sha256"],
        }
        assert len(receipt["receipt_sha256"]) == 64
        assert receipt["certified_direct_winners"] == evidence["certified_direct_winners"]
        assert receipt["list_vote_totals"] == evidence["list_vote_totals"]
        assert receipt["tie_break"] == "party_id_ascending"
        assert receipt["party_list_rule"] == "d_hondt_exact_fraction"


def test_receipt_replay_is_deterministic_and_detects_tampering(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    bundle = settle_election(evidence)
    assert verify_settlement_receipt(bundle["fixed_house"], evidence)["status"] == "OK"
    assert verify_settlement_receipt(bundle["overhang_expansion"], evidence)["status"] == "OK"
    # Re-settling from the same evidence reproduces the identical fingerprints.
    again = settle_election(evidence)
    assert again["fixed_house"]["receipt_sha256"] == bundle["fixed_house"]["receipt_sha256"]
    assert (
        again["overhang_expansion"]["receipt_sha256"]
        == bundle["overhang_expansion"]["receipt_sha256"]
    )
    tampered = copy.deepcopy(bundle)
    tampered["fixed_house"]["final_seats"]["A"] = 99
    with pytest.raises(SeatSettlementError, match="fingerprint mismatch"):
        verify_settlement_receipt(tampered["fixed_house"], evidence)
    tampered_two = copy.deepcopy(bundle)
    tampered_two["overhang_expansion"]["minimum_feasible_house_size"] = 11
    with pytest.raises(SeatSettlementError, match="fingerprint mismatch"):
        verify_settlement_receipt(tampered_two["overhang_expansion"], evidence)


def test_no_contract_double_counts_direct_and_national_seats(safe_snapshot: Path) -> None:
    evidence = inspect_election_run(safe_snapshot, CANONICAL)
    fixed = settle_fixed_house(evidence)
    expansion = settle_overhang_expansion(evidence)
    for receipt in (fixed, expansion):
        direct = receipt["direct_seats_retained"]
        top_ups = receipt["national_list_top_up_seats"]
        for party in evidence["parties"]:
            assert receipt["final_seats"][party] == direct[party] + top_ups[party]
            assert top_ups[party] >= 0
        assert sum(receipt["final_seats"].values()) == receipt["majority"]["house_size"]
    # Expansion final seats are exactly the minimum-N national allocation.
    assert expansion["final_seats"] == expansion["national_allocation_at_minimum"]


def test_majority_threshold_is_strict_half_plus_one() -> None:
    assert majority_threshold(10) == 6
    assert majority_threshold(11) == 6
    assert majority_threshold(12) == 7
    assert majority_threshold(1) == 1


# ---------------------------------------------------------------------------
# Local CLI
# ---------------------------------------------------------------------------


def test_election_cli_inspect_settle_and_replay(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    assert _run_cli("demo", "--out", str(demo)).returncode == 0
    snapshot = demo / "snapshot"
    inspect_proc = _run_cli("election", "inspect", str(snapshot), str(CANONICAL))
    assert inspect_proc.returncode == 0, inspect_proc.stderr
    payload = json.loads(inspect_proc.stdout)
    assert payload["list_vote_totals"] == {"A": 450, "B": 350, "C": 200}
    receipt_path = tmp_path / "settlement.json"
    settle_proc = _run_cli(
        "election", "settle", str(snapshot), str(CANONICAL), "--out", str(receipt_path)
    )
    assert settle_proc.returncode == 0, settle_proc.stderr
    assert "fixed_house:" in settle_proc.stdout
    assert "overhang_expansion:" in settle_proc.stdout
    bundle = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert bundle["fixed_house"]["final_seats"] == {"A": 6, "B": 3, "C": 1}
    assert bundle["overhang_expansion"]["minimum_feasible_house_size"] == 12
    verify_proc = _run_cli(
        "election",
        "verify-receipt",
        str(snapshot),
        str(CANONICAL),
        "--receipt",
        str(receipt_path),
    )
    assert verify_proc.returncode == 0, verify_proc.stderr
    assert "fixed_house: OK" in verify_proc.stdout
    assert "overhang_expansion: OK" in verify_proc.stdout


def test_election_cli_blocks_unsafe_run(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    assert _run_cli("demo", "--out", str(demo)).returncode == 0
    proc = _run_cli("election", "inspect", str(demo / "offset"), str(CANONICAL))
    assert proc.returncode == 2
    assert "unsafe" in proc.stderr


def test_election_cli_reports_never_feasible_contract(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    assert _run_cli("demo", "--out", str(demo)).returncode == 0
    receipt_path = tmp_path / "zero.json"
    proc = _run_cli(
        "election",
        "settle",
        str(demo / "snapshot"),
        str(ZERO_FIXTURE),
        "--out",
        str(receipt_path),
    )
    assert proc.returncode == 2
    assert "zero-vote" in proc.stderr
    assert not receipt_path.exists()


def test_module_cli_runs_both_contracts(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    assert _run_cli("demo", "--out", str(demo)).returncode == 0
    receipt_path = tmp_path / "module-settlement.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pagedrift.seat_settlement",
            "both",
            str(demo / "snapshot"),
            str(CANONICAL),
            "--out",
            str(receipt_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    bundle = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert bundle["fixed_house"]["final_seats"] == {"A": 6, "B": 3, "C": 1}
    assert bundle["overhang_expansion"]["final_seats"] == {"A": 6, "B": 4, "C": 2}


def test_module_cli_single_contract_to_stdout(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    assert _run_cli("demo", "--out", str(demo)).returncode == 0
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pagedrift.seat_settlement",
            "fixed-house",
            str(demo / "snapshot"),
            str(CANONICAL),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["contract"] == "fixed_house"
    assert receipt["final_seats"] == {"A": 6, "B": 3, "C": 1}
