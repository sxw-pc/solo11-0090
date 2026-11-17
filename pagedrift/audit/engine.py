"""Audit assembly: metrics, termination, verdicts and report files."""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path
from typing import Any
from ..fixtures import load_universe
from ..ids import sha256_bytes
from ..models import StrategySummary
from ..report.html import render_audit_html
from ..report.json_report import write_json
from ..report.markdown import render_audit_markdown
from .detect import AUDIT_CODE_ORDER, drift_findings, integrity_findings
from .extras import read_jsonl
from .termination import evaluate_termination

class AuditError(RuntimeError):
    pass
def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {path}: {exc}") from exc
def _verdict(
    *,
    integrity_ok: bool,
    proof_satisfied: bool,
    counts: Counter[str],
    dup_ratio: float,
    audit_cfg: dict[str, Any],
) -> str:
    if not integrity_ok:
        return "inconclusive"
    if not proof_satisfied:
        return "unsafe"
    if counts["DRIFT_SKIP"] or counts["DRIFT_PREMATURE_TERM"] or counts["DRIFT_TAIL_TRUNC"]:
        return "unsafe"
    if counts["DRIFT_OOR"] and not audit_cfg.get("allow_out_of_order", False):
        return "unsafe"
    if dup_ratio > float(audit_cfg.get("max_dup_ratio", 0.0)) + 1e-12:
        return "unsafe"
    return "safe"
def _evidence_rows(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "seq": int(row["seq"]),
            "evidence_file": row.get("evidence_file"),
            "sha256": row.get("sha256"),
            "status": row.get("status"),
            "valid": row.get("valid"),
            "returned": row.get("returned"),
            "first_key": row.get("first_key"),
            "last_key": row.get("last_key"),
        }
        for row in requests
    ]

def audit_run(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    run = _read_json(root / "run.json")
    plan = run.get("plan") or {}
    records = read_jsonl(root / "records.jsonl")
    requests = read_jsonl(root / "requests.jsonl")
    mutations = read_jsonl(root / "mutation_log.jsonl")
    initial = load_universe(root / str(run.get("initial_universe_file", "initial_universe.jsonl")))
    initial_records = [record.to_dict() for record in initial]
    proof = evaluate_termination(run, plan)
    integrity = integrity_findings(run, requests)
    findings, finding_dicts, meta = drift_findings(initial_records, records, requests, mutations, run)
    counts: Counter[str] = Counter(item.code for item in findings)
    for code in AUDIT_CODE_ORDER:
        counts[code] = int(counts.get(code, 0))
    counts["RESP_INVALID"] = len(integrity)
    duplicates = int(meta["duplicate_count"])
    dup_ratio = round(duplicates / max(1, len(records)), 6)
    initial_ids = {row["id"] for row in initial_records}
    observed_initial = len(initial_ids.intersection(meta["observed_ids"]))
    observed_extra_count = len(meta["extras"])
    allow_extra = bool((plan.get("audit") or {}).get("allow_insert_extra", True))
    included_extra = observed_extra_count if allow_extra else 0
    denominator = len(initial_ids) + included_extra
    numerator = observed_initial + included_extra
    coverage = round(numerator / max(1, denominator), 6)
    skip_count = counts["DRIFT_SKIP"]
    pages = int((run.get("termination_observation") or {}).get("pages_fetched", 0))
    skips_per_100 = round(skip_count * 100.0 / max(1, pages), 6)
    integrity_ok = not integrity
    verdict = _verdict(
        integrity_ok=integrity_ok,
        proof_satisfied=bool(proof["satisfied"]),
        counts=counts,
        dup_ratio=dup_ratio,
        audit_cfg=plan.get("audit") or {},
    )
    summary = StrategySummary(
        strategy=str(run.get("strategy", "")),
        coverage_ratio=coverage,
        dup_ratio=dup_ratio,
        duplicate_count=duplicates,
        skip_count=skip_count,
        max_observed_key=meta["max_observed"],
        universe_max_key=max((int(row["sort_key"]) for row in initial_records), default=None),
        findings_by_code={code: int(counts.get(code, 0)) for code in AUDIT_CODE_ORDER},
        termination_satisfied=bool(proof["satisfied"]),
        verdict=verdict,
        skips_per_100_pages=skips_per_100,
        records_observed=len(records),
        unique_observed=len(meta["observed_ids"]),
        pages_fetched=pages,
        integrity_ok=integrity_ok,
    )
    tokens = run.get("snapshot_tokens", [])
    checked = run.get("strategy") == "snapshot"
    consistent = True if not checked else bool(tokens) and len({token for token in tokens if token}) == 1
    audit = {
        "schema_version": 1,
        "run_id": run.get("run_id"),
        "strategy": run.get("strategy"),
        "generated_at": run.get("created_at"),
        "inputs": {
            "plan_sha256": run.get("plan_sha256"),
            "universe_sha256": run.get("universe_sha256"),
            "schedule_sha256": run.get("schedule_sha256"),
            "strategy_input_sha256": run.get("strategy_input_sha256"),
            "initial_universe_file": run.get("initial_universe_file"),
        },
        "summary": summary.to_dict(),
        "coverage": {
            "numerator": numerator,
            "denominator": denominator,
            "initial_count": len(initial_ids),
            "observed_initial": observed_initial,
            "included_extras": included_extra,
        },
        "termination_proof": proof,
        "snapshot_integrity": {
            "checked": checked,
            "tokens": tokens,
            "consistent": consistent,
        },
        "findings": finding_dicts,
        "integrity_findings": integrity,
        "evidence": _evidence_rows(requests),
    }
    audit["audit_fingerprint"] = sha256_bytes(
        json.dumps({**audit, "audit_fingerprint": ""}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    write_audit_reports(audit, root)
    return audit

def write_audit_reports(audit: dict[str, Any], run_dir: str | Path) -> dict[str, str]:
    root = Path(run_dir)
    json_path = root / "audit.json"
    md_path = root / "audit.md"
    html_path = root / "audit.html"
    write_json(json_path, audit)
    md_path.write_text(render_audit_markdown(audit), encoding="utf-8", newline="\n")
    html_path.write_text(render_audit_html(audit), encoding="utf-8", newline="\n")
    return {"json": str(json_path), "markdown": str(md_path), "html": str(html_path)}
