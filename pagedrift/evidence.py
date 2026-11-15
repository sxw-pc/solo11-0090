"""Byte-exact page evidence storage and two-way hash verification."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any
from .ids import sha256_bytes
from .models import PageEvidence

class EvidenceStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.pages_dir = self.run_dir / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
    def write_page(self, seq: int, body: bytes) -> PageEvidence:
        digest = sha256_bytes(body)
        relative = f"pages/{seq:04d}-{digest[:8]}.json"
        path = self.run_dir / relative
        if path.exists():
            raise FileExistsError(f"evidence page already exists: {relative}")
        path.write_bytes(body)
        return PageEvidence(seq=seq, path=relative, sha256=digest, size=len(body))
def _load_json(path: Path, issues: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append({"code": code, "detail": f"cannot read {path.name}: {exc}"})
        return None
def _load_requests(run_dir: Path, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = run_dir / "requests.jsonl"
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    issues.append({"code": "REQUESTS_INVALID", "detail": f"line {line_no}: {exc}"})
    except OSError as exc:
        issues.append({"code": "REQUESTS_MISSING", "detail": str(exc)})
    return rows
def _ledger_rows(ledger_path: Path, run_id: str, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not ledger_path.is_file():
        issues.append({"code": "LEDGER_MISSING", "detail": f"ledger not found: {ledger_path}"})
        return []
    try:
        conn = sqlite3.connect(f"file:{ledger_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT page_seq, evidence_file, sha256 FROM page_fetches WHERE run_id = ? ORDER BY page_seq",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        issues.append({"code": "LEDGER_INVALID", "detail": str(exc)})
        return []

def verify_run(run_dir: str | Path, ledger_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    issues: list[dict[str, Any]] = []
    checked = 0
    requests = _load_requests(root, issues)
    run_json = _load_json(root / "run.json", issues, "RUN_JSON_MISSING")
    run_id = str(run_json.get("run_id", "")) if run_json else ""
    referenced: set[str] = set()
    seen_seq: set[int] = set()
    actual_by_seq: dict[int, str] = {}
    for row in requests:
        seq = int(row.get("seq", 0))
        evidence_file = str(row.get("evidence_file", ""))
        expected = str(row.get("sha256", ""))
        if seq in seen_seq:
            issues.append({"code": "DUPLICATE_SEQ", "seq": seq, "detail": "duplicate request seq"})
        seen_seq.add(seq)
        if not evidence_file:
            issues.append({"code": "REQUEST_HASH_MISMATCH", "seq": seq, "detail": "missing evidence_file"})
            continue
        referenced.add(evidence_file)
        page_path = root / evidence_file
        if not page_path.is_file():
            issues.append(
                {"code": "PAGE_MISSING", "seq": seq, "path": evidence_file, "detail": "evidence file is missing"}
            )
            continue
        actual = sha256_bytes(page_path.read_bytes())
        actual_by_seq[seq] = actual
        checked += 1
        if actual != expected:
            issues.append(
                {
                    "code": "PAGE_SHA_MISMATCH",
                    "seq": seq,
                    "path": evidence_file,
                    "expected": expected,
                    "actual": actual,
                    "detail": "page bytes no longer match requests.jsonl",
                }
            )
        file_sha8 = page_path.stem.rsplit("-", 1)[-1]
        if file_sha8 != actual[:8]:
            issues.append(
                {
                    "code": "PAGE_NAME_MISMATCH",
                    "seq": seq,
                    "path": evidence_file,
                    "detail": "filename sha8 does not match page bytes",
                }
            )
    pages_dir = root / "pages"
    if pages_dir.is_dir():
        for page_path in sorted(pages_dir.iterdir()):
            if page_path.is_file():
                relative = f"pages/{page_path.name}"
                if relative not in referenced:
                    issues.append(
                        {
                            "code": "UNREFERENCED_PAGE",
                            "path": relative,
                            "detail": "page file is not referenced by requests.jsonl",
                        }
                    )
    ledger_rows: list[dict[str, Any]] = []
    if ledger_path is not None:
        ledger_rows = _ledger_rows(Path(ledger_path).resolve(), run_id, issues)
        request_by_seq = {int(row.get("seq", 0)): row for row in requests}
        ledger_by_seq = {int(row["page_seq"]): row for row in ledger_rows}
        for seq, row in request_by_seq.items():
            ledger_row = ledger_by_seq.get(seq)
            if ledger_row is None:
                issues.append({"code": "LEDGER_ROW_MISSING", "seq": seq, "detail": "page missing from ledger"})
                continue
            if ledger_row["sha256"] != row.get("sha256"):
                issues.append(
                    {
                        "code": "LEDGER_SHA_MISMATCH",
                        "seq": seq,
                        "expected": row.get("sha256"),
                        "actual": ledger_row["sha256"],
                        "detail": "requests and SQLite ledger disagree",
                    }
                )
            actual = actual_by_seq.get(seq)
            if actual is not None and ledger_row["sha256"] != actual:
                issues.append(
                    {
                        "code": "LEDGER_SHA_MISMATCH",
                        "seq": seq,
                        "expected": ledger_row["sha256"],
                        "actual": actual,
                        "detail": "SQLite ledger and page bytes disagree",
                    }
                )
            if ledger_row["evidence_file"] != row.get("evidence_file"):
                issues.append(
                    {"code": "LEDGER_PATH_MISMATCH", "seq": seq, "detail": "evidence file path disagrees"}
                )
        for seq in ledger_by_seq:
            if seq not in request_by_seq:
                issues.append(
                    {"code": "LEDGER_ORPHAN", "seq": seq, "detail": "ledger page has no request row"}
                )
    status = "OK" if not issues else "MISMATCH"
    return {
        "status": status,
        "run_id": run_id,
        "checked_pages": checked,
        "requests": len(requests),
        "ledger_pages": len(ledger_rows),
        "issues": issues,
    }
