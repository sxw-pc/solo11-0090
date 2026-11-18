"""Command line entry points for the offline verification bench."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Sequence
from .audit.compare import build_comparison, write_comparison
from .audit.engine import AuditError, audit_run, write_audit_reports
from .config import PlanError, load_plan, plan_error_lines
from .election_evidence import ElectionEvidenceError, inspect_election_run
from .evidence import verify_run
from .pipeline import DEFAULT_FROZEN_TIME, PipelineError, collect_single, run_demo
from .seat_settlement import (
    SeatSettlementError,
    settle_election,
    settle_fixed_house,
    settle_overhang_expansion,
    verify_settlement_receipt,
    write_receipt_bundle,
)
from .store.sqlite_ledger import Ledger

def _add_transport_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transport", choices=("inprocess", "http"), default="inprocess")
    parser.add_argument("--port", type=int, default=None, help="override mock HTTP port; 0 chooses a free port")
    parser.add_argument("--frozen-time", default=DEFAULT_FROZEN_TIME, help="fixed timestamp for reproducible output")
    parser.add_argument("--seed", type=int, default=None, help="override the plan's dataset seed")
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pagedrift", description="Deep-pagination drift verification bench")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="run four strategies, audit, compare, and write the ledger")
    demo.add_argument("--out", default="dist/demo")
    demo.add_argument("--plan", default=None)
    _add_transport_options(demo)
    collect = sub.add_parser("collect", help="collect and audit one strategy")
    collect.add_argument("--plan", required=True)
    collect.add_argument("--strategy", required=True, choices=("offset", "pagenum", "keyset", "snapshot"))
    collect.add_argument("--out", required=True)
    collect.add_argument("--ledger", default=None)
    _add_transport_options(collect)
    audit = sub.add_parser("audit", help="audit one existing run directory")
    audit.add_argument("--run-dir", required=True)
    audit.add_argument("--ledger", default=None)
    compare = sub.add_parser("compare", help="compare multiple run directories")
    compare.add_argument("run_dirs", nargs="+")
    compare.add_argument("--out", default=None)
    evidence = sub.add_parser("evidence", help="inspect page evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    verify = evidence_sub.add_parser("verify", help="recompute page sha256 values")
    verify.add_argument("run_dir")
    verify.add_argument("--ledger", default=None)
    ledger = sub.add_parser("ledger", help="inspect the SQLite ledger")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    show = ledger_sub.add_parser("show", help="show one run from the ledger")
    show.add_argument("--run", required=True)
    show.add_argument("--ledger", default="dist/demo/ledger.sqlite")
    election = sub.add_parser(
        "election",
        help="fictional election evidence and pre-fixed seat settlement contracts",
    )
    election_sub = election.add_subparsers(dest="election_command", required=True)
    inspect = election_sub.add_parser(
        "inspect", help="lock a fictional election fixture to a verified safe run"
    )
    inspect.add_argument("run_dir", help="verified safe pagination run directory")
    inspect.add_argument("fixture", help="fictional election contract JSON fixture")
    settle = election_sub.add_parser(
        "settle",
        help="settle both pre-fixed chamber contracts and write the receipt bundle",
    )
    settle.add_argument("run_dir", help="verified safe pagination run directory")
    settle.add_argument("fixture", help="fictional election contract JSON fixture")
    settle.add_argument("--out", required=True, help="receipt bundle output path")
    verify = election_sub.add_parser(
        "verify-receipt", help="replay a settlement receipt against the same evidence"
    )
    verify.add_argument("run_dir", help="verified safe pagination run directory")
    verify.add_argument("fixture", help="fictional election contract JSON fixture")
    verify.add_argument("--receipt", required=True, help="settlement receipt bundle path")
    return parser
def _cmd_demo(args: argparse.Namespace) -> int:
    result = run_demo(
        args.out,
        plan_path=args.plan,
        transport_kind=args.transport,
        port_override=args.port,
        frozen_time=args.frozen_time,
        seed_override=args.seed,
    )
    print("")
    print(f"overall_verdict: {result['overall_verdict']}")
    for row in result["runs"]:
        print(f"  {row['strategy']:<8} {row['verdict']:<12} coverage={row['coverage_ratio']} skips={row['skip_count']}")
    print(f"ledger: {result['ledger']}")
    print(f"reports: {Path(result['out']) / 'compare.json'}")
    return 0
def _cmd_collect(args: argparse.Namespace) -> int:
    result = collect_single(
        out=args.out,
        plan_path=args.plan,
        strategy=args.strategy,
        transport_kind=args.transport,
        port_override=args.port,
        frozen_time=args.frozen_time,
        seed_override=args.seed,
        ledger_path=args.ledger,
    )
    summary = result["audit"]["summary"]
    print(f"run_id={result['run']['run_id']} strategy={args.strategy} verdict={summary['verdict']}")
    print(f"run_dir={result['run_dir']}")
    return 0
def _cmd_audit(args: argparse.Namespace) -> int:
    audit = audit_run(args.run_dir)
    write_audit_reports(audit, args.run_dir)
    if args.ledger:
        run = json.loads((Path(args.run_dir) / "run.json").read_text(encoding="utf-8"))
        with Ledger(args.ledger) as ledger:
            ledger.record_run(run, args.run_dir)
            ledger.record_audit_file(audit, args.run_dir)
    summary = audit["summary"]
    print(
        f"run_id={audit['run_id']} strategy={audit['strategy']} verdict={summary['verdict']} "
        f"findings={len(audit['findings'])} integrity={len(audit['integrity_findings'])}"
    )
    return 0
def _cmd_compare(args: argparse.Namespace) -> int:
    result = build_comparison(args.run_dirs)
    out_dir = Path(args.out) if args.out else Path(args.run_dirs[0]).resolve().parent
    paths = write_comparison(result, out_dir)
    print(f"overall_verdict={result['overall_verdict']}")
    for row in result["rows"]:
        print(
            f"  {row['strategy']:<8} {row['verdict']:<12} coverage={row['coverage']} "
            f"dup={row['dup_ratio']} skip={row['skip_count']}"
        )
    for message in result["messages"]:
        print(f"note: {message}")
    print(f"wrote {paths['json']}")
    return 0
def _cmd_evidence_verify(args: argparse.Namespace) -> int:
    ledger_path = args.ledger
    if ledger_path is None:
        sibling = Path(args.run_dir).resolve().parent / "ledger.sqlite"
        ledger_path = str(sibling) if sibling.is_file() else None
    result = verify_run(args.run_dir, ledger_path)
    for issue in result["issues"]:
        code = issue.get("code")
        seq = issue.get("seq", "-")
        detail = issue.get("detail", "")
        print(f"{code} seq={seq} {detail}")
    if result["status"] == "OK":
        print(
            f"OK checked_pages={result['checked_pages']} requests={result['requests']} "
            f"ledger_pages={result['ledger_pages']}"
        )
        return 0
    print(f"MISMATCH issues={len(result['issues'])}")
    return 1
def _cmd_ledger_show(args: argparse.Namespace) -> int:
    with Ledger(args.ledger) as ledger:
        payload = ledger.show(args.run)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_election_inspect(args: argparse.Namespace) -> int:
    evidence = inspect_election_run(args.run_dir, args.fixture)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


def _cmd_election_settle(args: argparse.Namespace) -> int:
    evidence = inspect_election_run(args.run_dir, args.fixture)
    bundle = settle_election(evidence)
    write_receipt_bundle(args.out, bundle)
    for name in ("fixed_house", "overhang_expansion"):
        receipt = bundle[name]
        majority = receipt["majority"]
        print(
            f"{name}: house_size={receipt['majority']['house_size']} "
            f"seats={receipt['final_seats']} "
            f"single_party_majority={majority['single_party_majority']} "
            f"receipt_sha256={receipt['receipt_sha256'][:12]}"
        )
    print(f"wrote {args.out}")
    return 0


def _cmd_election_verify_receipt(args: argparse.Namespace) -> int:
    evidence = inspect_election_run(args.run_dir, args.fixture)
    bundle = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    for name in ("fixed_house", "overhang_expansion"):
        result = verify_settlement_receipt(bundle[name], evidence)
        print(f"{name}: {result['status']} receipt_sha256={result['receipt_sha256']}")
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return _cmd_demo(args)
        if args.command == "collect":
            return _cmd_collect(args)
        if args.command == "audit":
            return _cmd_audit(args)
        if args.command == "compare":
            return _cmd_compare(args)
        if args.command == "evidence" and args.evidence_command == "verify":
            return _cmd_evidence_verify(args)
        if args.command == "ledger" and args.ledger_command == "show":
            return _cmd_ledger_show(args)
        if args.command == "election":
            if args.election_command == "inspect":
                return _cmd_election_inspect(args)
            if args.election_command == "settle":
                return _cmd_election_settle(args)
            if args.election_command == "verify-receipt":
                return _cmd_election_verify_receipt(args)
        parser.error("unknown command")
    except PlanError as exc:
        for line in plan_error_lines(exc):
            print(line, file=sys.stderr)
        return 2
    except (
        PipelineError,
        AuditError,
        ElectionEvidenceError,
        SeatSettlementError,
        FileNotFoundError,
        KeyError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
