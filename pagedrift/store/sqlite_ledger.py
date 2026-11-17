"""SQLite ledger with run, page, finding and strategy-summary tables."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any
from ..ids import sha256_bytes, sha256_file
from ..models import StrategySummary
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    universe_sha256 TEXT NOT NULL,
    schedule_sha256 TEXT NOT NULL,
    strategy_input_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    transport TEXT NOT NULL,
    run_dir TEXT NOT NULL,
    initial_count INTEGER NOT NULL,
    universe_max_key INTEGER
);
CREATE TABLE IF NOT EXISTS page_fetches (
    run_id TEXT NOT NULL,
    page_seq INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    status INTEGER NOT NULL,
    valid INTEGER NOT NULL,
    evidence_file TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    returned INTEGER NOT NULL,
    first_key INTEGER,
    last_key INTEGER,
    elapsed_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, page_seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    code TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    page_seq INTEGER NOT NULL,
    evidence_sha8 TEXT NOT NULL,
    detail TEXT NOT NULL,
    is_defect INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS strategy_summary (
    run_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    coverage_ratio REAL NOT NULL,
    dup_ratio REAL NOT NULL,
    skip_count INTEGER NOT NULL,
    max_observed_key INTEGER,
    universe_max_key INTEGER,
    findings_by_code_json TEXT NOT NULL,
    termination_satisfied INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    skips_per_100_pages REAL NOT NULL,
    integrity_ok INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS audit_runs (
    run_id TEXT PRIMARY KEY,
    audited_at TEXT NOT NULL,
    audit_sha256 TEXT NOT NULL,
    findings_total INTEGER NOT NULL,
    integrity_findings INTEGER NOT NULL,
    snapshot_consistent INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON audit_findings(run_id, code);
CREATE INDEX IF NOT EXISTS idx_pages_run ON page_fetches(run_id);
"""

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows

class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
    def close(self) -> None:
        self.conn.close()
    def __enter__(self) -> "Ledger":
        return self
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
    def record_run(self, run: dict[str, Any], run_dir: str | Path) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO runs
            (run_id, strategy, status, plan_sha256, universe_sha256, schedule_sha256,
             strategy_input_sha256, created_at, transport, run_dir, initial_count, universe_max_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["run_id"],
                run["strategy"],
                run["status"],
                run["plan_sha256"],
                run["universe_sha256"],
                run["schedule_sha256"],
                run["strategy_input_sha256"],
                run["created_at"],
                run["transport"],
                str(Path(run_dir).resolve()),
                int(run.get("initial_count", 0)),
                run.get("universe_max_key"),
            ),
        )
        self.conn.execute("DELETE FROM page_fetches WHERE run_id = ?", (run["run_id"],))
        for row in _read_jsonl(Path(run_dir) / "requests.jsonl"):
            self.conn.execute(
                """
                INSERT INTO page_fetches
                (run_id, page_seq, attempt, status, valid, evidence_file, sha256, returned,
                 first_key, last_key, elapsed_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    int(row["seq"]),
                    int(row.get("attempt", 1)),
                    int(row["status"]),
                    1 if row.get("valid") else 0,
                    row["evidence_file"],
                    row["sha256"],
                    int(row.get("returned", 0)),
                    row.get("first_key"),
                    row.get("last_key"),
                    int(row.get("elapsed_ms", 0)),
                ),
            )
        self.conn.commit()
    def record_audit(self, audit: dict[str, Any], audit_sha256: str | None = None) -> None:
        run_id = str(audit["run_id"])
        if audit_sha256 is None:
            audit_sha256 = sha256_bytes(json.dumps(audit, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        self.conn.execute("DELETE FROM audit_findings WHERE run_id = ?", (run_id,))
        for finding in audit["findings"]:
            self.conn.execute(
                """
                INSERT INTO audit_findings
                (run_id, code, natural_key, page_seq, evidence_sha8, detail, is_defect)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    finding["code"],
                    finding["natural_key"],
                    int(finding["page_seq"]),
                    finding["evidence_sha8"],
                    finding["detail"],
                    1 if finding.get("is_defect", True) else 0,
                ),
            )
        summary = audit["summary"]
        self.conn.execute(
            """
            INSERT OR REPLACE INTO strategy_summary
            (run_id, strategy, coverage_ratio, dup_ratio, skip_count, max_observed_key,
             universe_max_key, findings_by_code_json, termination_satisfied, verdict,
             skips_per_100_pages, integrity_ok)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                summary["strategy"],
                float(summary["coverage_ratio"]),
                float(summary["dup_ratio"]),
                int(summary["skip_count"]),
                summary.get("max_observed_key"),
                summary.get("universe_max_key"),
                json.dumps(summary["findings_by_code"], sort_keys=True),
                1 if summary["termination_satisfied"] else 0,
                summary["verdict"],
                float(summary["skips_per_100_pages"]),
                1 if summary.get("integrity_ok", True) else 0,
            ),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO audit_runs
            (run_id, audited_at, audit_sha256, findings_total, integrity_findings, snapshot_consistent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                audit.get("generated_at") or "",
                audit_sha256,
                len(audit["findings"]),
                len(audit.get("integrity_findings", [])),
                1 if audit.get("snapshot_integrity", {}).get("consistent") else 0,
            ),
        )
        self.conn.commit()
    def record_audit_file(self, audit: dict[str, Any], run_dir: str | Path) -> None:
        audit_path = Path(run_dir) / "audit.json"
        digest = sha256_file(audit_path) if audit_path.is_file() else None
        self.record_audit(audit, digest)
    def page_hashes(self, run_id: str) -> dict[int, str]:
        rows = self.conn.execute(
            "SELECT page_seq, sha256 FROM page_fetches WHERE run_id = ? ORDER BY page_seq", (run_id,)
        ).fetchall()
        return {int(row["page_seq"]): str(row["sha256"]) for row in rows}
    def show(self, run_id: str) -> dict[str, Any]:
        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        pages = self.conn.execute(
            "SELECT * FROM page_fetches WHERE run_id = ? ORDER BY page_seq", (run_id,)
        ).fetchall()
        findings = self.conn.execute(
            "SELECT * FROM audit_findings WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        summary = self.conn.execute(
            "SELECT * FROM strategy_summary WHERE run_id = ?", (run_id,)
        ).fetchone()
        audits = self.conn.execute(
            "SELECT * FROM audit_runs WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {
            "run": dict(run),
            "pages": [dict(row) for row in pages],
            "findings": [dict(row) for row in findings],
            "summary": dict(summary) if summary else None,
            "audits": [dict(row) for row in audits],
        }
    def summary_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM strategy_summary ORDER BY strategy"
        ).fetchall()
        return [dict(row) for row in rows]
