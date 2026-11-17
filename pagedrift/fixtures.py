"""Deterministic synthetic universes used by demos and tests."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any
from .config import Plan
from .ids import item_id, universe_bytes
from .models import Record
CATEGORIES = ("alpha", "beta", "gamma", "delta")
PACKAGE_DIR = Path(__file__).resolve().parent

def generate_universe(
    size: int = 30,
    seed: int = 10090,
    start_key: int = 10,
    key_step: int = 3,
    dataset_name: str = "synthetic-demo",
) -> list[Record]:
    """Create a deterministic ordered universe; no random module is involved."""
    if size < 1 or key_step < 1:
        raise ValueError("size and key_step must be positive")
    records: list[Record] = []
    for number in range(1, size + 1):
        key = start_key + (number - 1) * key_step
        category = CATEGORIES[(number + seed) % len(CATEGORIES)]
        title = f"Synthetic item {number:04d}"
        payload_material = f"{dataset_name}|{seed}|{number}|{key}|{title}|{category}"
        payload = hashlib.sha256(payload_material.encode("utf-8")).hexdigest()[:8]
        records.append(
            Record(
                id=item_id(number),
                sort_key=key,
                title=title,
                category=category,
                payload_sha8=payload,
            )
        )
    return records

def write_universe(path: Path, records: list[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(universe_bytes(records))

def load_universe(path: Path) -> list[Record]:
    records: list[Record] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                records.append(Record.from_dict(row))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid universe line {line_no} in {path}: {exc}") from exc
    return sorted(records, key=lambda record: (record.sort_key, record.id))
def _resolve_fixture(value: str, plan: Plan) -> Path:
    candidate = Path(value)
    candidates = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend([plan.path.parent / candidate, PACKAGE_DIR / candidate, PACKAGE_DIR / "fixtures" / candidate])
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"dataset fixture not found: {value}")

def dataset_from_plan(plan: Plan, seed_override: int | None = None) -> tuple[list[Record], dict[str, Any]]:
    dataset = plan.dataset
    if "fixture" in dataset:
        path = _resolve_fixture(str(dataset["fixture"]), plan)
        records = load_universe(path)
        info = {
            "kind": "fixture",
            "fixture": str(dataset["fixture"]),
            "fixture_sha256_file": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": len(records),
        }
        return records, info
    seed = int(dataset.get("seed", 10090) if seed_override is None else seed_override)
    records = generate_universe(
        size=int(dataset["size"]),
        seed=seed,
        start_key=int(dataset.get("start_key", 10)),
        key_step=int(dataset.get("key_step", 3)),
        dataset_name=str(dataset.get("name", "synthetic-demo")),
    )
    info = {
        "kind": "generated",
        "name": str(dataset.get("name", "synthetic-demo")),
        "size": len(records),
        "seed": seed,
        "start_key": int(dataset.get("start_key", 10)),
        "key_step": int(dataset.get("key_step", 3)),
    }
    return records, info
