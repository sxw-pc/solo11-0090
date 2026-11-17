"""Snapshot-token collection: the safe baseline strategy."""
from __future__ import annotations
from typing import Any
from .base import BaseCollector, register

@register
class SnapshotCollector(BaseCollector):
    strategy = "snapshot"
    def initial_state(self) -> dict[str, Any]:
        return {"offset": 0, "token": None}
    def build_query(self, state: dict[str, Any]) -> dict[str, Any]:
        token = state["token"]
        return {
            "snapshot": "1" if token is None else str(token),
            "offset": int(state["offset"]),
            "limit": self.limit,
        }
    def advance(self, state: dict[str, Any], page: dict[str, Any], records: list[dict[str, Any]]) -> None:
        state["offset"] = int(state["offset"]) + self.limit
        state["token"] = page.get("snapshot_token", state["token"])
