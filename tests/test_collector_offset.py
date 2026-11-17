from __future__ import annotations
import json
from pathlib import Path
import pytest
from pagedrift.audit.engine import audit_run
from pagedrift.collectors.base import CollectorError, collect_strategy
from pagedrift.models import TransportResponse
from helpers import build_context, execute_strategy, make_plan, make_records, read_jsonl, write_jsonl

class FlakyTransport:
    def __init__(self, inner, failures: int = 1):
        self.inner = inner
        self.failures = failures
        self.calls = 0
    def meta(self):
        return self.inner.meta()
    def fetch(self, query):
        self.calls += 1
        if self.calls <= self.failures:
            return TransportResponse(200, b'{"not":"a page"}\n', 0)
        return self.inner.fetch(query)

def test_offset_artifacts_and_byte_exact_evidence(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    records = make_records(12)
    result = execute_strategy(plan, "offset", records, tmp_path / "runs")
    run_dir = result["run_dir"]
    requests = read_jsonl(run_dir / "requests.jsonl")
    rows = read_jsonl(run_dir / "records.jsonl")
    assert result["run"]["status"] == "completed"
    assert result["run"]["transport"] == "inprocess"
    assert result["run"]["termination_observation"]["keyset_reached_max"] is True
    assert len(rows) == 12
    assert [row["source_page_seq"] for row in rows[:3]] == [1, 1, 1]
    assert len(requests) == 4
    for request in requests:
        page_path = run_dir / request["evidence_file"]
        raw = page_path.read_bytes()
        assert raw.endswith(b"\n")
        payload = json.loads(raw)
        assert payload["page"]["returned"] == request["returned"] == 3
        assert request["elapsed_ms"] == 0
        assert request["valid"] is True
    assert result["audit"]["summary"]["verdict"] == "safe"
    assert result["audit"]["summary"]["coverage_ratio"] == 1.0

def test_offset_retries_invalid_page_once_then_continues(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="retry-plan")
    records = make_records(10)
    ctx, core = build_context(plan, records, tmp_path / "retry-run")
    ctx.strategy = "offset"
    flaky = FlakyTransport(ctx.transport, failures=1)
    ctx.transport = flaky
    collect_strategy(ctx)
    write_jsonl(ctx.out_dir / "mutation_log.jsonl", core.mutation_log)
    audit = audit_run(ctx.out_dir)
    requests = read_jsonl(ctx.out_dir / "requests.jsonl")
    assert len(requests) == 5
    assert requests[0]["valid"] is False
    assert requests[0]["error_code"] == "RESP_INVALID"
    assert requests[1]["valid"] is True
    assert audit["summary"]["verdict"] == "safe"
    assert len(read_jsonl(ctx.out_dir / "records.jsonl")) == 10

def test_offset_two_invalid_pages_fail_but_evidence_remains(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="fail-plan")
    records = make_records(6)
    ctx, core = build_context(plan, records, tmp_path / "fail-run")
    ctx.strategy = "offset"
    ctx.transport = FlakyTransport(ctx.transport, failures=99)
    collect_strategy(ctx)
    write_jsonl(ctx.out_dir / "mutation_log.jsonl", core.mutation_log)
    audit = audit_run(ctx.out_dir)
    run = json.loads((ctx.out_dir / "run.json").read_text(encoding="utf-8"))
    requests = read_jsonl(ctx.out_dir / "requests.jsonl")
    assert run["status"] == "failed"
    assert len(requests) == 2
    assert len(list((ctx.out_dir / "pages").iterdir())) == 2
    assert audit["integrity_findings"][0]["code"] == "RESP_INVALID"
    assert audit["summary"]["verdict"] == "inconclusive"

def test_run_directory_is_append_only(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="once-plan")
    records = make_records(5)
    execute_strategy(plan, "offset", records, tmp_path / "once")
    with pytest.raises(CollectorError):
        execute_strategy(plan, "offset", records, tmp_path / "once")

def test_run_id_is_stable_for_same_inputs(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="stable-plan")
    records = make_records(5)
    first = execute_strategy(plan, "offset", records, tmp_path / "stable-a")
    second = execute_strategy(plan, "offset", records, tmp_path / "stable-b")
    assert first["run"]["run_id"] == second["run"]["run_id"]
    assert first["run"]["universe_sha256"] == second["run"]["universe_sha256"]
    assert first["run"]["schedule_sha256"] == second["run"]["schedule_sha256"]
