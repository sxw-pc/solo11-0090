"""Self-contained, zero-external-resource HTML audit view."""
from __future__ import annotations
import html
from typing import Any
STYLE = """
:root { color-scheme: light dark; --bg:#f7f5ef; --fg:#1d2522; --card:#fffdf7; --line:#c9c4b7; --accent:#0d6656; --bad:#a33232; }
@media (prefers-color-scheme: dark) { :root { --bg:#141917; --fg:#ece9df; --card:#1d2421; --line:#3e4a45; --accent:#7ad9c4; --bad:#ff9a9a; } }
* { box-sizing: border-box; }
body { margin:0; padding:2rem; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
main { max-width:1100px; margin:0 auto; }
h1,h2 { line-height:1.2; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem 1.25rem; margin:1rem 0; }
.meta { color:var(--accent); font-weight:600; }
.verdict { font-size:1.2rem; font-weight:700; }
.bad { color:var(--bad); }
table { border-collapse:collapse; width:100%; margin:.6rem 0; font-size:.9rem; }
th,td { border:1px solid var(--line); padding:.45rem .55rem; text-align:left; vertical-align:top; overflow-wrap:anywhere; }
th { background:color-mix(in srgb, var(--accent) 12%, transparent); }
code { overflow-wrap:anywhere; }
.small { font-size:.85rem; opacity:.85; }
""".strip()

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)
def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(value)}</td>" for value in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

def render_audit_html(audit: dict[str, Any]) -> str:
    summary = audit["summary"]
    proof = audit["termination_proof"]
    findings = audit["findings"]
    integrity = audit["integrity_findings"]
    inputs = audit["inputs"]
    verdict_class = "bad" if summary["verdict"] != "safe" else ""
    finding_table = (
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
        if findings
        else "<p>No drift findings.</p>"
    )
    integrity_table = (
        _table(
            ["code", "page_seq", "detail"],
            [[item["code"], item["page_seq"], item["detail"]] for item in integrity],
        )
        if integrity
        else "<p>No integrity errors.</p>"
    )
    evidence_table = (
        _table(
            ["seq", "file", "status", "returned", "sha256"],
            [
                [item["seq"], item["evidence_file"], item["status"], item["returned"], item["sha256"]]
                for item in audit["evidence"]
            ],
        )
        if audit["evidence"]
        else "<p>No page evidence was captured.</p>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pagedrift audit {_e(audit['run_id'])}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>pagedrift audit</h1>
<p class="small">Offline verification of a synthetic, loopback-only pagination source. This file has no external resources.</p>
<section class="card">
<div class="meta">{_e(audit['strategy'])} · {_e(audit['run_id'])}</div>
<p class="verdict {verdict_class}">verdict: {_e(summary['verdict'])}</p>
<p>generated_at: <code>{_e(audit['generated_at'])}</code></p>
<p>coverage {_e(summary['coverage_ratio'])} · dup_ratio {_e(summary['dup_ratio'])} · skips {_e(summary['skip_count'])} · pages {_e(summary['pages_fetched'])}</p>
<p>max observed key {_e(summary['max_observed_key'])} / universe max {_e(summary['universe_max_key'])}</p>
</section>
<section class="card">
<h2>Termination proof</h2>
<p>satisfied: <strong>{_e(proof['satisfied'])}</strong> · stop reason: <code>{_e(proof['stop_reason'])}</code></p>
<p>matched: {_e(', '.join(proof['matched_conditions']) or 'none')}</p>
<p>declared: {_e(', '.join(proof['declared_conditions']) or 'none')}</p>
<p>rejected: {_e('; '.join(proof['rejected_because']) or 'none')}</p>
</section>
<section class="card"><h2>Findings</h2>{finding_table}</section>
<section class="card"><h2>Integrity</h2>{integrity_table}</section>
<section class="card"><h2>Evidence pages</h2>{evidence_table}</section>
<section class="card">
<h2>Input fingerprints</h2>
<p>plan <code>{_e(inputs['plan_sha256'])}</code></p>
<p>universe <code>{_e(inputs['universe_sha256'])}</code></p>
<p>schedule <code>{_e(inputs['schedule_sha256'])}</code></p>
<p>strategy input <code>{_e(inputs['strategy_input_sha256'])}</code></p>
</section>
</main>
</body>
</html>
"""
