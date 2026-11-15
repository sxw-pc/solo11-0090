from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[1]

def _run(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pagedrift", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

def test_demo_cli_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    proc = _run("demo", "--out", str(out), "--frozen-time", "2024-01-01T00:00:00Z")
    assert proc.returncode == 0, proc.stderr
    assert "overall_verdict: unsafe" in proc.stdout
    assert (out / "ledger.sqlite").is_file()
    assert (out / "compare.json").is_file()
    assert (out / "compare.md").is_file()
    assert (out / "demo.json").is_file()
    for strategy in ("offset", "pagenum", "keyset", "snapshot"):
        assert (out / strategy / "audit.json").is_file()
        assert (out / strategy / "audit.md").is_file()
        assert (out / strategy / "audit.html").is_file()
        assert (out / strategy / "pages").is_dir()
    compare = json.loads((out / "compare.json").read_text(encoding="utf-8"))
    assert compare["overall_verdict"] == "unsafe"

def test_demo_refuses_non_empty_output(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    first = _run("demo", "--out", str(out))
    assert first.returncode == 0, first.stderr
    second = _run("demo", "--out", str(out))
    assert second.returncode == 2
    assert "not empty" in second.stderr

def test_demo_http_transport_cli(tmp_path: Path) -> None:
    out = tmp_path / "demo-http"
    proc = _run("demo", "--out", str(out), "--transport", "http", "--port", "0")
    assert proc.returncode == 0, proc.stderr
    assert "mock server bound to http://127.0.0.1:" in proc.stdout
    audit = json.loads((out / "snapshot" / "audit.json").read_text(encoding="utf-8"))
    assert audit["summary"]["verdict"] == "safe"

def test_evidence_verify_and_tamper_exit_codes(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    assert _run("demo", "--out", str(out)).returncode == 0
    run_dir = out / "offset"
    ok = _run("evidence", "verify", str(run_dir))
    assert ok.returncode == 0, ok.stderr
    assert "OK checked_pages=" in ok.stdout
    requests = [json.loads(line) for line in (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    page = run_dir / requests[0]["evidence_file"]
    page.write_bytes(page.read_bytes() + b"tamper")
    bad = _run("evidence", "verify", str(run_dir), "--ledger", str(out / "ledger.sqlite"))
    assert bad.returncode == 1
    assert "MISMATCH" in bad.stdout
    assert "PAGE_SHA_MISMATCH" in bad.stdout

def test_compare_and_ledger_cli(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    assert _run("demo", "--out", str(out)).returncode == 0
    compare_out = tmp_path / "cmp"
    proc = _run(
        "compare",
        str(out / "offset"),
        str(out / "snapshot"),
        "--out",
        str(compare_out),
    )
    assert proc.returncode == 0, proc.stderr
    assert "overall_verdict=unsafe" in proc.stdout
    run_id = json.loads((out / "offset" / "run.json").read_text(encoding="utf-8"))["run_id"]
    show = _run("ledger", "show", "--run", run_id, "--ledger", str(out / "ledger.sqlite"))
    assert show.returncode == 0, show.stderr
    payload = json.loads(show.stdout)
    assert payload["run"]["run_id"] == run_id
    assert payload["summary"]["verdict"] == "unsafe"

def test_collect_and_audit_cli(tmp_path: Path) -> None:
    out = tmp_path / "one"
    proc = _run(
        "collect",
        "--plan",
        str(ROOT / "pagedrift" / "plans" / "default.yaml"),
        "--strategy",
        "keyset",
        "--out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "audit.json").is_file()
    reaudit = _run("audit", "--run-dir", str(out))
    assert reaudit.returncode == 0, reaudit.stderr
    assert "strategy=keyset" in reaudit.stdout

def test_plan_validation_error_is_stable_cli_code(tmp_path: Path) -> None:
    plan_path = tmp_path / "bad-plan.yaml"
    plan_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "dataset": {"size": 5, "seed": 1},
                "source": {"base_url": "http://target.invalid", "sort_key": "sort_key"},
                "strategies": {"offset": {"limit": 2}},
                "mutation_schedule": "nope.yaml",
                "termination": {"allow": ["saw_empty_page"]},
                "audit": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    out = tmp_path / "never-created"
    proc = _run("demo", "--plan", str(plan_path), "--out", str(out))
    assert proc.returncode == 2
    assert "PLAN_NON_LOOPBACK_SOURCE" in proc.stderr
    assert not out.exists()
