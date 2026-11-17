"""Declarative plan loading and fail-fast static validation."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
import yaml
from .ids import canonical_json, sha256_bytes, sha256_file
from .models import TERMINATION_CONDITIONS
KNOWN_STRATEGIES = ("offset", "pagenum", "keyset", "snapshot")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_PARAMS = {
    "offset_param": "offset",
    "limit_param": "limit",
    "page_param": "page",
    "page_size_param": "page_size",
    "after_key_param": "after_key",
    "snapshot_param": "snapshot",
    "items_path": "/items",
    "meta_path": "/meta",
    "sort_key_field": "sort_key",
}

class PlanError(ValueError):
    """A collection of stable validation issues."""
    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        names = ", ".join(issue["code"] for issue in issues)
        super().__init__(f"plan validation failed: {names}")

def loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized in LOOPBACK_HOSTS

def base_url_host(url: str) -> str:
    return urlsplit(url).hostname or ""

@dataclass(frozen=True)
class Plan:
    raw: dict[str, Any]
    path: Path
    sha256: str
    schedule_path: Path
    schedule_sha256: str
    @property
    def strategies(self) -> dict[str, dict[str, Any]]:
        return self.raw["strategies"]
    @property
    def source(self) -> dict[str, Any]:
        return self.raw["source"]
    @property
    def dataset(self) -> dict[str, Any]:
        return self.raw["dataset"]
    @property
    def audit(self) -> dict[str, Any]:
        return self.raw["audit"]
    @property
    def termination(self) -> dict[str, Any]:
        return self.raw["termination"]
    @property
    def base_url(self) -> str:
        return str(self.raw["source"]["base_url"]).rstrip("/")
    def strategy_input(self, name: str) -> dict[str, Any]:
        return dict(self.strategies[name])
    def strategy_input_sha(self, name: str) -> str:
        return sha256_bytes(canonical_json(self.strategy_input(name)))
    def public_dict(self) -> dict[str, Any]:
        return dict(self.raw)
def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}
def _positive_int(value: Any, maximum: int = 500) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum
def _validate_dataset(dataset: Any, issues: list[dict[str, str]]) -> None:
    if not isinstance(dataset, dict):
        issues.append(_issue("PLAN_MISSING_FIELD", "dataset", "dataset must be a mapping"))
        return
    if "fixture" in dataset:
        if not isinstance(dataset["fixture"], str) or not dataset["fixture"]:
            issues.append(_issue("PLAN_MISSING_FIELD", "dataset.fixture", "fixture must be a path string"))
        return
    if "size" not in dataset:
        issues.append(_issue("PLAN_MISSING_FIELD", "dataset.size", "dataset.size or dataset.fixture is required"))
    elif not _positive_int(dataset["size"], 100000):
        issues.append(_issue("PLAN_BAD_LIMIT", "dataset.size", "dataset.size must be a positive integer"))
    if "seed" not in dataset:
        issues.append(_issue("PLAN_MISSING_FIELD", "dataset.seed", "dataset.seed is required"))
    elif not isinstance(dataset["seed"], int) or isinstance(dataset["seed"], bool):
        issues.append(_issue("PLAN_BAD_LIMIT", "dataset.seed", "dataset.seed must be an integer"))
    for field in ("start_key", "key_step"):
        if field in dataset and (
            not isinstance(dataset[field], int) or isinstance(dataset[field], bool) or dataset[field] < 0
        ):
            issues.append(_issue("PLAN_BAD_LIMIT", f"dataset.{field}", f"{field} must be a non-negative integer"))
    if dataset.get("key_step", 1) == 0:
        issues.append(_issue("PLAN_BAD_LIMIT", "dataset.key_step", "key_step must be greater than zero"))
def _validate_source(source: Any, issues: list[dict[str, str]]) -> None:
    if not isinstance(source, dict):
        issues.append(_issue("PLAN_MISSING_FIELD", "source", "source must be a mapping"))
        return
    for field in ("base_url", "sort_key"):
        if field not in source:
            issues.append(_issue("PLAN_MISSING_FIELD", f"source.{field}", f"source.{field} is required"))
    url = source.get("base_url")
    if isinstance(url, str):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            issues.append(_issue("PLAN_NON_LOOPBACK_SOURCE", "source.base_url", "base_url must be a valid http URL"))
        elif not loopback_host(parts.hostname):
            issues.append(_issue("PLAN_NON_LOOPBACK_SOURCE", "source.base_url", "base_url host must be loopback"))
    elif "base_url" in source:
        issues.append(_issue("PLAN_NON_LOOPBACK_SOURCE", "source.base_url", "base_url must be a string"))
    if "max_page_size" in source and not _positive_int(source["max_page_size"], 1000):
        issues.append(_issue("PLAN_BAD_LIMIT", "source.max_page_size", "max_page_size must be 1..1000"))
def _validate_strategies(strategies: Any, issues: list[dict[str, str]]) -> None:
    if not isinstance(strategies, dict) or not strategies:
        issues.append(_issue("PLAN_MISSING_FIELD", "strategies", "strategies must be a non-empty mapping"))
        return
    for name, cfg in strategies.items():
        path = f"strategies.{name}"
        if name not in KNOWN_STRATEGIES:
            issues.append(_issue("PLAN_UNKNOWN_STRATEGY", path, f"unknown strategy {name!r}"))
            continue
        if not isinstance(cfg, dict):
            issues.append(_issue("PLAN_BAD_LIMIT", path, "strategy settings must be a mapping"))
            continue
        limit_field = "page_size" if name == "pagenum" else "limit"
        if limit_field not in cfg:
            issues.append(_issue("PLAN_BAD_LIMIT", f"{path}.{limit_field}", f"{limit_field} is required"))
        elif not _positive_int(cfg[limit_field]):
            issues.append(_issue("PLAN_BAD_LIMIT", f"{path}.{limit_field}", f"{limit_field} must be 1..500"))
        if "max_pages" in cfg and not _positive_int(cfg["max_pages"], 1000):
            issues.append(_issue("PLAN_BAD_LIMIT", f"{path}.max_pages", "max_pages must be 1..1000"))
def _validate_schedule(path: Path, schedule: Any, issues: list[dict[str, str]]) -> Path | None:
    if not isinstance(schedule, str) or not schedule:
        issues.append(_issue("PLAN_MISSING_FIELD", "mutation_schedule", "mutation_schedule must be a path string"))
        return None
    resolved = (path.parent / schedule).resolve()
    if not resolved.is_file():
        issues.append(_issue("PLAN_MISSING_FIELD", "mutation_schedule", f"schedule file not found: {schedule}"))
        return None
    return resolved
def _validate_termination(termination: Any, issues: list[dict[str, str]]) -> None:
    if not isinstance(termination, dict) or "allow" not in termination:
        issues.append(_issue("PLAN_MISSING_FIELD", "termination.allow", "termination.allow is required"))
        return
    allowed = termination["allow"]
    if not isinstance(allowed, list) or not allowed:
        issues.append(_issue("PLAN_BAD_TERMINATION", "termination.allow", "allow must be a non-empty list"))
        return
    for value in allowed:
        if value not in TERMINATION_CONDITIONS:
            issues.append(
                _issue("PLAN_BAD_TERMINATION", "termination.allow", f"unknown termination condition {value!r}")
            )
def _validate_audit(audit: Any, issues: list[dict[str, str]]) -> None:
    if not isinstance(audit, dict):
        issues.append(_issue("PLAN_MISSING_FIELD", "audit", "audit must be a mapping"))
        return
    ratio = audit.get("max_dup_ratio", 0.0)
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
        issues.append(_issue("PLAN_BAD_LIMIT", "audit.max_dup_ratio", "max_dup_ratio must be between 0 and 1"))
    for field in ("allow_out_of_order", "allow_insert_extra"):
        if field in audit and not isinstance(audit[field], bool):
            issues.append(_issue("PLAN_MISSING_FIELD", f"audit.{field}", f"{field} must be boolean"))

def load_plan(path: str | Path) -> Plan:
    plan_path = Path(path).resolve()
    issues: list[dict[str, str]] = []
    if not plan_path.is_file():
        raise PlanError([_issue("PLAN_MISSING_FIELD", "plan", f"plan file not found: {path}")])
    raw_bytes = plan_path.read_bytes()
    try:
        raw = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise PlanError([_issue("PLAN_MISSING_FIELD", "plan", f"invalid YAML: {exc}")]) from exc
    if not isinstance(raw, dict):
        raise PlanError([_issue("PLAN_MISSING_FIELD", "plan", "plan root must be a mapping")])
    normalized: dict[str, Any] = dict(raw)
    for field in ("dataset", "source", "strategies", "mutation_schedule", "termination", "audit"):
        if field not in normalized:
            issues.append(_issue("PLAN_MISSING_FIELD", field, f"{field} is required"))
    _validate_dataset(normalized.get("dataset"), issues)
    _validate_source(normalized.get("source"), issues)
    _validate_strategies(normalized.get("strategies"), issues)
    _validate_termination(normalized.get("termination"), issues)
    _validate_audit(normalized.get("audit"), issues)
    schedule_path = None
    if "mutation_schedule" in normalized:
        schedule_path = _validate_schedule(plan_path, normalized["mutation_schedule"], issues)
    if isinstance(normalized.get("source"), dict):
        source = dict(normalized["source"])
        for key, value in DEFAULT_PARAMS.items():
            source.setdefault(key, value)
        normalized["source"] = source
    if isinstance(normalized.get("audit"), dict):
        audit = dict(normalized["audit"])
        audit.setdefault("max_dup_ratio", 0.0)
        audit.setdefault("allow_out_of_order", False)
        audit.setdefault("allow_insert_extra", True)
        normalized["audit"] = audit
    if isinstance(normalized.get("strategies"), dict):
        strategies = {}
        for name, cfg in normalized["strategies"].items():
            if isinstance(cfg, dict):
                item = dict(cfg)
                item.setdefault("max_pages", 10)
                strategies[name] = item
            else:
                strategies[name] = cfg
        normalized["strategies"] = strategies
    if issues:
        raise PlanError(issues)
    assert schedule_path is not None
    return Plan(
        raw=normalized,
        path=plan_path,
        sha256=sha256_bytes(raw_bytes),
        schedule_path=schedule_path,
        schedule_sha256=sha256_file(schedule_path),
    )

def plan_error_lines(exc: PlanError) -> list[str]:
    return [f"{item['code']} {item['path']}: {item['message']}" for item in exc.issues]
