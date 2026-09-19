"""Safety-first AAG v2 brief and coverage contract.

The brief never turns absence of a finding into an absence-of-impact claim.  It
also keeps the source snapshot identity pinned so a consumer can reproduce the
statement against the same repository/ref/commit.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

UNSAFE_STATES = {
    "not_detected": "inspect the target manually and run the required analyzer",
    "outside_scope": "expand scan scope before deciding impact",
    "partial": "complete the missing inventory before deciding impact",
    "stale": "refresh the pinned ref and regenerate the brief",
    "fallback": "restore central authoritative evidence and revalidate",
    "mismatch": "resolve commit/ref mismatch and rescan",
    "truncated": "retrieve the complete pinned result before deciding impact",
}


@dataclass(frozen=True)
class Coverage:
    inventory_scanned: int
    inventory_total: int
    inventory_ratio: float
    risk_scanned: int
    risk_total: int
    risk_ratio: float
    unobserved_scopes: tuple[str, ...]


def _nonnegative(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _ratio(scanned: int, total: int) -> float:
    return round(scanned / total, 4) if total else 0.0


def coverage_from_snapshot(snapshot: Mapping[str, Any]) -> Coverage:
    stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), Mapping) else {}
    coverage = stats.get("coverage") if isinstance(stats.get("coverage"), Mapping) else {}
    inventory_total = _nonnegative(coverage.get("inventory_total"))
    inventory_scanned = min(_nonnegative(coverage.get("inventory_scanned")), inventory_total)
    risk_total = _nonnegative(coverage.get("risk_total"))
    risk_scanned = min(_nonnegative(coverage.get("risk_scanned")), risk_total)
    unobserved = coverage.get("unobserved_scopes") or []
    return Coverage(
        inventory_scanned=inventory_scanned,
        inventory_total=inventory_total,
        inventory_ratio=_ratio(inventory_scanned, inventory_total),
        risk_scanned=risk_scanned,
        risk_total=risk_total,
        risk_ratio=_ratio(risk_scanned, risk_total),
        unobserved_scopes=tuple(sorted({str(item) for item in unobserved if str(item).strip()})),
    )


def build_brief_v2(
    *, snapshot: Mapping[str, Any], target: str, findings: Sequence[Mapping[str, Any]],
    status: str, now: datetime | None = None, stale_after_minutes: int = 180,
) -> dict[str, Any]:
    """Build a pinned brief with explicit limits and recovery actions."""
    now = now or datetime.now(UTC)
    verified_at = snapshot.get("verified_at")
    if isinstance(verified_at, str):
        verified_at = datetime.fromisoformat(verified_at)
    age_minutes = None
    if isinstance(verified_at, datetime):
        age_minutes = max(0, int((now - verified_at.astimezone(UTC)).total_seconds() / 60))

    state = str(status or "partial").strip().lower()
    resolved = str(snapshot.get("resolved_commit_sha") or "")
    expected = str(snapshot.get("expected_target_ref_head_sha") or "")
    source = str(snapshot.get("source") or "")
    authoritative = snapshot.get("authoritative") is True
    truncated = snapshot.get("truncated") is True
    if not authoritative or source != "central_db":
        state = "fallback"
    elif not resolved or resolved != expected:
        state = "mismatch"
    elif age_minutes is None or age_minutes > stale_after_minutes:
        state = "stale"
    elif truncated:
        state = "truncated"

    coverage = coverage_from_snapshot(snapshot)
    actions: list[str] = []
    if state in UNSAFE_STATES:
        actions.append(UNSAFE_STATES[state])
    if coverage.inventory_ratio < 1:
        actions.append("close inventory coverage gaps")
    if coverage.risk_ratio < 1:
        actions.append("inspect uncovered risk-weighted paths")
    if coverage.unobserved_scopes:
        actions.append("review unobserved scopes: " + ", ".join(coverage.unobserved_scopes))
    actions = list(dict.fromkeys(actions))

    analyzer = snapshot.get("analyzer") if isinstance(snapshot.get("analyzer"), Mapping) else {}
    return {
        "schema_version": "aag-brief-v2",
        "target": target,
        "state": state,
        "conclusion": "findings_present" if findings else "not_proven",
        "negative_assertion_allowed": False,
        "snapshot": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "observation_id": snapshot.get("observation_id"),
            "run_id": snapshot.get("run_id"),
            "project": snapshot.get("project"),
            "repository_id": snapshot.get("repository_id"),
            "target_ref": snapshot.get("target_ref"),
            "governance_scope": snapshot.get("governance_scope", "default"),
            "resolved_commit_sha": resolved,
            "expected_target_ref_head_sha": expected,
        },
        "analyzer": {
            "name": analyzer.get("name"),
            "version": analyzer.get("version"),
            "ruleset_digest": analyzer.get("ruleset_digest"),
            "scan_scope_digest": analyzer.get("scan_scope_digest"),
        },
        "coverage": asdict(coverage),
        "findings": [dict(item) for item in findings],
        "freshness": {"verified_at": verified_at, "age_minutes": age_minutes},
        "limitations": actions,
        "required_actions": actions,
        "miss_review_required": not findings or bool(actions),
    }
