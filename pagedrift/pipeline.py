"""End-to-end demo and single-strategy collection orchestration."""
from __future__ import annotations
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from .audit.compare import build_comparison, write_comparison
from .audit.engine import audit_run
from .collectors.base import collect_strategy
from .config import Plan, load_plan
from .fixtures import dataset_from_plan, write_universe
from .ids import canonical_json, sha256_bytes, universe_fingerprint
from .mock import MockCore, MutationSchedule, Universe, start_mock_server, stop_mock_server
from .models import CollectionContext
from .store.sqlite_ledger import Ledger
from .transport import HttpTransport, InProcessTransport
DEFAULT_FROZEN_TIME = "2024-01-01T00:00:00Z"
STRATEGY_ORDER = ("offset", "pagenum", "keyset", "snapshot")
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PLAN = PACKAGE_DIR / "plans" / "default.yaml"

class PipelineError(RuntimeError):
    pass
def _ordered_strategies(plan: Plan) -> list[str]:
    selected = list(plan.strategies)
    return sorted(selected, key=lambda name: (STRATEGY_ORDER.index(name) if name in STRATEGY_ORDER else 99, name))
def _assert_empty_directory(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise PipelineError(f"output path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise PipelineError(f"output directory is not empty; evidence is append-only: {path}")
def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(canonical_json(row).decode("utf-8") + "\n")
def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
def _start_transport(
    plan: Plan,
    strategy: str,
    initial_records: list,
    transport_kind: str,
    port_override: int | None,
) -> tuple[Any, MockCore, Any, list[str]]:
    schedule = MutationSchedule.load(plan.schedule_path)
    universe = Universe(initial_records, universe_id=f"uni-{strategy}")
    max_page_size = int(plan.source.get("max_page_size", 500))
    core = MockCore(universe, schedule, max_page_size=max_page_size)
    server = None
    thread = None
    notes: list[str] = []
    if transport_kind == "http":
        parts = urlsplit(plan.base_url)
        host = parts.hostname or "127.0.0.1"
        port = int(port_override if port_override is not None else (parts.port or 8765))
        try:
            server, thread = start_mock_server(core, host=host, port=port)
        except OSError as exc:
            raise PipelineError(f"could not bind mock server on {host}:{port}: {exc}") from exc
        actual = server.bind_description()
        notes.append(f"mock server bound to {actual}")
        transport = HttpTransport(
            actual,
            items_path=str(plan.source.get("items_path", "/items")),
            meta_path=str(plan.source.get("meta_path", "/meta")),
        )
    elif transport_kind == "inprocess":
        transport = InProcessTransport(core)
    else:
        raise PipelineError(f"unknown transport: {transport_kind}")
    return transport, core, (server, thread), notes

def run_one(
    *,
    plan: Plan,
    strategy: str,
    initial_records: list,
    out_dir: Path,
    transport_kind: str = "inprocess",
    frozen_time: str = DEFAULT_FROZEN_TIME,
    seed: int = 0,
    port_override: int | None = None,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise PipelineError(f"run directory is not empty: {out_dir}")
    transport, core, server_pair, notes = _start_transport(
        plan, strategy, initial_records, transport_kind, port_override
    )
    server, thread = server_pair
    try:
        source_meta = transport.meta()
        ctx = CollectionContext(
            out_dir=out_dir,
            plan=plan,
            strategy=strategy,
            initial_records=initial_records,
            transport=transport,
            schedule_sha256=plan.schedule_sha256,
            frozen_time=frozen_time,
            seed=seed,
            source_meta=source_meta,
            transport_kind=transport_kind,
        )
        run_dir = collect_strategy(ctx)
        _write_jsonl(run_dir / "mutation_log.jsonl", core.mutation_log)
        _write_jsonl(run_dir / "transport_audit.jsonl", core.audit_events)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        audit = audit_run(run_dir)
    finally:
        if server is not None and thread is not None:
            stop_mock_server(server, thread)
    return {
        "run_dir": run_dir,
        "run": run,
        "audit": audit,
        "mutation_log": core.mutation_log,
        "transport_notes": notes,
        "transport_audit_events": len(core.audit_events),
    }

def run_demo(
    out: str | Path,
    *,
    plan_path: str | Path | None = None,
    transport_kind: str = "inprocess",
    port_override: int | None = None,
    frozen_time: str = DEFAULT_FROZEN_TIME,
    seed_override: int | None = None,
) -> dict[str, Any]:
    plan = load_plan(plan_path or DEFAULT_PLAN)
    initial_records, dataset_info = dataset_from_plan(plan, seed_override=seed_override)
    seed = int(dataset_info.get("seed", 0))
    root = Path(out).resolve()
    _assert_empty_directory(root)
    write_universe(root / "initial_universe.jsonl", initial_records)
    shutil.copyfile(plan.path, root / "plan.yaml")
    shutil.copyfile(plan.schedule_path, root / "mutation_schedule.yaml")
    dataset_doc = {
        **dataset_info,
        "universe_sha256": universe_fingerprint(initial_records),
        "universe_max_key": max(record.sort_key for record in initial_records),
        "count": len(initial_records),
    }
    _write_json(root / "dataset.json", dataset_doc)
    results = []
    for strategy in _ordered_strategies(plan):
        result = run_one(
            plan=plan,
            strategy=strategy,
            initial_records=initial_records,
            out_dir=root / strategy,
            transport_kind=transport_kind,
            frozen_time=frozen_time,
            seed=seed,
            port_override=port_override,
        )
        results.append(result)
        print(
            f"[collect] {strategy}: verdict={result['audit']['summary']['verdict']} "
            f"pages={result['audit']['summary']['pages_fetched']} "
            f"skips={result['audit']['summary']['skip_count']} "
            f"dup_ratio={result['audit']['summary']['dup_ratio']}",
            flush=True,
        )
        for note in result["transport_notes"]:
            print(f"[{strategy}] {note}", flush=True)
    ledger_path = root / "ledger.sqlite"
    with Ledger(ledger_path) as ledger:
        for result in results:
            ledger.record_run(result["run"], result["run_dir"])
            ledger.record_audit_file(result["audit"], result["run_dir"])
    comparison = build_comparison([result["run_dir"] for result in results])
    write_comparison(comparison, root)
    summary = {
        "schema_version": 1,
        "created_at": frozen_time,
        "overall_verdict": comparison["overall_verdict"],
        "input_fingerprints": comparison["input_fingerprints"],
        "inputs_compatible": comparison["inputs_compatible"],
        "snapshot_baseline_defect": comparison["snapshot_baseline_defect"],
        "messages": comparison["messages"],
        "dataset": dataset_doc,
        "runs": [
            {
                "run_id": result["run"]["run_id"],
                "strategy": result["run"]["strategy"],
                "run_dir": result["run_dir"].name,
                "verdict": result["audit"]["summary"]["verdict"],
                "coverage_ratio": result["audit"]["summary"]["coverage_ratio"],
                "dup_ratio": result["audit"]["summary"]["dup_ratio"],
                "skip_count": result["audit"]["summary"]["skip_count"],
                "termination_satisfied": result["audit"]["summary"]["termination_satisfied"],
            }
            for result in results
        ],
    }
    _write_json(root / "demo.json", summary)
    return {
        **summary,
        "out": str(root),
        "ledger": str(ledger_path),
        "comparison": comparison,
        "run_paths": [str(result["run_dir"]) for result in results],
    }

def collect_single(
    *,
    out: str | Path,
    plan_path: str | Path,
    strategy: str,
    transport_kind: str = "inprocess",
    port_override: int | None = None,
    frozen_time: str = DEFAULT_FROZEN_TIME,
    seed_override: int | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    plan = load_plan(plan_path)
    if strategy not in plan.strategies:
        raise PipelineError(f"strategy {strategy!r} is not declared in the plan")
    initial_records, dataset_info = dataset_from_plan(plan, seed_override=seed_override)
    seed = int(dataset_info.get("seed", 0))
    root = Path(out).resolve()
    _assert_empty_directory(root)
    result = run_one(
        plan=plan,
        strategy=strategy,
        initial_records=initial_records,
        out_dir=root,
        transport_kind=transport_kind,
        frozen_time=frozen_time,
        seed=seed,
        port_override=port_override,
    )
    if ledger_path is not None:
        with Ledger(ledger_path) as ledger:
            ledger.record_run(result["run"], result["run_dir"])
            ledger.record_audit_file(result["audit"], result["run_dir"])
    return result
