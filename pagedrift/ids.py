"""Stable identifiers, canonical JSON and fingerprints."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

def canonical_json(obj: Any) -> bytes:
    """Canonical compact JSON bytes used by both transports and evidence."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))

def sha8(value: str) -> str:
    return value[:8]

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def record_rows(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize record objects/dicts and sort them by (sort_key, id)."""
    rows = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in records]
    return sorted(rows, key=lambda r: (int(r["sort_key"]), str(r["id"])))

def universe_bytes(records: Iterable[Any]) -> bytes:
    lines = [canonical_json(row) + b"\n" for row in record_rows(records)]
    return b"".join(lines)

def universe_fingerprint(records: Iterable[Any]) -> str:
    return sha256_bytes(universe_bytes(records))

def stable_run_id(
    *,
    strategy: str,
    plan_sha256: str,
    universe_sha256: str,
    schedule_sha256: str,
    strategy_input_sha256: str,
    frozen_time: str,
    seed: int,
) -> str:
    material = canonical_json(
        {
            "strategy": strategy,
            "plan_sha256": plan_sha256,
            "universe_sha256": universe_sha256,
            "schedule_sha256": schedule_sha256,
            "strategy_input_sha256": strategy_input_sha256,
            "frozen_time": frozen_time,
            "seed": seed,
        }
    )
    return "run-" + sha256_bytes(material)[:12]

def item_id(number: int) -> str:
    return f"itm-{number:04d}"
