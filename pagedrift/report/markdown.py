"""Human-readable Markdown view of an audit."""
from __future__ import annotations
from typing import Any

def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)

def render_audit_markdown(audit: dict[str, Any]) -> str:
    summary = audit["summary"]
    proof = audit["termination_proof"]
    findings = audit["findings"]
    integrity = audit["integrity_findings"]
    inputs = audit["inputs"]
    lines = [
        "# pagedrift audit",
        "",
        "> Offline verification of a synthetic, loopback-only pagination source.",
        "",
        f"- run_id: `{audit['run_id']}`",
        f"- strategy: `{audit['strategy']}`",
        f"- verdict: **{summary['verdict']}**",
        f"- generated_at: `{audit['generated_at']}`",
        "",
        "## Summary",
        "",
        _table(
            [
                "coverage",
                "dup_ratio",
                "skips",
                "max_observed_key",
                "universe_max_key",
                "pages",
            ],
            [
                [
                    summary["coverage_ratio"],
                    summary["dup_ratio"],
                    summary["skip_count"],
                    summary["max_observed_key"],
                    summary["universe_max_key"],
                    summary["pages_fetched"],
                ]
            ],
        ),
        "",
        f"Findings: {len(findings)}; integrity findings: {len(integrity)}.",
        "",
        "## Termination proof",
        "",
        f"- satisfied: `{proof['satisfied']}`",
        f"- stop_reason: `{proof['stop_reason']}`",
        f"- matched: `{', '.join(proof['matched_conditions']) or 'none'}`",
        f"- declared: `{', '.join(proof['declared_conditions']) or 'none'}`",
        f"- rejected: {'; '.join(proof['rejected_because']) or 'none'}",
        "",
        "## Findings",
        "",
    ]
    if findings:
        lines.append(
            _table(
                ["code", "natural_key", "page_seq", "evidence_sha8", "detail"],
                [
                    [
                        item["code"],
                        item["natural_key"],
                        item["page_seq"],
                        item["evidence_sha8"],
                        item["detail"],
                    ]
                    for item in findings
                ],
            )
        )
    else:
        lines.append("No drift findings.")
    lines.extend(["", "## Integrity", ""])
    if integrity:
        lines.append(
            _table(
                ["code", "page_seq", "detail"],
                [[item["code"], item["page_seq"], item["detail"]] for item in integrity],
            )
        )
    else:
        lines.append("No integrity errors.")
    lines.extend(["", "## Evidence pages", ""])
    if audit["evidence"]:
        lines.append(
            _table(
                ["seq", "file", "status", "returned", "sha256"],
                [
                    [item["seq"], item["evidence_file"], item["status"], item["returned"], item["sha256"]]
                    for item in audit["evidence"]
                ],
            )
        )
    else:
        lines.append("No page evidence was captured.")
    lines.extend(
        [
            "",
            "## Input fingerprints",
            "",
            f"- plan: `{inputs['plan_sha256']}`",
            f"- universe: `{inputs['universe_sha256']}`",
            f"- schedule: `{inputs['schedule_sha256']}`",
            f"- strategy input: `{inputs['strategy_input_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)
