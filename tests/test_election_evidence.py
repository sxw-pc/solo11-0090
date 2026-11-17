from __future__ import annotations

import json
from pathlib import Path

import pytest

from pagedrift.election_evidence import ElectionEvidenceError, inspect_election_run, preview_national_list_seats
from pagedrift.pipeline import run_demo

FIXTURE = Path(__file__).resolve().parents[1] / "pagedrift" / "fixtures" / "fictional_election" / "election.json"


def test_safe_precinct_directory_shows_direct_seat_overhang(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    evidence = inspect_election_run(tmp_path / "demo" / "snapshot", FIXTURE)
    assert evidence["list_vote_totals"] == {"A": 450, "B": 350, "C": 200}
    assert evidence["direct_seat_counts"] == {"A": 6, "B": 2, "C": 0}
    assert preview_national_list_seats(evidence, 10) == {"A": 5, "B": 3, "C": 2}
    assert preview_national_list_seats(evidence, 11) == {"A": 5, "B": 4, "C": 2}
    assert preview_national_list_seats(evidence, 12) == {"A": 6, "B": 4, "C": 2}
    assert evidence["fixed_house_decision"] is evidence["overhang_expansion_decision"] is None
    assert len(evidence["contract_sha256"]) == 64
    with pytest.raises(ElectionEvidenceError, match="unsafe"):
        inspect_election_run(tmp_path / "demo" / "offset", FIXTURE)


def test_duplicate_precinct_identity_blocks_election_input(tmp_path: Path) -> None:
    run_demo(tmp_path / "demo")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["precincts"][1]["item_id"] = data["precincts"][0]["item_id"]
    file = tmp_path / "election.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ElectionEvidenceError, match="duplicate"):
        inspect_election_run(tmp_path / "demo" / "snapshot", file)
