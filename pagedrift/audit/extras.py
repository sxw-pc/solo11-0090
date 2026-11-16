"""Attribute observations that appeared only during collection."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows

def insert_attribution(mutations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    attribution: dict[str, dict[str, Any]] = {}
    for row in mutations:
        if row.get("type") != "insert" or not row.get("applied"):
            continue
        natural_key = row.get("id")
        if not isinstance(natural_key, str):
            continue
        attribution.setdefault(
            natural_key,
            {
                "mutation_request_seq": row.get("request_seq"),
                "mutation_index": row.get("mutation_index"),
                "sort_key": (row.get("after") or {}).get("sort_key"),
            },
        )
    return attribution

def deleted_ids(mutations: list[dict[str, Any]]) -> set[str]:
    return {
        str(row["id"])
        for row in mutations
        if row.get("type") == "delete" and row.get("applied") and isinstance(row.get("id"), str)
    }

def observed_extras(
    records: list[dict[str, Any]],
    initial_ids: set[str],
    mutations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    attribution = insert_attribution(mutations)
    first_observation: dict[str, dict[str, Any]] = {}
    for record in records:
        natural_key = str(record["id"])
        if natural_key in initial_ids or natural_key in first_observation:
            continue
        first_observation[natural_key] = record
    extras: list[dict[str, Any]] = []
    for natural_key in sorted(first_observation):
        record = first_observation[natural_key]
        attr = attribution.get(natural_key)
        extras.append(
            {
                "id": natural_key,
                "sort_key": record.get("sort_key"),
                "source_page_seq": record.get("source_page_seq"),
                "attributed_insert": attr is not None,
                "mutation_request_seq": attr.get("mutation_request_seq") if attr else None,
            }
        )
    return extras
