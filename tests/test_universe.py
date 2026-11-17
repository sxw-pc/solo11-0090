from __future__ import annotations
import json
import pytest
from pagedrift.mock import QueryError, Universe
from helpers import make_records

def test_offset_and_page_semantics_are_equivalent() -> None:
    universe = Universe(make_records(10))
    offset_page = universe.page_of(universe.current_records(), mode="offset", offset=3, limit=3)
    page_page = universe.page_of(universe.current_records(), mode="pagenum", offset=6, limit=3)
    assert [row["id"] for row in offset_page["items"]] == ["itm-0004", "itm-0005", "itm-0006"]
    assert [row["id"] for row in page_page["items"]] == ["itm-0007", "itm-0008", "itm-0009"]
    assert offset_page["page"]["returned"] == 3
    assert page_page["universe"]["total"] == 10

def test_keyset_returns_strictly_greater_keys() -> None:
    universe = Universe(make_records(8))
    page = universe.page_of(universe.current_records(), mode="keyset", offset=0, limit=3, after_key=16)
    assert [row["sort_key"] for row in page["items"]] == [19, 22, 25]
    assert page["page"]["after_key"] == 16

def test_snapshot_pins_records_against_later_mutations() -> None:
    universe = Universe(make_records(6))
    token, pinned = universe.create_snapshot()
    universe.apply_delete("itm-0001")
    universe.apply_insert(
        natural_key="itm-0901",
        sort_key=11,
        title="late",
        category="inserted",
        payload_sha8="deadbeef",
    )
    pinned_page = universe.page_of(pinned, mode="snapshot", offset=0, limit=6, snapshot_token=token)
    current_page = universe.page_of(universe.current_records(), mode="offset", offset=0, limit=6)
    assert [row["id"] for row in pinned_page["items"]][0] == "itm-0001"
    assert len(pinned_page["items"]) == 6
    assert "itm-0001" not in [row["id"] for row in current_page["items"]]
    assert current_page["items"][0]["id"] == "itm-0901"

def test_unknown_snapshot_token_is_a_query_error() -> None:
    universe = Universe(make_records(3))
    with pytest.raises(QueryError) as caught:
        universe.snapshot("snap-9999")
    assert caught.value.code == "SNAPSHOT_TOKEN_INVALID"

def test_insert_delete_update_maintain_version_and_order() -> None:
    universe = Universe(make_records(4))
    universe.apply_insert(
        natural_key="itm-0901",
        sort_key=14,
        title="x",
        category="inserted",
        payload_sha8="12345678",
    )
    universe.apply_update_sortkey("itm-0002", 50)
    universe.apply_delete("itm-0001")
    ids = [row.id for row in universe.current_records()]
    assert ids[-1] == "itm-0002"
    assert "itm-0001" not in ids
    assert universe.version == 3
    assert universe.meta()["count"] == 4
    assert universe.meta()["max_sort_key"] == 50

def test_page_is_json_serializable_and_stably_ordered() -> None:
    universe = Universe(make_records(5))
    page = universe.page_of(universe.current_records(), mode="offset", offset=0, limit=2)
    encoded = json.dumps(page, sort_keys=True).encode("utf-8")
    assert b"itm-0001" in encoded
    assert page["items"][0]["sort_key"] < page["items"][1]["sort_key"]
