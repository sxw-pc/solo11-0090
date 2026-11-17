"""In-memory mutable ordered list and four pagination query semantics."""
from __future__ import annotations
from typing import Any, Iterable
from ..ids import canonical_json
from ..models import Record

class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

class Universe:
    def __init__(self, records: Iterable[Record], universe_id: str = "uni-demo"):
        self.universe_id = universe_id
        source = list(records)
        self._records: dict[str, Record] = {record.id: record for record in source}
        if len(self._records) != len(source):
            raise ValueError("duplicate natural keys in universe")
        self._initial_ids = frozenset(self._records)
        self.version = 0
        self._snapshots: dict[str, list[Record]] = {}
        self._snapshot_counter = 0
    @property
    def initial_ids(self) -> frozenset[str]:
        return self._initial_ids
    def current_records(self) -> list[Record]:
        return self._ordered(self._records.values())
    def initial_records(self) -> list[Record]:
        return self._ordered(record for record in self._records.values() if record.id in self._initial_ids)
    @staticmethod
    def _ordered(records: Iterable[Record]) -> list[Record]:
        return sorted(records, key=lambda record: (record.sort_key, record.id))
    def get(self, natural_key: str) -> Record | None:
        return self._records.get(natural_key)
    def apply_insert(
        self,
        *,
        natural_key: str,
        sort_key: int,
        title: str,
        category: str,
        payload_sha8: str,
    ) -> dict[str, Any]:
        if natural_key in self._records:
            raise ValueError(f"insert duplicate id: {natural_key}")
        record = Record(
            id=natural_key,
            sort_key=int(sort_key),
            title=title,
            category=category,
            payload_sha8=payload_sha8,
        )
        self._records[natural_key] = record
        self.version += 1
        return {"before": None, "after": record.to_dict()}
    def apply_delete(self, natural_key: str) -> dict[str, Any]:
        record = self._records.get(natural_key)
        if record is None:
            raise KeyError(natural_key)
        before = record.to_dict()
        del self._records[natural_key]
        self.version += 1
        return {"before": before, "after": None}
    def apply_update_sortkey(self, natural_key: str, sort_key: int) -> dict[str, Any]:
        record = self._records.get(natural_key)
        if record is None:
            raise KeyError(natural_key)
        before = record.to_dict()
        updated = Record(
            id=record.id,
            sort_key=int(sort_key),
            title=record.title,
            category=record.category,
            payload_sha8=record.payload_sha8,
        )
        self._records[natural_key] = updated
        self.version += 1
        return {"before": before, "after": updated.to_dict()}
    def create_snapshot(self) -> tuple[str, list[Record]]:
        self._snapshot_counter += 1
        token = f"snap-{self._snapshot_counter:04d}"
        records = [record for record in self.current_records()]
        self._snapshots[token] = records
        return token, records
    def snapshot(self, token: str) -> list[Record]:
        if token not in self._snapshots:
            raise QueryError("SNAPSHOT_TOKEN_INVALID", f"unknown snapshot token {token!r}")
        return self._snapshots[token]
    def meta(self) -> dict[str, Any]:
        current = self.current_records()
        return {
            "universe_id": self.universe_id,
            "version": self.version,
            "count": len(current),
            "initial_count": len(self._initial_ids),
            "min_sort_key": current[0].sort_key if current else None,
            "max_sort_key": current[-1].sort_key if current else None,
            "snapshot_count": len(self._snapshots),
        }
    def page_of(
        self,
        records: list[Record],
        *,
        mode: str,
        offset: int,
        limit: int,
        after_key: int | None = None,
        snapshot_token: str | None = None,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise QueryError("BAD_REQUEST", "limit must be positive")
        if offset < 0:
            raise QueryError("BAD_REQUEST", "offset must be non-negative")
        ordered = self._ordered(records)
        if mode == "keyset":
            if after_key is not None:
                ordered = [record for record in ordered if record.sort_key > after_key]
            selected = ordered[:limit]
        else:
            selected = ordered[offset : offset + limit]
        page: dict[str, Any] = {
            "mode": mode,
            "offset": offset,
            "limit": limit,
            "returned": len(selected),
            "next_offset": offset + len(selected),
        }
        if mode == "keyset":
            page["after_key"] = after_key
        if mode == "snapshot":
            page["snapshot_token"] = snapshot_token
        return {
            "items": [record.to_dict() for record in selected],
            "page": page,
            "universe": {
                "version": self.version,
                "total": len(ordered),
                "max_sort_key": ordered[-1].sort_key if ordered else None,
            },
        }

class MockCore:
    """Shared request handler used by both in-process and HTTP transports."""
    def __init__(self, universe: Universe, schedule: Any, max_page_size: int = 500):
        self.universe = universe
        self.schedule = schedule
        self.max_page_size = max_page_size
        self.request_seq = 0
        self.mutation_log: list[dict[str, Any]] = []
        self.audit_events: list[dict[str, Any]] = []
    def add_audit_event(self, event: dict[str, Any]) -> None:
        self.audit_events.append(event)
    def meta_bytes(self) -> bytes:
        return canonical_json(self.universe.meta()) + b"\n"
    def _limit(self, raw: Any, field: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise QueryError("BAD_REQUEST", f"{field} must be an integer") from exc
        if value < 1 or value > self.max_page_size:
            raise QueryError("BAD_REQUEST", f"{field} must be 1..{self.max_page_size}")
        return value
    def _int_value(self, raw: Any, field: str, minimum: int = 0) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise QueryError("BAD_REQUEST", f"{field} must be an integer") from exc
        if value < minimum:
            raise QueryError("BAD_REQUEST", f"{field} must be >= {minimum}")
        return value
    def _apply_schedule(self) -> None:
        self.request_seq += 1
        entries = self.schedule.apply(self.request_seq, self.universe)
        self.mutation_log.extend(entries)
    def handle_items(self, query: dict[str, Any]) -> tuple[int, bytes]:
        try:
            self._apply_schedule()
            mode, offset, limit, after_key, create_snapshot, token = self._parse_query(query)
            records = self.universe.current_records()
            snapshot_token = None
            if mode == "snapshot":
                if create_snapshot:
                    snapshot_token, records = self.universe.create_snapshot()
                else:
                    snapshot_token = token
                    records = self.universe.snapshot(token)
            payload = self.universe.page_of(
                records,
                mode=mode,
                offset=offset,
                limit=limit,
                after_key=after_key,
                snapshot_token=snapshot_token,
            )
            return 200, canonical_json(payload) + b"\n"
        except QueryError as exc:
            return 400, canonical_json({"error": {"code": exc.code, "message": exc.message}}) + b"\n"
        except (KeyError, ValueError) as exc:
            return 500, canonical_json({"error": {"code": "MOCK_ERROR", "message": str(exc)}}) + b"\n"
    def _parse_query(self, query: dict[str, Any]) -> tuple[str, int, int, int | None, bool, str | None]:
        snapshot_raw = query.get("snapshot")
        after_raw = query.get("after_key")
        page_raw = query.get("page")
        offset_raw = query.get("offset")
        if snapshot_raw is not None:
            limit = self._limit(query.get("limit"), "limit")
            offset = self._int_value(query.get("offset", 0), "offset")
            if str(snapshot_raw) == "1":
                return "snapshot", offset, limit, None, True, None
            if not str(snapshot_raw):
                raise QueryError("BAD_REQUEST", "snapshot token must not be empty")
            return "snapshot", offset, limit, None, False, str(snapshot_raw)
        if after_raw is not None:
            limit = self._limit(query.get("limit"), "limit")
            after_key = None if str(after_raw) == "" else self._int_value(after_raw, "after_key")
            return "keyset", 0, limit, after_key, False, None
        if page_raw is not None:
            size = self._limit(query.get("page_size"), "page_size")
            page = self._int_value(page_raw, "page", minimum=1)
            return "pagenum", (page - 1) * size, size, None, False, None
        if offset_raw is not None:
            limit = self._limit(query.get("limit"), "limit")
            offset = self._int_value(offset_raw, "offset")
            return "offset", offset, limit, None, False, None
        raise QueryError("BAD_REQUEST", "one pagination mode is required")
