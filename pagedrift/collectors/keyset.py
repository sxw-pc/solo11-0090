"""Keyset/seek collection using the last observed sort key as cursor."""
from __future__ import annotations
from typing import Any
from .base import BaseCollector, register

@register
class KeysetCollector(BaseCollector):
    strategy = "keyset"
    def initial_state(self) -> dict[str, Any]:
        return {"after_key": None}
    def build_query(self, state: dict[str, Any]) -> dict[str, Any]:
        after_key = state["after_key"]
        return {
            "after_key": "" if after_key is None else int(after_key),
            "limit": self.limit,
        }
    def advance(self, state: dict[str, Any], page: dict[str, Any], records: list[dict[str, Any]]) -> None:
        if records:
            state["after_key"] = int(records[-1]["sort_key"])
