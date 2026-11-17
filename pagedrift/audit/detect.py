"""Finding extraction rules, kept separate from report assembly."""
from __future__ import annotations
from collections import defaultdict
from typing import Any
from ..models import FINDING_CODES, Finding
from .extras import observed_extras
AUDIT_CODE_ORDER = FINDING_CODES + ("RESP_INVALID",)

def _evidence_index(requests: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["seq"]): row for row in requests}
def _sha8_for(seq: int, by_seq: dict[int, dict[str, Any]]) -> str:
    row = by_seq.get(seq)
    return str(row.get("sha256", ""))[:8] if row else ""
def _locate_page(sort_key: int, requests: list[dict[str, Any]]) -> int:
    valid = [row for row in requests if row.get("valid") and row.get("returned", 0) > 0]
    for row in valid:
        first_key, last_key = row.get("first_key"), row.get("last_key")
        if first_key is not None and last_key is not None and first_key <= sort_key <= last_key:
            return int(row["seq"])
    for row in valid:
        if row.get("last_key") is not None and int(row["last_key"]) >= sort_key:
            return int(row["seq"])
    if valid:
        return int(valid[-1]["seq"])
    return int(requests[-1]["seq"]) if requests else 0
def _finding(
    code: str,
    natural_key: str,
    page_seq: int,
    requests_by_seq: dict[int, dict[str, Any]],
    detail: str,
    *,
    is_defect: bool = True,
) -> Finding:
    return Finding(
        code=code,
        natural_key=natural_key,
        page_seq=page_seq,
        evidence_sha8=_sha8_for(page_seq, requests_by_seq),
        detail=detail,
        is_defect=is_defect,
    )

def integrity_findings(run: dict[str, Any], requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_seq = _evidence_index(requests)
    last_seq = int(requests[-1]["seq"]) if requests else 0
    natural_key = "run"
    findings: list[dict[str, Any]] = []
    if run.get("status") != "completed":
        findings.append(
            _finding(
                "RESP_INVALID",
                natural_key,
                last_seq,
                by_seq,
                "collector did not complete; last page contract or transport failed",
            ).to_dict()
        )
    if run.get("strategy") == "snapshot":
        tokens = [token for token in run.get("snapshot_tokens", []) if token]
        if not tokens:
            findings.append(
                _finding(
                    "RESP_INVALID",
                    natural_key,
                    last_seq,
                    by_seq,
                    "snapshot strategy did not observe a snapshot token",
                ).to_dict()
            )
        elif len(set(tokens)) > 1:
            findings.append(
                _finding(
                    "RESP_INVALID",
                    natural_key,
                    last_seq,
                    by_seq,
                    f"snapshot token changed during the run: {sorted(set(tokens))}",
                ).to_dict()
            )
    return findings

def drift_findings(
    initial_records: list[dict[str, Any]],
    records: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
    run: dict[str, Any],
) -> tuple[list[Finding], list[dict[str, Any]], dict[str, Any]]:
    by_seq = _evidence_index(requests)
    initial_by_id = {row["id"]: row for row in initial_records}
    observed_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        observed_by_id[str(row["id"])].append(row)
    observed_ids = set(observed_by_id)
    max_observed = max((int(row["sort_key"]) for row in records), default=None)
    lower = min((int(row["sort_key"]) for row in initial_records), default=0)
    last_seq = int(requests[-1]["seq"]) if requests else 0
    findings: list[Finding] = []
    for row in initial_records:
        if row["id"] in observed_ids:
            continue
        key = int(row["sort_key"])
        if key >= lower and (max_observed is None or key <= max_observed):
            findings.append(
                _finding(
                    "DRIFT_SKIP",
                    str(row["id"]),
                    _locate_page(key, requests),
                    by_seq,
                    f"initial sort_key {key} lies in observed range but was never collected",
                )
            )
    duplicate_count = 0
    first_key_by_id: dict[str, int] = {}
    occurrence_by_id: dict[str, int] = defaultdict(int)
    for row in records:
        natural_key = str(row["id"])
        first_key_by_id.setdefault(natural_key, int(row["sort_key"]))
        occurrence_by_id[natural_key] += 1
        if occurrence_by_id[natural_key] > 1:
            duplicate_count += 1
            findings.append(
                _finding(
                    "DRIFT_DUP",
                    natural_key,
                    int(row.get("source_page_seq", 0)),
                    by_seq,
                    f"natural key observed {len(observed_by_id[natural_key])} times; "
                    f"this is occurrence {occurrence_by_id[natural_key]}",
                )
            )
    previous_key: int | None = None
    for row in records:
        key = int(row["sort_key"])
        natural_key = str(row["id"])
        first_key = first_key_by_id[natural_key]
        issues = []
        if previous_key is not None and key < previous_key:
            issues.append(f"sort_key decreased from {previous_key} to {key}")
        if key != first_key:
            issues.append(f"natural key reappeared with changed sort_key {first_key}->{key}")
        if issues:
            findings.append(
                _finding(
                    "DRIFT_OOR",
                    natural_key,
                    int(row.get("source_page_seq", 0)),
                    by_seq,
                    "; ".join(issues),
                )
            )
        previous_key = key
    unseen_above = [
        row
        for row in initial_records
        if row["id"] not in observed_ids
        and (max_observed is None or int(row["sort_key"]) > max_observed)
    ]
    for row in unseen_above:
        findings.append(
            _finding(
                "DRIFT_PREMATURE_TERM",
                str(row["id"]),
                last_seq,
                by_seq,
                f"collection stopped after max observed key {max_observed}; "
                f"initial sort_key {row['sort_key']} was still pending",
            )
        )
    observation = run.get("termination_observation", {})
    stopped_short = bool(observation.get("last_page_empty") or observation.get("last_page_short"))
    if stopped_short and unseen_above:
        first = unseen_above[0]
        findings.append(
            _finding(
                "DRIFT_TAIL_TRUNC",
                str(first["id"]),
                last_seq,
                by_seq,
                f"final page was empty/short but {len(unseen_above)} initial keys extend beyond "
                f"max observed key {max_observed}",
            )
        )
    extras = observed_extras(records, set(initial_by_id), mutations)
    for extra in extras:
        detail = (
            f"item appeared after collection start and was attributed to insert at request "
            f"{extra['mutation_request_seq']}"
            if extra["attributed_insert"]
            else "item appeared after collection start but has no insert mutation attribution"
        )
        findings.append(
            _finding(
                "DRIFT_INSERT_OBSERVED",
                str(extra["id"]),
                int(extra["source_page_seq"] or 0),
                by_seq,
                detail,
                is_defect=False,
            )
        )
    findings.sort(
        key=lambda item: (
            AUDIT_CODE_ORDER.index(item.code),
            item.natural_key,
            item.page_seq,
            item.detail,
        )
    )
    meta = {
        "observed_ids": observed_ids,
        "max_observed": max_observed,
        "duplicate_count": duplicate_count,
        "extras": extras,
        "unseen_above": unseen_above,
    }
    return findings, [finding.to_dict() for finding in findings], meta
