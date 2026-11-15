"""Typed records shared by the mock source, collectors and audit engine."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
RECORD_FIELDS = ("id", "sort_key", "title", "category", "payload_sha8")
FINDING_CODES = (
    "DRIFT_SKIP",
    "DRIFT_DUP",
    "DRIFT_OOR",
    "DRIFT_PREMATURE_TERM",
    "DRIFT_TAIL_TRUNC",
    "DRIFT_INSERT_OBSERVED",
)
TERMINATION_CONDITIONS = (
    "saw_empty_page",
    "partial_page_lt_limit",
    "exhausted_by_max_key",
)

@dataclass(frozen=True)
class Record:
    id: str
    sort_key: int
    title: str
    category: str
    payload_sha8: str
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sort_key": self.sort_key,
            "title": self.title,
            "category": self.category,
            "payload_sha8": self.payload_sha8,
        }
    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Record":
        return cls(
            id=str(row["id"]),
            sort_key=int(row["sort_key"]),
            title=str(row["title"]),
            category=str(row["category"]),
            payload_sha8=str(row["payload_sha8"]),
        )

@dataclass
class PageEvidence:
    seq: int
    path: str
    sha256: str
    size: int

@dataclass
class TerminationObservation:
    last_page_empty: bool = False
    last_page_short: bool = False
    keyset_reached_max: bool = False
    stopped_at_max_pages: bool = False
    pages_fetched: int = 0
    last_page_seq: int = 0
    last_page_returned: int = 0
    last_key: int | None = None
    failed: bool = False
    def to_dict(self) -> dict[str, Any]:
        return {
            "last_page_empty": self.last_page_empty,
            "last_page_short": self.last_page_short,
            "keyset_reached_max": self.keyset_reached_max,
            "stopped_at_max_pages": self.stopped_at_max_pages,
            "pages_fetched": self.pages_fetched,
            "last_page_seq": self.last_page_seq,
            "last_page_returned": self.last_page_returned,
            "last_key": self.last_key,
            "failed": self.failed,
        }

@dataclass
class Finding:
    code: str
    natural_key: str
    page_seq: int
    evidence_sha8: str
    detail: str
    is_defect: bool = True
    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "natural_key": self.natural_key,
            "page_seq": self.page_seq,
            "evidence_sha8": self.evidence_sha8,
            "detail": self.detail,
            "is_defect": self.is_defect,
        }

@dataclass
class StrategySummary:
    strategy: str
    coverage_ratio: float
    dup_ratio: float
    duplicate_count: int
    skip_count: int
    max_observed_key: int | None
    universe_max_key: int | None
    findings_by_code: dict[str, int]
    termination_satisfied: bool
    verdict: str
    skips_per_100_pages: float
    records_observed: int
    unique_observed: int
    pages_fetched: int
    integrity_ok: bool = True
    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "coverage_ratio": self.coverage_ratio,
            "dup_ratio": self.dup_ratio,
            "duplicate_count": self.duplicate_count,
            "skip_count": self.skip_count,
            "max_observed_key": self.max_observed_key,
            "universe_max_key": self.universe_max_key,
            "findings_by_code": dict(self.findings_by_code),
            "termination_satisfied": self.termination_satisfied,
            "verdict": self.verdict,
            "skips_per_100_pages": self.skips_per_100_pages,
            "records_observed": self.records_observed,
            "unique_observed": self.unique_observed,
            "pages_fetched": self.pages_fetched,
            "integrity_ok": self.integrity_ok,
        }

@dataclass
class RequestLog:
    seq: int
    attempt: int
    path: str
    query: dict[str, Any]
    status: int
    returned: int
    first_key: int | None
    last_key: int | None
    sha256: str
    evidence_file: str
    elapsed_ms: int
    valid: bool
    error_code: str | None = None
    def to_dict(self) -> dict[str, Any]:
        out = {
            "seq": self.seq,
            "attempt": self.attempt,
            "path": self.path,
            "query": dict(self.query),
            "status": self.status,
            "returned": self.returned,
            "first_key": self.first_key,
            "last_key": self.last_key,
            "sha256": self.sha256,
            "evidence_file": self.evidence_file,
            "elapsed_ms": self.elapsed_ms,
            "valid": self.valid,
        }
        if self.error_code:
            out["error_code"] = self.error_code
        return out

@dataclass
class TransportResponse:
    status: int
    body: bytes
    elapsed_ms: int = 0

@dataclass
class CollectionContext:
    out_dir: Path
    plan: Any
    strategy: str
    initial_records: list[Record]
    transport: Any
    schedule_sha256: str
    frozen_time: str
    seed: int
    source_meta: dict[str, Any]
    transport_kind: str = "inprocess"
    run_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
