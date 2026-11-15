"""Side-by-side safety comparison for strategy runs."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from ..report.json_report import write_json
from .engine import audit_run
STRATEGY_ORDER = ("offset", "pagenum", "keyset", "snapshot")

def _read_audit(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "audit.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return audit_run(run_dir)
def _row(audit: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    summary = audit["summary"]
    proof = audit["termination_proof"]
    return {
        "strategy": audit["strategy"],
        "run_id": audit["run_id"],
        "run_dir": run_dir.name,
        "coverage": summary["coverage_ratio"],
        "coverage_ratio": summary["coverage_ratio"],
        "dup_ratio": summary["dup_ratio"],
        "skip_count": summary["skip_count"],
        "pages_fetched": summary["pages_fetched"],
        "skips_per_100_pages": summary["skips_per_100_pages"],
        "termination_proof": {
            "satisfied": proof["satisfied"],
            "matched_conditions": proof["matched_conditions"],
            "stop_reason": proof["stop_reason"],
        },
        "findings_by_code": summary["findings_by_code"],
        "integrity_ok": summary["integrity_ok"],
        "verdict": summary["verdict"],
    }
def _overall(rows: list[dict[str, Any]], compatible: bool) -> tuple[str, list[str], bool]:
    messages: list[str] = []
    if not compatible:
        messages.append("input fingerprints differ; the runs are not directly comparable")
        return "inconclusive", messages, False
    snapshot_rows = [row for row in rows if row["strategy"] == "snapshot"]
    snapshot_defect = any(row["verdict"] != "safe" for row in snapshot_rows)
    if snapshot_defect:
        messages.append("snapshot baseline is not safe: mock or audit itself is defective")
        return "inconclusive", messages, True
    if any(row["verdict"] == "inconclusive" for row in rows):
        messages.append("at least one strategy has an unproven run or integrity error")
        return "inconclusive", messages, False
    if rows and all(row["verdict"] == "safe" for row in rows):
        messages.append("all compared strategies are safe on this schedule")
        return "safe", messages, False
    messages.append("at least one strategy lost or duplicated data under this schedule")
    return "unsafe", messages, False

def build_comparison(run_dirs: list[str | Path]) -> dict[str, Any]:
    resolved = [Path(item).resolve() for item in run_dirs]
    audits = [_read_audit(path) for path in resolved]
    rows = [_row(audit, path) for audit, path in zip(audits, resolved)]
    rows.sort(key=lambda row: (STRATEGY_ORDER.index(row["strategy"]) if row["strategy"] in STRATEGY_ORDER else 99, row["strategy"]))
    fingerprints = {
        "plan_sha256": sorted({str(audit["inputs"]["plan_sha256"]) for audit in audits}),
        "universe_sha256": sorted({str(audit["inputs"]["universe_sha256"]) for audit in audits}),
        "schedule_sha256": sorted({str(audit["inputs"]["schedule_sha256"]) for audit in audits}),
    }
    compatible = all(len(values) == 1 for values in fingerprints.values())
    verdict, messages, snapshot_defect = _overall(rows, compatible)
    result = {
        "schema_version": 1,
        "generated_at": audits[0].get("generated_at") if audits else None,
        "overall_verdict": verdict,
        "input_fingerprints": fingerprints,
        "inputs_compatible": compatible,
        "snapshot_baseline_defect": snapshot_defect,
        "messages": messages,
        "rows": rows,
    }
    return result

def render_compare_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# pagedrift strategy comparison",
        "",
        "> Offline comparison of the same synthetic universe and mutation schedule.",
        "",
        f"- overall_verdict: **{result['overall_verdict']}**",
        f"- inputs_compatible: `{result['inputs_compatible']}`",
        f"- snapshot_baseline_defect: `{result['snapshot_baseline_defect']}`",
        "",
        "| strategy | coverage | dup_ratio | skip | termination | verdict | skips/100 pages |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        proof = row["termination_proof"]
        lines.append(
            f"| {row['strategy']} | {row['coverage']} | {row['dup_ratio']} | {row['skip_count']} | "
            f"{proof['satisfied']} ({proof['stop_reason']}) | {row['verdict']} | {row['skips_per_100_pages']} |"
        )
    lines.extend(["", "## Messages", ""])
    lines.extend(f"- {message}" for message in result["messages"])
    lines.extend(
        [
            "",
            "## Input fingerprints",
            "",
            f"- plan: `{', '.join(result['input_fingerprints']['plan_sha256'])}`",
            f"- universe: `{', '.join(result['input_fingerprints']['universe_sha256'])}`",
            f"- schedule: `{', '.join(result['input_fingerprints']['schedule_sha256'])}`",
            "",
        ]
    )
    return "\n".join(lines)

def write_comparison(result: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "compare.json"
    md_path = root / "compare.md"
    write_json(json_path, result)
    md_path.write_text(render_compare_markdown(result), encoding="utf-8", newline="\n")
    return {"json": str(json_path), "markdown": str(md_path)}

def compare_runs(run_dirs: list[str | Path], out_dir: str | Path) -> dict[str, Any]:
    result = build_comparison(run_dirs)
    write_comparison(result, out_dir)
    return result
