"""Classic offset/limit collection."""
from __future__ import annotations
from typing import Any
from .base import BaseCollector, register

@register
class OffsetCollector(BaseCollector):
    strategy = "offset"
    def initial_state(self) -> dict[str, Any]:
        return {"offset": 0}
    def build_query(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"offset": int(state["offset"]), "limit": self.limit}
    def advance(self, state: dict[str, Any], page: dict[str, Any], records: list[dict[str, Any]]) -> None:
        state["offset"] = int(state["offset"]) + self.limit
