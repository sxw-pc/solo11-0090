"""Shared test builders; every helper stays fully offline and deterministic."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
import yaml
from pagedrift.audit.engine import audit_run
from pagedrift.collectors.base import collect_strategy
from pagedrift.config import Plan, load_plan
from pagedrift.fixtures import generate_universe
from pagedrift.ids import canonical_json
from pagedrift.mock import MockCore, MutationSchedule, Universe
from pagedrift.models import CollectionContext
from pagedrift.transport import InProcessTransport
EMPTY_SCHEDULE = "version: 1\nbefore_request: []\n"

def write_schedule(tmp_path: Path, text: str = EMPTY_SCHEDULE, name: str = "schedule") -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path
def make_plan(
    tmp_path: Path,
    *,
    schedule_text: str = EMPTY_SCHEDULE,
    name: str = "plan",
    limit: int = 3,
    max_pages: int = 10,
    audit: dict[str, Any] | None = None,
    source_extra: dict[str, Any] | None = None,
    dataset: dict[str, Any] | None = None,
) -> Plan:
    schedule_path = write_schedule(tmp_path, schedule_text, name=f"{name}-schedule")
    source = {
        "base_url": "http://127.0.0.1:8765",
        "items_path": "/items",
        "meta_path": "/meta",
        "sort_key": "sort_key",
    }
    if source_extra:
        source.update(source_extra)
    data = {
        "version": 1,
        "dataset": dataset
        or {"name": f"test-{name}", "size": 12, "seed": 5, "start_key": 10, "key_step": 3},
        "source": source,
        "strategies": {
            "offset": {"limit": limit, "max_pages": max_pages},
            "pagenum": {"page_size": limit, "max_pages": max_pages},
            "keyset": {"limit": limit, "max_pages": max_pages},
            "snapshot": {"limit": limit, "max_pages": max_pages},
        },
        "mutation_schedule": schedule_path.name,
        "termination": {
            "allow": ["saw_empty_page", "partial_page_lt_limit", "exhausted_by_max_key"]
        },
        "audit": audit
        or {"max_dup_ratio": 0.0, "allow_out_of_order": False, "allow_insert_extra": True},
    }
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_plan(path)
def make_records(
    size: int = 12,
    *,
    seed: int = 5,
    start_key: int = 10,
    key_step: int = 3,
):
    return generate_universe(size=size, seed=seed, start_key=start_key, key_step=key_step, dataset_name="helper")
def build_context(plan: Plan, records: list, run_dir: Path) -> tuple[CollectionContext, MockCore]:
    schedule = MutationSchedule.load(plan.schedule_path)
    universe = Universe(records, universe_id=f"uni-{plan.strategy_input('offset')}")
    core = MockCore(universe, schedule)
    transport = InProcessTransport(core)
    source_meta = transport.meta()
    ctx = CollectionContext(
        out_dir=run_dir,
        plan=plan,
        strategy="",
        initial_records=records,
        transport=transport,
        schedule_sha256=plan.schedule_sha256,
        frozen_time="2024-01-01T00:00:00Z",
        seed=int(plan.dataset.get("seed", 0)),
        source_meta=source_meta,
        transport_kind="inprocess",
    )
    return ctx, core
def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(canonical_json(row).decode("utf-8") + "\n")
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows
def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
def execute_strategy(
    plan: Plan,
    strategy: str,
    records: list,
    base_dir: Path,
    *,
    run_name: str | None = None,
) -> dict[str, Any]:
    run_dir = base_dir / (run_name or strategy)
    ctx, core = build_context(plan, records, run_dir)
    ctx.strategy = strategy
    collect_strategy(ctx)
    write_jsonl(run_dir / "mutation_log.jsonl", core.mutation_log)
    write_jsonl(run_dir / "transport_audit.jsonl", core.audit_events)
    audit = audit_run(run_dir)
    return {
        "run_dir": run_dir,
        "run": read_json(run_dir / "run.json"),
        "audit": audit,
        "core": core,
    }
