"""Shared collection loop, request ledger and page contract validation."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from ..evidence import EvidenceStore
from ..fixtures import write_universe as write_universe_file
from ..ids import canonical_json, sha256_bytes, stable_run_id, universe_fingerprint
from ..models import CollectionContext, Record, RequestLog, TerminationObservation
from ..transport import TransportError
REQUIRED_ITEM_FIELDS = ("id", "sort_key", "title", "category", "payload_sha8")

class CollectorError(RuntimeError):
    pass

class PageContractError(ValueError):
    def __init__(self, message: str, error_code: str = "RESP_INVALID"):
        self.error_code = error_code
        super().__init__(message)

def validate_page(raw: bytes, status: int, snapshot_mode: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if status != 200:
        code = "RESP_INVALID"
        try:
            err = json.loads(raw.decode("utf-8"))
            code = str(err.get("error", {}).get("code", code))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise PageContractError(f"HTTP status {status}", code)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PageContractError(f"response is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise PageContractError("response must contain an items list")
    page = payload.get("page")
    if not isinstance(page, dict):
        raise PageContractError("response must contain a page mapping")
    items = payload["items"]
    if "returned" not in page or not isinstance(page["returned"], int) or page["returned"] != len(items):
        raise PageContractError("page.returned must equal the number of items")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PageContractError(f"item #{index} is not an object")
        missing = [field for field in REQUIRED_ITEM_FIELDS if field not in item]
        if missing:
            raise PageContractError(f"item #{index} missing {missing}")
        if not isinstance(item["id"], str) or not item["id"]:
            raise PageContractError(f"item #{index} id must be a non-empty string")
        if not isinstance(item["sort_key"], int) or isinstance(item["sort_key"], bool):
            raise PageContractError(f"item #{index} sort_key must be an integer")
        normalized.append({field: item[field] for field in REQUIRED_ITEM_FIELDS})
    if snapshot_mode:
        token = page.get("snapshot_token")
        if not isinstance(token, str) or not token:
            raise PageContractError("snapshot page requires page.snapshot_token")
    return payload, normalized

class BaseCollector:
    strategy = ""
    def __init__(self, params: dict[str, Any]):
        self.params = dict(params)
        self.limit = int(params.get("page_size", params.get("limit", 0)))
        if self.limit <= 0:
            raise CollectorError("collector requires a positive limit/page_size")
        self.max_pages = int(params.get("max_pages", 10))
        self.state: dict[str, Any] = {}
    def initial_state(self) -> dict[str, Any]:
        return {}
    def build_query(self, state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
    def advance(self, state: dict[str, Any], page: dict[str, Any], records: list[dict[str, Any]]) -> None:
        raise NotImplementedError
    def snapshot_mode(self) -> bool:
        return self.strategy == "snapshot"
    def collect(self, ctx: CollectionContext) -> Path:
        out_dir = Path(ctx.out_dir)
        if out_dir.exists() and any(out_dir.iterdir()):
            raise CollectorError(f"run directory is not empty: {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
        write_universe_file(out_dir / "initial_universe.jsonl", ctx.initial_records)
        evidence = EvidenceStore(out_dir)
        self.state = self.initial_state()
        requests: list[RequestLog] = []
        records_out: list[dict[str, Any]] = []
        snapshot_tokens: list[str | None] = []
        observation = TerminationObservation()
        status = "completed"
        pages_ok = 0
        request_seq = 0
        max_attempts = self.max_pages * 2 + 4
        while pages_ok < self.max_pages and request_seq < max_attempts:
            query = self.build_query(self.state)
            page_accepted = False
            page_payload: dict[str, Any] | None = None
            page_records: list[dict[str, Any]] = []
            for attempt in (1, 2):
                try:
                    response = ctx.transport.fetch(query)
                except TransportError as exc:
                    status = "failed"
                    observation.failed = True
                    observation.error = str(exc)  # type: ignore[attr-defined]
                    break
                request_seq += 1
                evidence_page = evidence.write_page(request_seq, response.body)
                parsed: dict[str, Any] | None = None
                normalized: list[dict[str, Any]] = []
                error_code: str | None = None
                try:
                    parsed, normalized = validate_page(
                        response.body, response.status, self.snapshot_mode()
                    )
                except PageContractError as exc:
                    error_code = exc.error_code
                first_key = normalized[0]["sort_key"] if normalized else None
                last_key = normalized[-1]["sort_key"] if normalized else None
                elapsed = 0 if ctx.frozen_time else int(response.elapsed_ms)
                log = RequestLog(
                    seq=request_seq,
                    attempt=attempt,
                    path=str(ctx.plan.source.get("items_path", "/items")),
                    query=dict(query),
                    status=response.status,
                    returned=len(normalized) if parsed else 0,
                    first_key=first_key,
                    last_key=last_key,
                    sha256=evidence_page.sha256,
                    evidence_file=evidence_page.path,
                    elapsed_ms=elapsed,
                    valid=error_code is None,
                    error_code=error_code,
                )
                requests.append(log)
                if error_code is None:
                    page_accepted = True
                    page_payload = parsed
                    page_records = normalized
                    break
            if status == "failed":
                break
            if not page_accepted or page_payload is None:
                status = "failed"
                observation.failed = True
                break
            page = page_payload.get("page", {})
            if self.snapshot_mode():
                snapshot_tokens.append(page.get("snapshot_token"))
            for row in page_records:
                records_out.append({**row, "source_page_seq": request_seq})
            returned = len(page_records)
            last_key = page_records[-1]["sort_key"] if page_records else None
            pages_ok += 1
            observation.pages_fetched = pages_ok
            observation.last_page_seq = request_seq
            observation.last_page_returned = returned
            observation.last_key = last_key
            observation.last_page_empty = returned == 0
            observation.last_page_short = 0 < returned < self.limit
            self.advance(self.state, page, page_records)
            if returned == 0:
                break
            if returned < self.limit:
                break
            universe_max = ctx.source_meta.get("max_sort_key")
            if last_key is not None and universe_max is not None and last_key >= int(universe_max):
                observation.keyset_reached_max = True
                break
            if pages_ok >= self.max_pages:
                observation.stopped_at_max_pages = True
                break
        if not observation.failed and pages_ok >= self.max_pages and not (
            observation.last_page_empty
            or observation.last_page_short
            or observation.keyset_reached_max
        ):
            observation.stopped_at_max_pages = True
        self._write_records(out_dir / "records.jsonl", records_out)
        self._write_requests(out_dir / "requests.jsonl", requests)
        run_id = ctx.run_id or stable_run_id(
            strategy=self.strategy,
            plan_sha256=ctx.plan.sha256,
            universe_sha256=universe_fingerprint(ctx.initial_records),
            schedule_sha256=ctx.schedule_sha256,
            strategy_input_sha256=ctx.plan.strategy_input_sha(self.strategy),
            frozen_time=ctx.frozen_time,
            seed=ctx.seed,
        )
        run = {
            "schema_version": 1,
            "run_id": run_id,
            "strategy": self.strategy,
            "status": status,
            "created_at": ctx.frozen_time,
            "plan_sha256": ctx.plan.sha256,
            "schedule_sha256": ctx.schedule_sha256,
            "universe_sha256": universe_fingerprint(ctx.initial_records),
            "strategy_input_sha256": ctx.plan.strategy_input_sha(self.strategy),
            "strategy_config": ctx.plan.strategy_input(self.strategy),
            "seed": ctx.seed,
            "transport": ctx.transport_kind,
            "initial_universe_file": "initial_universe.jsonl",
            "initial_count": len(ctx.initial_records),
            "universe_max_key": ctx.source_meta.get("max_sort_key"),
            "source_meta": dict(ctx.source_meta),
            "termination_observation": observation.to_dict(),
            "snapshot_tokens": snapshot_tokens,
            "pages_fetched": pages_ok,
            "requests_count": len(requests),
            "plan": ctx.plan.public_dict(),
        }
        (out_dir / "run.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        return out_dir
    @staticmethod
    def _write_requests(path: Path, rows: list[RequestLog]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(canonical_json(row.to_dict()).decode("utf-8") + "\n")
    @staticmethod
    def _write_records(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(canonical_json(row).decode("utf-8") + "\n")
_COLLECTORS: dict[str, type[BaseCollector]] = {}

def register(cls: type[BaseCollector]) -> type[BaseCollector]:
    _COLLECTORS[cls.strategy] = cls
    return cls

def get_collector(strategy: str, params: dict[str, Any]) -> BaseCollector:
    if strategy not in _COLLECTORS:
        from . import keyset, offset, pagenum, snapshot  # noqa: F401
    if strategy not in _COLLECTORS:
        raise CollectorError(f"unknown collector strategy: {strategy}")
    return _COLLECTORS[strategy](params)

def collect_strategy(ctx: CollectionContext) -> Path:
    return get_collector(ctx.strategy, ctx.plan.strategy_input(ctx.strategy)).collect(ctx)
