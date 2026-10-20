"""Safety-first AAG v2 brief and coverage contract.

The brief never turns absence of a finding into an absence-of-impact claim.  It
also keeps the source snapshot identity pinned so a consumer can reproduce the
statement against the same repository/ref/commit.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STALE_AFTER_MINUTES = 180
MAX_BRIEF_TEXT_FINDINGS = 30
_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_TARGET_CHARS = 50_000
MAX_TARGET_WORD = 300
_TARGET_WORD_SPLIT = re.compile(r"[\s\"'`(),;<>\[\]{}|]+")
_TARGET_TOKEN = re.compile(
    r"/?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?"
    r"|[A-Za-z0-9_.\-]+/"
    r"|[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*\.[A-Za-z0-9]{1,8}"
)

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


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age_minutes(verified_at: datetime | None, now: datetime) -> int | None:
    if verified_at is None:
        return None
    return max(0, int((now - verified_at).total_seconds() / 60))


def snapshot_freshness(
    verified_at: Any, now: datetime | None = None,
    stale_after_minutes: int = STALE_AFTER_MINUTES,
) -> dict[str, Any]:
    """Classify snapshot age; an unverifiable timestamp is never reported fresh."""
    age = _age_minutes(_as_utc(verified_at), now or datetime.now(UTC))
    if age is None:
        status = "unknown"
    else:
        status = "stale" if age > stale_after_minutes else "fresh"
    return {"status": status, "age_minutes": age, "stale_after_minutes": stale_after_minutes}


def _unsafe_snapshot_state(
    snapshot: Mapping[str, Any], age_minutes: int | None, stale_after_minutes: int,
) -> str | None:
    resolved = str(snapshot.get("resolved_commit_sha") or "")
    expected = str(snapshot.get("expected_target_ref_head_sha") or "")
    if snapshot.get("authoritative") is not True or str(snapshot.get("source") or "") != "central_db":
        return "fallback"
    if not resolved or resolved != expected:
        return "mismatch"
    if age_minutes is None or age_minutes > stale_after_minutes:
        return "stale"
    if snapshot.get("truncated") is True:
        return "truncated"
    return None


def _coverage_actions(coverage: Coverage) -> list[str]:
    actions: list[str] = []
    if coverage.inventory_ratio < 1:
        actions.append("close inventory coverage gaps")
    if coverage.risk_ratio < 1:
        actions.append("inspect uncovered risk-weighted paths")
    if coverage.unobserved_scopes:
        actions.append("review unobserved scopes: " + ", ".join(coverage.unobserved_scopes))
    return actions


def snapshot_limitations(
    snapshot: Mapping[str, Any], now: datetime | None = None,
    stale_after_minutes: int = STALE_AFTER_MINUTES,
) -> list[str]:
    """Reasons a read of this snapshot must not be presented as proof of absence."""
    now = now or datetime.now(UTC)
    age = _age_minutes(_as_utc(snapshot.get("verified_at")), now)
    state = _unsafe_snapshot_state(snapshot, age, stale_after_minutes)
    actions = [UNSAFE_STATES[state]] if state else []
    actions += _coverage_actions(coverage_from_snapshot(snapshot))
    if snapshot.get("unresolved"):
        actions.append("resolve unresolved references before deciding impact")
    return list(dict.fromkeys(actions))


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").replace(":", "/")


def _repo_relative(raw: str, repository_id: str | None) -> str | None:
    """Repository-relative form of one path-like token, or None if it is not ours.

    ``./app/x.py`` and ``app/`` normalize to ``app/x.py`` and ``app``.  An absolute
    path is re-rooted only when it lies under this server's checkout or contains the
    snapshot's own ``repository_id`` directory; any other absolute path (another
    repository, ``/etc``, or an API route like ``/api/v1/x``) keeps its old
    root-less form, which cannot reach a repo file.  ``..`` is never resolved.
    """
    absolute = raw.startswith("/")
    segments = [seg for seg in raw.split("/") if seg and seg != "."]
    if ".." in segments:
        return None
    if absolute:
        rest: list[str] | None = None
        root_parts = REPO_ROOT.parts[1:]
        if tuple(segments[: len(root_parts)]) == root_parts:
            rest = segments[len(root_parts):]
        elif repository_id:
            ident = repository_id.strip().lower()
            for index, seg in enumerate(segments):
                if seg.lower() == ident:
                    rest = segments[index + 1:]
                    break
        if rest is not None:
            segments = rest
    return "/".join(segments) or None


def target_tokens(target: str, repository_id: str | None = None) -> list[str]:
    """Repo-relative path tokens named by a target (a file path or free-form instruction).

    Words are scanned one at a time and oversized ones are skipped, so a hostile
    instruction cannot drive the token regex into quadratic backtracking.
    """
    text = (target or "")[:MAX_TARGET_CHARS].strip().replace("\\", "/")
    tokens: set[str] = set()
    for word in _TARGET_WORD_SPLIT.split(text):
        if 0 < len(word) <= MAX_TARGET_WORD:
            for match in _TARGET_TOKEN.finditer(word):
                token = _repo_relative(match.group(0), repository_id)
                if token and len(token) >= 3:
                    tokens.add(token)
    return sorted(tokens)


def match_findings(
    findings: Iterable[Mapping[str, Any]], target: str, repository_id: str | None = None,
) -> list[Mapping[str, Any]]:
    """Findings whose file/module/key lies at or under a path the target names.

    Matching is segment-aware: ``aag.py`` matches ``app/api/aag.py`` but not
    ``app/api/aag.pyc`` or ``data_aag.py``, and ``app/api`` matches everything below it.
    """
    tokens = [f"/{token}/" for token in target_tokens(target, repository_id)]
    if not tokens:
        return []
    matches = []
    for finding in findings:
        identities = (
            finding.get("file"), finding.get("module"), finding.get("path"),
            finding.get("key"), finding.get("semantic_target"),
        )
        haystacks = [f"/{_normalize_path(value).strip('/')}/" for value in identities if value]
        if any(token in haystack for token in tokens for haystack in haystacks):
            matches.append(finding)
    return matches


def brief_status(matches: Sequence[Any], unresolved: Sequence[Any] | None) -> str:
    """Raw match status; absence is only ``not_detected``, never a clean bill."""
    if matches:
        return "detected"
    return "partial" if unresolved else "not_detected"


def brief_snapshot_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map an authoritative pointer row to the identity ``build_brief_v2`` pins."""
    return {
        "snapshot_id": str(row["snapshot_id"]),
        "observation_id": str(row["observation_id"]),
        "run_id": str(row["run_id"]),
        "project": row["project"],
        "repository_id": row["repository_id"],
        "target_ref": row["target_ref"],
        "governance_scope": row["governance_scope"],
        "resolved_commit_sha": row["resolved_commit_sha"],
        "expected_target_ref_head_sha": row["expected_target_ref_head_sha"],
        "source": "central_db",
        "authoritative": True,
        "verified_at": row["verified_at"],
        "analyzer": {
            "name": "scan_aads",
            "version": row["scanner_version"],
            "ruleset_digest": row["ruleset_digest"],
            "scan_scope_digest": row["scan_scope_digest"],
        },
        "stats": row.get("stats") or {},
        "truncated": False,
    }


def build_brief_v2(
    *, snapshot: Mapping[str, Any], target: str, findings: Sequence[Mapping[str, Any]],
    status: str, now: datetime | None = None, stale_after_minutes: int = STALE_AFTER_MINUTES,
) -> dict[str, Any]:
    """Build a pinned brief with explicit limits and recovery actions."""
    now = now or datetime.now(UTC)
    verified_at = _as_utc(snapshot.get("verified_at"))
    age_minutes = _age_minutes(verified_at, now)

    state = str(status or "partial").strip().lower()
    state = _unsafe_snapshot_state(snapshot, age_minutes, stale_after_minutes) or state
    resolved = str(snapshot.get("resolved_commit_sha") or "")
    expected = str(snapshot.get("expected_target_ref_head_sha") or "")

    coverage = coverage_from_snapshot(snapshot)
    actions: list[str] = []
    if state in UNSAFE_STATES:
        actions.append(UNSAFE_STATES[state])
    actions += _coverage_actions(coverage)
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


def render_brief_text(brief: Mapping[str, Any]) -> str:
    """Compact bullet rendering for a worker prompt; never states absence of impact."""
    snap = brief.get("snapshot") or {}
    cov = brief.get("coverage") or {}
    fresh = brief.get("freshness") or {}
    lines = [
        f"- 상태: {brief.get('state')} · 결론: {brief.get('conclusion')} · 부재/무영향 단정 금지",
        f"- 스냅샷: {snap.get('project')}/{snap.get('repository_id')} {snap.get('target_ref')} "
        f"commit={str(snap.get('resolved_commit_sha') or '')[:12]} "
        f"snapshot={snap.get('snapshot_id')} 검증 {fresh.get('age_minutes')}분 전",
        f"- 커버리지: inventory {cov.get('inventory_scanned')}/{cov.get('inventory_total')}, "
        f"risk {cov.get('risk_scanned')}/{cov.get('risk_total')}",
    ]
    lines += [f"- 한계/필요 조치: {item}" for item in brief.get("required_actions") or []]
    findings = sorted(
        brief.get("findings") or [],
        key=lambda item: _SEVERITY_RANK.get(str(item.get("severity", "")).upper(), len(_SEVERITY_RANK)),
    )
    shown, omitted = findings[:MAX_BRIEF_TEXT_FINDINGS], findings[MAX_BRIEF_TEXT_FINDINGS:]
    for finding in shown:
        location = finding.get("path") or finding.get("file") or finding.get("module") or "-"
        detail = str(finding.get("detail") or "").replace("\n", " ")[:160]
        lines.append(f"- [{finding.get('severity', '-')}] {finding.get('rule', '-')} `{location}` — {detail}")
    if omitted:
        by_severity: dict[str, int] = {}
        for finding in omitted:
            label = str(finding.get("severity") or "-")
            by_severity[label] = by_severity.get(label, 0) + 1
        counts = ", ".join(
            f"{label} {count}건" for label, count in sorted(
                by_severity.items(), key=lambda kv: (_SEVERITY_RANK.get(kv[0].upper(), len(_SEVERITY_RANK)), kv[0]),
            )
        )
        urgent = any(_SEVERITY_RANK.get(label.upper(), 9) <= 1 for label in by_severity)
        lines.append(
            f"- 외 {len(omitted)}건 생략({counts}){' — P0/P1 포함, 전체 조회 필수' if urgent else ''} "
            f"snapshot={snap.get('snapshot_id')} ref={snap.get('target_ref')} "
            f"commit={str(snap.get('resolved_commit_sha') or '')[:12]} "
            f"(전체는 /aag/v2/findings?snapshot_id={snap.get('snapshot_id')})"
        )
    return "\n".join(lines)
