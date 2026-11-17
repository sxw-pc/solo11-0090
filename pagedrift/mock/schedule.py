"""Deterministic mutation scheduler keyed by item-request sequence."""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml
from ..models import Record
from .universe import Universe
MUTATION_TYPES = ("insert", "delete", "update_sortkey", "noop")

class ScheduleError(ValueError):
    pass

@dataclass(frozen=True)
class Mutation:
    type: str
    natural_key: str | None = None
    sort_key: int | None = None
    title: str | None = None
    category: str | None = None

@dataclass(frozen=True)
class ScheduledMutation:
    at_request: int
    mutations: tuple[Mutation, ...]
def _require_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScheduleError(f"{field} must be an integer")
    return value

def parse_mutation(raw: Any, index: int) -> Mutation:
    if not isinstance(raw, dict):
        raise ScheduleError(f"mutation #{index} must be a mapping")
    kind = raw.get("type")
    if kind not in MUTATION_TYPES:
        raise ScheduleError(f"mutation #{index} has unknown type {kind!r}")
    if kind == "noop":
        return Mutation(type="noop", natural_key=raw.get("id"))
    natural_key = raw.get("id")
    if not isinstance(natural_key, str) or not natural_key:
        raise ScheduleError(f"mutation #{index} requires a string id")
    if kind == "insert":
        if "sort_key" not in raw:
            raise ScheduleError(f"mutation #{index} insert requires sort_key")
        key = _require_int(raw["sort_key"], "sort_key")
        return Mutation(
            type=kind,
            natural_key=natural_key,
            sort_key=key,
            title=str(raw.get("title", f"Inserted {natural_key}")),
            category=str(raw.get("category", "inserted")),
        )
    if kind == "update_sortkey":
        if "sort_key" not in raw:
            raise ScheduleError(f"mutation #{index} update_sortkey requires sort_key")
        return Mutation(type=kind, natural_key=natural_key, sort_key=_require_int(raw["sort_key"], "sort_key"))
    return Mutation(type=kind, natural_key=natural_key)

class MutationSchedule:
    def __init__(self, entries: list[ScheduledMutation], source_path: Path | None = None):
        self.entries = sorted(entries, key=lambda entry: entry.at_request)
        self.source_path = source_path
    @classmethod
    def load(cls, path: str | Path) -> "MutationSchedule":
        schedule_path = Path(path).resolve()
        if not schedule_path.is_file():
            raise ScheduleError(f"schedule file not found: {path}")
        try:
            raw = yaml.safe_load(schedule_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ScheduleError(f"invalid schedule YAML: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("before_request"), list):
            raise ScheduleError("schedule must contain a before_request list")
        entries: list[ScheduledMutation] = []
        for entry_index, entry in enumerate(raw["before_request"]):
            if not isinstance(entry, dict):
                raise ScheduleError(f"before_request #{entry_index} must be a mapping")
            at = entry.get("at", entry.get("request"))
            at_request = _require_int(at, "at")
            if at_request < 1:
                raise ScheduleError("at must be >= 1")
            mutations_raw = entry.get("mutations", [])
            if not isinstance(mutations_raw, list):
                raise ScheduleError("mutations must be a list")
            mutations = tuple(parse_mutation(item, i) for i, item in enumerate(mutations_raw))
            entries.append(ScheduledMutation(at_request=at_request, mutations=mutations))
        return cls(entries, schedule_path)
    @staticmethod
    def _payload_sha8(natural_key: str, sort_key: int, title: str, category: str) -> str:
        material = f"{natural_key}|{sort_key}|{title}|{category}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:8]
    def apply(self, request_seq: int, universe: Universe) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        for entry in self.entries:
            if entry.at_request != request_seq:
                continue
            for index, mutation in enumerate(entry.mutations):
                base = {
                    "request_seq": request_seq,
                    "mutation_index": index,
                    "type": mutation.type,
                    "id": mutation.natural_key,
                    "applied": False,
                }
                if mutation.type == "noop":
                    base["applied"] = True
                    base["universe_version"] = universe.version
                    logs.append(base)
                    continue
                try:
                    if mutation.type == "insert":
                        assert mutation.natural_key and mutation.sort_key is not None
                        title = mutation.title or f"Inserted {mutation.natural_key}"
                        category = mutation.category or "inserted"
                        payload = self._payload_sha8(mutation.natural_key, mutation.sort_key, title, category)
                        change = universe.apply_insert(
                            natural_key=mutation.natural_key,
                            sort_key=mutation.sort_key,
                            title=title,
                            category=category,
                            payload_sha8=payload,
                        )
                    elif mutation.type == "delete":
                        assert mutation.natural_key
                        change = universe.apply_delete(mutation.natural_key)
                    else:
                        assert mutation.natural_key and mutation.sort_key is not None
                        change = universe.apply_update_sortkey(mutation.natural_key, mutation.sort_key)
                except KeyError:
                    base["reason"] = "natural_key_not_found"
                    logs.append(base)
                    continue
                except ValueError as exc:
                    base["reason"] = str(exc)
                    logs.append(base)
                    continue
                base.update(change)
                base["applied"] = True
                base["universe_version"] = universe.version
                logs.append(base)
        return logs

def record_from_mutation_log(row: dict[str, Any]) -> Record | None:
    after = row.get("after")
    if not isinstance(after, dict):
        return None
    return Record.from_dict(after)
