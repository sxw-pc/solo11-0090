"""Page-number/page-size collection."""
from __future__ import annotations
from typing import Any
from .base import BaseCollector, register

@register
class PageNumberCollector(BaseCollector):
    strategy = "pagenum"
    def initial_state(self) -> dict[str, Any]:
        return {"page": 1}
    def build_query(self, state: dict[str, Any]) -> dict[str, Any]:
        return {"page": int(state["page"]), "page_size": self.limit}
    def advance(self, state: dict[str, Any], page: dict[str, Any], records: list[dict[str, Any]]) -> None:
        state["page"] = int(state["page"]) + 1
