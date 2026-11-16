"""Deterministic JSON serialization for machine-readable reports."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

def dumps_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

def write_json(path: Path, obj: Any) -> None:
    path.write_text(dumps_json(obj), encoding="utf-8", newline="\n")

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
