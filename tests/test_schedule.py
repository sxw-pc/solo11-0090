from __future__ import annotations
from pathlib import Path
import pytest
from pagedrift.mock import MutationSchedule, ScheduleError, Universe
from helpers import make_records
SCHEDULE = """
version: 1
before_request:
  - at: 2
    mutations:
      - type: insert
        id: itm-0901
        sort_key: 14
        title: Inserted
        category: inserted
      - type: delete
        id: itm-0001
      - type: update_sortkey
        id: itm-0002
        sort_key: 70
      - type: noop
        id: itm-0003
"""

def _load(tmp_path: Path, text: str = SCHEDULE) -> MutationSchedule:
    path = tmp_path / "schedule.yaml"
    path.write_text(text, encoding="utf-8")
    return MutationSchedule.load(path)

def test_schedule_applies_only_at_declared_request(tmp_path: Path) -> None:
    schedule = _load(tmp_path)
    universe = Universe(make_records(5))
    assert schedule.apply(1, universe) == []
    logs = schedule.apply(2, universe)
    assert [row["type"] for row in logs] == ["insert", "delete", "update_sortkey", "noop"]
    assert all(row["applied"] for row in logs)
    assert schedule.apply(3, universe) == []
    assert universe.version == 3
    assert universe.get("itm-0901").sort_key == 14
    assert universe.get("itm-0001") is None
    assert universe.get("itm-0002").sort_key == 70

def test_schedule_log_is_deterministic_and_has_before_after(tmp_path: Path) -> None:
    first = Universe(make_records(5))
    second = Universe(make_records(5))
    logs_a = _load(tmp_path).apply(2, first)
    logs_b = _load(tmp_path).apply(2, second)
    assert logs_a == logs_b
    insert_log = logs_a[0]
    assert insert_log["before"] is None
    assert insert_log["after"]["id"] == "itm-0901"
    assert insert_log["universe_version"] == 1
    update_log = logs_a[2]
    assert update_log["before"]["sort_key"] == 13
    assert update_log["after"]["sort_key"] == 70

def test_missing_delete_target_is_logged_not_crashing(tmp_path: Path) -> None:
    schedule = _load(
        tmp_path,
        """
version: 1
before_request:
  - at: 1
    mutations:
      - type: delete
        id: itm-9999
""",
    )
    logs = schedule.apply(1, Universe(make_records(3)))
    assert logs[0]["applied"] is False
    assert logs[0]["reason"] == "natural_key_not_found"

def test_unknown_mutation_type_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
version: 1
before_request:
  - at: 1
    mutations:
      - type: rewrite_everything
""",
        encoding="utf-8",
    )
    with pytest.raises(ScheduleError):
        MutationSchedule.load(path)

def test_negative_request_number_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
version: 1
before_request:
  - at: 0
    mutations: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ScheduleError):
        MutationSchedule.load(path)

def test_noop_does_not_increment_universe_version(tmp_path: Path) -> None:
    schedule = _load(
        tmp_path,
        """
version: 1
before_request:
  - at: 1
    mutations:
      - type: noop
""",
    )
    universe = Universe(make_records(3))
    logs = schedule.apply(1, universe)
    assert logs[0]["applied"] is True
    assert universe.version == 0
