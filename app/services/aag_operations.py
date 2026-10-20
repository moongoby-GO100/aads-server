"""AAG 운영 화면 조회 계약 (aag-operations-v1).

측정하지 못한 값은 `unknown`/null 이다. 0/0 을 100% 로, 마지막 성공을 최신으로,
skip 을 성공으로 바꾸지 않는다. 실제 스냅샷(aag_latest_pointers 의 authoritative
행)과 aag_scan_runs 에서만 값을 계산하고, DB 에 없는 컬럼은 있는 척하지 않는다.

스케줄러 상태 파일(STATE_DIR)은 호스트에만 있고 API 컨테이너에는 마운트되지 않는다.
그래서 `last_run` 은 적재 단계(aag_scan_runs)만 반영하며, 이 한계를 limitations 에
항상 적는다.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.db_pool import get_pool
from app.services.aag_brief_v2 import STALE_AFTER_MINUTES, _as_utc, snapshot_freshness

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "aag-operations-v1"
RUN_STUCK_MINUTES = 60
ERROR_TEXT_MAX = 300
REPUBLISH_GAP_MINUTES = 30
_SCHEMA_CACHE_TTL = 300.0

_POINTER_COLUMNS = {
    "aag_latest_pointers": {
        "snapshot_id", "observation_id", "run_id", "project", "repository_id", "target_ref",
        "governance_scope", "resolved_commit_sha", "expected_target_ref_head_sha",
        "generated_at", "verified_at",
    },
    "aag_graph_snapshots_v2": {"id", "project", "repository_id", "stats", "findings",
                               "finding_count", "publish_status"},
    "aag_snapshot_observations": {"id", "snapshot_id", "run_id", "project", "repository_id",
                                  "target_ref", "governance_scope", "resolved_commit_sha",
                                  "authoritative", "verification_status"},
}
_RUN_REQUIRED = {"id", "project", "repository_id", "target_ref", "governance_scope",
                 "result", "started_at"}
_RUN_OPTIONAL = ("finished_at", "error_code", "error_detail")

_RUN_STATUS = {
    "succeeded": "success", "no_change_success": "success",
    "scan_failed": "failed", "ingest_failed": "failed",
    "skipped_locked": "skipped", "cancelled": "cancelled",
    "out_of_order": "rejected", "source_behind": "rejected",
    "running": "running",
}

_schema_cache: dict[str, Any] = {"at": 0.0, "columns": None}


class AAGOperationsError(Exception):
    """A client-visible, typed operations-view failure."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _iso(value: Any) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    # asyncpg returns json/jsonb as text unless a custom codec is installed.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, Mapping) else {}


def build_freshness(pointer: Mapping[str, Any] | None, now: datetime) -> tuple[dict[str, Any], list[str]]:
    """Freshness comes only from the authoritative pointer, never from run attempts."""
    notes: list[str] = []
    if not pointer:
        return {"status": "unknown", "verified_at": None, "age_minutes": None,
                "generated_at": None, "scan_age_minutes": None,
                "stale_after_minutes": STALE_AFTER_MINUTES}, notes
    verified = snapshot_freshness(pointer.get("verified_at"), now)
    scanned = snapshot_freshness(pointer.get("generated_at"), now)
    statuses = {verified["status"], scanned["status"]}
    if "stale" in statuses:
        status = "stale"
    elif verified["status"] == "unknown":
        status = "unknown"
    else:
        status = "fresh"
    age, scan_age = verified["age_minutes"], scanned["age_minutes"]
    if scan_age is not None and age is not None and scan_age - age > REPUBLISH_GAP_MINUTES:
        notes.append(
            "verified_at 은 재적재 시각이다 — 스캔(generated_at)은 %d분 전이며 "
            "신선도는 스캔 시각까지 함께 본다" % scan_age
        )
    if scanned["status"] == "unknown":
        notes.append("스냅샷 generated_at 을 확인할 수 없어 스캔 시각 신선도는 미측정이다")
    return {
        "status": status, "verified_at": _iso(pointer.get("verified_at")),
        "age_minutes": age, "generated_at": _iso(pointer.get("generated_at")),
        "scan_age_minutes": scan_age, "stale_after_minutes": STALE_AFTER_MINUTES,
    }, notes


def build_last_run(row: Mapping[str, Any] | None, now: datetime,
                   columns_missing: tuple[str, ...] = ()) -> dict[str, Any]:
    if not row:
        return {"status": "unknown", "finished_at": None, "error": None,
                "reason": "실행 이력을 수집하지 못했다 (aag_scan_runs 에 해당 ref 의 행이 없다)"}
    result = str(row.get("result") or "")
    status = _RUN_STATUS.get(result, "unknown")
    started = _as_utc(row.get("started_at"))
    reason: str | None = None
    if status == "running" and started is not None:
        minutes = int((now - started).total_seconds() / 60)
        if minutes > RUN_STUCK_MINUTES:
            status, reason = "running_stale", "적재가 %d분째 끝나지 않았다" % minutes
        else:
            reason = "적재 진행 중 (%d분 경과)" % max(0, minutes)
    elif status == "unknown":
        reason = "알 수 없는 result 값: %s" % (result[:60] or "(빈 값)")
    error_code = row.get("error_code") if "error_code" not in columns_missing else None
    error_detail = row.get("error_detail") if "error_detail" not in columns_missing else None
    if status in ("failed", "rejected"):
        reason = str(error_detail)[:ERROR_TEXT_MAX] if error_detail else (result or None)
    elif status == "skipped":
        reason = "다른 실행이 lock 을 잡고 있어 건너뜀 — 성공도 신선도 증거도 아니다"
    elif status == "cancelled":
        reason = "취소됨"
    error = str(error_code)[:100] if error_code else (result if status in ("failed", "rejected") else None)
    return {"status": status, "finished_at": _iso(row.get("finished_at")),
            "error": error, "reason": reason}


def build_debt(severity_counts: Mapping[str, Any] | None) -> dict[str, Any]:
    if severity_counts is None:
        return {"status": "unknown", "p0": None, "p1": None, "p2": None, "p3": None,
                "unclassified": None}
    counts = {str(k).upper(): _int_or_none(v) or 0 for k, v in severity_counts.items()}
    p = {name: counts.get(name.upper(), 0) for name in ("P0", "P1", "P2", "P3")}
    unclassified = sum(v for k, v in counts.items() if k not in p)
    if p["P0"] > 0:
        status = "blocked"
    elif p["P1"] > 0:
        status = "attention"
    elif p["P2"] or p["P3"] or unclassified:
        status = "watch"
    else:
        status = "none_detected"
    return {"status": status, "p0": p["P0"], "p1": p["P1"], "p2": p["P2"], "p3": p["P3"],
            "unclassified": unclassified}


def build_coverage(stats: Any) -> tuple[dict[str, Any], list[str]]:
    """0/0 is unmeasured, not 100%; complete requires a measured, fully scanned inventory and risk set."""
    notes: list[str] = []
    cov = _mapping(_mapping(stats).get("coverage"))
    inv_s, inv_t = _int_or_none(cov.get("inventory_scanned")), _int_or_none(cov.get("inventory_total"))
    risk_s, risk_t = _int_or_none(cov.get("risk_scanned")), _int_or_none(cov.get("risk_total"))
    inv_ok = inv_s is not None and inv_t is not None and inv_t > 0 and inv_s <= inv_t
    risk_ok = risk_s is not None and risk_t is not None and risk_t > 0 and risk_s <= risk_t
    result = {
        "inventory_scanned": inv_s if inv_ok else None, "inventory_total": inv_t if inv_ok else None,
        "risk_scanned": risk_s if risk_ok else None, "risk_total": risk_t if risk_ok else None,
    }
    if not inv_ok and not risk_ok:
        status = "unknown"
        notes.append("커버리지가 측정되지 않았다 (stats.coverage 없음, 0/0 또는 불일치)")
    elif inv_ok and risk_ok and inv_s == inv_t and risk_s == risk_t:
        status = "complete"
    else:
        status = "partial"
        if not inv_ok or not risk_ok:
            notes.append("inventory 와 risk 중 한쪽만 측정되었다")
    unobserved = [str(item) for item in (cov.get("unobserved_scopes") or []) if str(item).strip()]
    if unobserved:
        status = "partial" if status != "unknown" else status
        notes.append("미관측 범위: " + ", ".join(sorted(set(unobserved))[:10]))
    return {"status": status, **result}, notes


def build_recovery_actions(
    *, project: str, freshness: Mapping[str, Any], last_run: Mapping[str, Any],
    debt: Mapping[str, Any], coverage: Mapping[str, Any], has_snapshot: bool,
) -> list[dict[str, Any]]:
    """Evidence only. Nothing here executes; rescans stay behind approval."""
    evidence: list[str] = []
    if not has_snapshot:
        evidence.append("authoritative 스냅샷 없음")
    if freshness["status"] == "stale":
        evidence.append("신선도 stale (검증 %s분, 스캔 %s분 전)" % (
            freshness.get("age_minutes"), freshness.get("scan_age_minutes")))
    elif freshness["status"] == "unknown":
        evidence.append("신선도 미측정")
    if last_run["status"] in ("failed", "rejected", "running_stale", "skipped"):
        evidence.append("최근 실행 %s" % last_run["status"])
    rescan_reason = "승인 필요 — 이 API 는 재스캔을 실행하지 않는다. 근거: " + (
        "; ".join(evidence) if evidence else "재스캔을 정당화하는 근거 없음 (fresh, 최근 실행 정상)")
    if project == "GO100":
        rescan_reason += " · GO100 원격 scan 은 평일 08:00~15:40 KST 회피 정책이 적용된다"

    failed = last_run["status"] in ("failed", "rejected", "running_stale")
    urgent = (debt.get("p0") or 0) + (debt.get("p1") or 0)
    gap = coverage["status"] in ("partial", "unknown")
    return [
        {"kind": "request_rescan", "label": "재스캔 요청 (승인 필요)", "enabled": False,
         "reason": rescan_reason},
        {"kind": "inspect_last_failure", "label": "최근 실패 원인 확인", "enabled": failed,
         "reason": (last_run.get("reason") or last_run.get("error") or "실패 원인 기록 없음")
         if failed else "최근 실행이 실패 상태가 아니다 (%s)" % last_run["status"]},
        {"kind": "review_debt", "label": "P0/P1 결함 검토", "enabled": urgent > 0,
         "reason": ("P0 %s건, P1 %s건 열림" % (debt.get("p0"), debt.get("p1")))
         if urgent > 0 else "열린 P0/P1 이 없다 (debt=%s)" % debt["status"]},
        {"kind": "expand_coverage", "label": "커버리지 공백 점검", "enabled": gap,
         "reason": ("커버리지 %s" % coverage["status"]) if gap else "커버리지 완료"},
    ]


def build_operations_view(
    *, project: str, repository_id: str, target_ref: str, governance_scope: str = "default",
    pointer: Mapping[str, Any] | None, severity_counts: Mapping[str, Any] | None,
    last_run: Mapping[str, Any] | None, now: datetime | None = None,
    run_columns_missing: tuple[str, ...] = (),
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    freshness, fresh_notes = build_freshness(pointer, now)
    run = build_last_run(last_run, now, run_columns_missing)
    debt = build_debt(severity_counts if pointer else None)
    coverage, cov_notes = build_coverage(_mapping(pointer).get("stats") if pointer else None)
    if not pointer:
        coverage = {"status": "unknown", "inventory_scanned": None, "inventory_total": None,
                    "risk_scanned": None, "risk_total": None}
    limitations = [
        "last_run 은 적재 단계(aag_scan_runs)만 반영한다 — 스케줄러의 스캔 실패·skip·장중 회피는 "
        "호스트 상태 파일에 있고 이 API 는 읽지 않는다",
        "debt 는 스냅샷 원본 결함 수이며 승인된 예외(aag_finding_exceptions)를 차감하지 않았다",
    ]
    if not pointer:
        limitations.append("이 ref 에 authoritative 스냅샷이 없어 freshness/debt/coverage 는 미측정이다")
    if debt.get("unclassified"):
        limitations.append("severity 가 P0~P3 가 아닌 결함 %d건은 unclassified 로만 센다" % debt["unclassified"])
    if run_columns_missing:
        limitations.append("aag_scan_runs 에 없는 컬럼: " + ", ".join(run_columns_missing))
    limitations += fresh_notes + cov_notes
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "repository_id": repository_id,
        "target_ref": target_ref,
        "governance_scope": governance_scope,
        "snapshot_id": str(pointer["snapshot_id"]) if pointer and pointer.get("snapshot_id") else None,
        "commit_sha": (pointer.get("resolved_commit_sha") if pointer else None),
        "evaluated_at": now.isoformat(),
        "axes": {"freshness": freshness, "last_run": run, "debt": debt, "coverage": coverage},
        "recovery_actions": build_recovery_actions(
            project=project, freshness=freshness, last_run=run, debt=debt,
            coverage=coverage, has_snapshot=bool(pointer)),
        "limitations": list(dict.fromkeys(limitations)),
    }


_POINTER_SQL = """
SELECT p.snapshot_id, p.observation_id, p.run_id, p.project, p.repository_id,
       p.target_ref, p.governance_scope, p.resolved_commit_sha,
       p.expected_target_ref_head_sha, p.generated_at, p.verified_at,
       s.stats, s.finding_count,
       CASE WHEN jsonb_typeof(s.findings) = 'array' THEN (
            SELECT COALESCE(jsonb_object_agg(sev, n), '{}'::jsonb) FROM (
                SELECT COALESCE(upper(f->>'severity'), 'UNCLASSIFIED') AS sev, count(*) AS n
                  FROM jsonb_array_elements(s.findings) f GROUP BY 1) t)
            ELSE NULL END AS severity_counts
  FROM aag_latest_pointers p
  JOIN aag_graph_snapshots_v2 s
    ON s.id=p.snapshot_id AND s.project=p.project AND s.repository_id=p.repository_id
  JOIN aag_snapshot_observations o
    ON o.id=p.observation_id AND o.snapshot_id=p.snapshot_id AND o.run_id=p.run_id
   AND o.project=p.project AND o.repository_id=p.repository_id
   AND o.target_ref=p.target_ref AND o.governance_scope=p.governance_scope
   AND o.resolved_commit_sha=p.resolved_commit_sha
 WHERE p.project=$1 AND p.repository_id=$2 AND p.target_ref=$3 AND p.governance_scope=$4
   AND s.publish_status='ready' AND o.authoritative=TRUE AND o.verification_status='verified'
"""

_COLUMNS_SQL = """
SELECT table_name, column_name FROM information_schema.columns
 WHERE table_schema = current_schema()
   AND table_name = ANY($1::text[])
"""


async def _columns(conn: Any) -> dict[str, set[str]]:
    cached = _schema_cache["columns"]
    if cached is not None and time.monotonic() - _schema_cache["at"] < _SCHEMA_CACHE_TTL:
        return cached
    tables = sorted([*_POINTER_COLUMNS, "aag_scan_runs"])
    rows = await conn.fetch(_COLUMNS_SQL, tables)
    columns: dict[str, set[str]] = {}
    for row in rows:
        columns.setdefault(row["table_name"], set()).add(row["column_name"])
    _schema_cache.update(at=time.monotonic(), columns=columns)
    return columns


def reset_schema_cache() -> None:
    _schema_cache.update(at=0.0, columns=None)


def _missing_schema(columns: dict[str, set[str]]) -> list[str]:
    missing = []
    for table, needed in _POINTER_COLUMNS.items():
        for column in sorted(needed - columns.get(table, set())):
            missing.append(f"{table}.{column}")
    for column in sorted(_RUN_REQUIRED - columns.get("aag_scan_runs", set())):
        missing.append(f"aag_scan_runs.{column}")
    return missing


async def load_operations_view(
    *, project: str, repository_id: str, target_ref: str, governance_scope: str = "default",
    now: datetime | None = None,
) -> dict[str, Any]:
    project = project.strip().upper()
    repository_id, target_ref = repository_id.strip(), target_ref.strip()
    governance_scope = governance_scope.strip() or "default"
    if not repository_id or not target_ref:
        raise AAGOperationsError("invalid_query", "repository_id 와 target_ref 가 필요하다", 400)
    try:
        async with get_pool().acquire() as conn:
            columns = await _columns(conn)
            missing = _missing_schema(columns)
            if missing:
                raise AAGOperationsError(
                    "schema_unavailable", "AAG v2 스키마가 준비되지 않았다: " + ", ".join(missing[:8]), 503)
            pointer = await conn.fetchrow(_POINTER_SQL, project, repository_id, target_ref, governance_scope)
            run_cols = columns.get("aag_scan_runs", set())
            optional = [c for c in _RUN_OPTIONAL if c in run_cols]
            run_missing = tuple(c for c in _RUN_OPTIONAL if c not in run_cols)
            select = ", ".join(["id", "result", "started_at", *optional])
            run = await conn.fetchrow(
                f"SELECT {select} FROM aag_scan_runs "  # noqa: S608 - column names come from a fixed whitelist
                "WHERE project=$1 AND repository_id=$2 AND target_ref=$3 AND governance_scope=$4 "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                project, repository_id, target_ref, governance_scope,
            )
    except AAGOperationsError:
        raise
    except Exception as exc:
        logger.exception("AAG operations view query failed")
        raise AAGOperationsError("operations_unavailable", "AAG 운영 상태를 조회하지 못했다", 503) from exc
    if pointer is None and run is None:
        raise AAGOperationsError(
            "snapshot_not_found", "이 project/repository/ref 의 AAG v2 스냅샷도 실행 이력도 없다", 404)
    pointer_data = dict(pointer) if pointer else None
    return build_operations_view(
        project=project, repository_id=repository_id, target_ref=target_ref,
        governance_scope=governance_scope, pointer=pointer_data,
        severity_counts=_mapping(pointer_data.get("severity_counts")) if pointer_data
        and pointer_data.get("severity_counts") is not None else None,
        last_run=dict(run) if run else None, now=now, run_columns_missing=run_missing,
    )
