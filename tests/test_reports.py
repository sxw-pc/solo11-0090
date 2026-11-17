from __future__ import annotations
import json
import shutil
from pathlib import Path
from pagedrift.audit.engine import audit_run
from pagedrift.report.html import render_audit_html
from helpers import read_json

def test_three_audit_views_share_the_same_numbers(demo_bundle: dict) -> None:
    run_dir = demo_bundle["root"] / "offset"
    audit = read_json(run_dir / "audit.json")
    markdown = (run_dir / "audit.md").read_text(encoding="utf-8")
    html = (run_dir / "audit.html").read_text(encoding="utf-8")
    for value in (
        str(audit["summary"]["coverage_ratio"]),
        str(audit["summary"]["dup_ratio"]),
        str(audit["summary"]["skip_count"]),
        audit["summary"]["verdict"],
    ):
        assert value in markdown
        assert value in html
    assert "Termination proof" in markdown and "Termination proof" in html
    assert "Evidence pages" in markdown and "Evidence pages" in html

def test_html_is_self_contained_with_inline_css(demo_bundle: dict) -> None:
    html = (demo_bundle["root"] / "offset" / "audit.html").read_text(encoding="utf-8")
    assert "<style>" in html
    assert "<script" not in html.lower()
    assert "http://" not in html
    assert "https://" not in html
    assert "<link" not in html.lower()
    assert "src=" not in html
    assert "href=" not in html

def test_audit_json_is_byte_deterministic(demo_bundle: dict, tmp_path: Path) -> None:
    original = demo_bundle["root"] / "offset"
    copied = tmp_path / "offset-reaudit"
    shutil.copytree(original, copied)
    original_bytes = (original / "audit.json").read_bytes()
    audit_run(copied)
    assert (copied / "audit.json").read_bytes() == original_bytes
    assert (copied / "audit.md").read_text(encoding="utf-8") == (
        original / "audit.md"
    ).read_text(encoding="utf-8")

def test_reports_do_not_embed_absolute_paths(demo_bundle: dict, tmp_path: Path) -> None:
    original = demo_bundle["root"] / "offset"
    copied = tmp_path / "portable"
    shutil.copytree(original, copied)
    audit_run(copied)
    for name in ("audit.json", "audit.md", "audit.html"):
        text = (copied / name).read_text(encoding="utf-8")
        assert str(copied) not in text
        assert str(demo_bundle["root"]) not in text

def test_html_escapes_finding_details(demo_bundle: dict) -> None:
    audit = read_json(demo_bundle["root"] / "offset" / "audit.json")
    audit["findings"][0]["detail"] = '<script>alert("x")</script>'
    html = render_audit_html(audit)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html

def test_audit_json_has_stable_top_level_order(demo_bundle: dict) -> None:
    audit = read_json(demo_bundle["root"] / "offset" / "audit.json")
    assert list(audit)[:8] == [
        "schema_version",
        "run_id",
        "strategy",
        "generated_at",
        "inputs",
        "summary",
        "coverage",
        "termination_proof",
    ]
    json.dumps(audit, ensure_ascii=False)
