"""목표 파생 신호 — 활동 시각·휴면·우선순위 순번·자동승인 표시 상태·P0 상한.

정본: docs/prd/20261015_AADS_목표_우선순위_휴면정리_자동승인표시.md
(document_key=goal-priority-dormancy-autoapproval-display-prd, §3 PR-1·PR-2,
§4 DO-1·DO-2·DO-5, §5 AA-1~AA-7).

**저장 상태는 바꾸지 않는다.** 여기 있는 것은 전부 읽어서 계산하는 값이다.
휴면이라고 목표를 닫거나 지우는 경로는 두지 않는다(DO-5) — 표시만 하고,
닫을지는 대표님이 정한다.

`goals.updated_at` 은 활동 근거로 쓰지 않는다. 자동화(진행률 갱신·승인 사용량
기록 등)가 계속 건드려 멈춘 목표도 늘 "방금 갱신" 으로 보인다(2026-10-15 실측).
대신 아래 다섯 가지의 최신값을 쓴다.

    milestones          생성·착수·완료·보고 시각 (상태가 바뀐 시각)
    goal_task_links     → pipeline_jobs 생성·갱신·완료
    pipeline_jobs.goal_id 로 직접 귀속된 작업
    지시문 `GOAL_ID:` 줄로 귀속된 작업 (rev2: 연결 칸이 빈 러너 작업 322건 실측)
    goal_task_links(chat_session) 담당 세션 링크 생성·갱신
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

DORMANT_DAYS = 14
P0_CAP = 3
# 지시문 `GOAL_ID:` 스캔 범위. 목록 화면은 넓게, 게이트는 휴면 판정에 필요한 만큼만.
INSTRUCTION_SCAN_DAYS = 60
GATE_INSTRUCTION_SCAN_DAYS = DORMANT_DAYS + 1

DORMANT_INACTIVE = "inactive_14d"
DORMANT_BLOCKED_WITHOUT_REASON = "blocked_without_reason"

# 정리 카드 구분(rev2 DO-2): 차단 목표는 휴면 카드가 아니라 '차단 해소 또는 종료' 카드로 간다.
CLEANUP_DORMANT = "dormant"
CLEANUP_BLOCKED = "blocked_resolution"

OPEN_STATUSES = ("draft", "active", "blocked")

_PRIORITY_RANK = {"P0-CRITICAL": 0, "P0": 1, "P1": 2, "P2": 3, "P3": 4}
_UNKNOWN_PRIORITY_RANK = 9

# 자동승인 표시 상태(PRD §5 표). 화면 칩과 게이트 판정이 같은 값을 본다.
AA_MANUAL = "manual"
AA_HIGH = "high"
AA_ALL = "all"
AA_EXHAUSTED = "exhausted"
AA_OFF = "off"
AA_DELEGATED = "delegated"
AA_PAUSED = "paused"
AA_EXPIRED = "expired"

# goal_auto_approval_grants 중 게이트(live_trading_guard)가 보는 것.
GATE_GRANT_ACTION = "live_trading_gate"
# 이관 되돌림(down)으로 회수된 행은 "grant 없음" 으로 본다 → approval_policy 호환 경로.
ROLLBACK_REVOKE_REASON = "migration_rollback"

_UUID_RE = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def priority_rank(priority: Any) -> int:
    """정렬용 순번. 작을수록 먼저(P0-CRITICAL 0 · P0 1 · P1 2 · P2 3 · P3 4 · 그 외 9)."""
    return _PRIORITY_RANK.get(str(priority or "").strip().upper(), _UNKNOWN_PRIORITY_RANK)


def priority_rank_case_sql(column: str = "g.priority") -> str:
    """priority_rank() 와 같은 순번을 SQL 안에서 계산하는 CASE 식(상수 공유)."""
    whens = " ".join(f"WHEN '{key}' THEN {rank}" for key, rank in _PRIORITY_RANK.items())
    return (
        f"CASE upper(btrim(COALESCE({column}, ''))) {whens} "
        f"ELSE {_UNKNOWN_PRIORITY_RANK} END"
    )


def is_p0(priority: Any) -> bool:
    """P0 상한 집계 대상. P0-CRITICAL 도 P0 다(PRD §1: P0 30개 중 P0-CRITICAL 2)."""
    return str(priority or "").strip().upper().startswith("P0")


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def derive_dormancy(
    status: Any,
    last_activity_at: Any,
    *,
    has_block_reason: bool,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[str]]:
    """(dormant, dormant_reason). 열린 목표(draft/active/blocked)만 대상이다.

    - blocked 인데 차단 사유·해제 조건이 비어 있으면 blocked_without_reason (DO-2)
    - 마지막 활동이 14일을 **넘으면** inactive_14d (정확히 14일은 아직 아니다)
    - 활동 기록이 하나도 없으면 휴면으로 본다 — 근거 없는 목표가 맨 앞에 서면 안 된다.
    """
    state = str(status or "").strip().lower()
    if state not in OPEN_STATUSES:
        return False, None
    if state == "blocked" and not has_block_reason:
        return True, DORMANT_BLOCKED_WITHOUT_REASON
    current = now or datetime.now(timezone.utc)
    last = _aware(last_activity_at)
    if last is None or current - last > timedelta(days=DORMANT_DAYS):
        return True, DORMANT_INACTIVE
    return False, None


def cleanup_lane(status: Any, dormant: bool) -> Optional[str]:
    """정리 카드 구분(rev2 DO-2). 휴면이 아니면 None."""
    if not dormant:
        return None
    return CLEANUP_BLOCKED if str(status or "").lower() == "blocked" else CLEANUP_DORMANT


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "t", "yes", "y", "on", "1"}


def _iso(value: Any) -> Optional[str]:
    parsed = _aware(value)
    return parsed.isoformat() if parsed else None


def goal_is_suspended(status: Any, paused_at: Any, dormant: bool) -> bool:
    """자동승인 정지 사유가 목표 쪽에 있는가 (blocked/draft/paused/휴면, AA-2)."""
    return str(status or "").lower() != "active" or paused_at is not None or bool(dormant)


def auto_approval_view(
    *,
    status: Any,
    paused_at: Any,
    dormant: bool,
    policy: Optional[Mapping[str, Any]],
    grant: Optional[Mapping[str, Any]],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """칩에 그릴 자동승인 상태. 게이트와 같은 순서로 판정한다.

    grant(정본)가 있으면 grant 를, 없으면(이관 전) approval_policy 를 읽는다.
    """
    current = now or datetime.now(timezone.utc)
    closed = str(status or "").lower() not in OPEN_STATUSES
    suspended = goal_is_suspended(status, paused_at, dormant)
    if grant:
        conditions = grant.get("conditions") or {}
        if not isinstance(conditions, Mapping):
            conditions = {}
        view = {
            "source": "grant",
            "grant_id": str(grant.get("id") or "") or None,
            "used": _int(grant.get("used_executions")),
            "max": _int(grant.get("max_executions")),
            "expires_at": _iso(grant.get("expires_at")),
            "set_by": conditions.get("set_by") or None,
            "set_at": conditions.get("set_at") or _iso(grant.get("issued_at")),
            "max_risk_tier": grant.get("max_risk_tier"),
        }
        grant_status = str(grant.get("status") or "").lower()
        expires = _aware(grant.get("expires_at"))
        if closed or grant_status in {"revoked", "stale", "superseded"}:
            state = AA_OFF
        elif conditions.get("delegated_roles"):
            state = AA_DELEGATED
        elif suspended or grant_status == "paused":
            state = AA_PAUSED
        elif grant_status == "expired" or (expires is not None and expires <= current):
            state = AA_EXPIRED
        elif view["used"] >= view["max"]:
            state = AA_EXHAUSTED
        else:
            state = AA_ALL if str(grant.get("max_risk_tier") or "") == "A2" else AA_HIGH
        view["state"] = state
        return view

    data = policy if isinstance(policy, Mapping) else {}
    high = _flag(data.get("auto_approve_high"))
    critical = _flag(data.get("auto_approve_critical"))
    view = {
        "source": "approval_policy" if data else "none",
        "grant_id": None,
        "used": _int(data.get("used")),
        "max": _int(data.get("max_executions")),
        "expires_at": _iso(data.get("expires_at")),
        "set_by": data.get("set_by") or None,
        "set_at": data.get("set_at") or None,
        "max_risk_tier": "A2" if critical else ("A1" if high else None),
    }
    has_flags = "auto_approve_high" in data or "auto_approve_critical" in data
    expires = _aware(data.get("expires_at"))
    if not data:
        state = AA_MANUAL
    elif not has_flags:
        # 역할 키(db_ddl/deploy/…)만 있는 위임형. 게이트는 이 키를 읽지 않는다.
        state = AA_DELEGATED
    elif closed or not (high or critical):
        state = AA_OFF
    elif suspended:
        state = AA_PAUSED
    elif expires is not None and expires <= current:
        state = AA_EXPIRED
    elif view["used"] >= view["max"]:
        state = AA_EXHAUSTED
    else:
        state = AA_ALL if critical else AA_HIGH
    view["state"] = state
    return view


# ── SQL ─────────────────────────────────────────────────────────────────────
# 활동 시각. $1 = 목표 id 배열(uuid[]), $2 = 지시문 스캔 일수.
# 상관 서브쿼리(목표마다 다섯 번)는 실측 8.8초였다 — 목표 집합에 대해 한 번씩
# 집계해 붙인다(실측 0.27초, 2026-10-15 목표 69개·러너 작업 3,551건).
LAST_ACTIVITY_SQL = f"""
WITH g AS (SELECT id FROM goals WHERE id = ANY($1::uuid[])),
ms AS (
    SELECT m.goal_id AS gid,
           max(GREATEST(m.created_at, m.started_at, m.completed_at, m.reported_at)) AS t
      FROM milestones m JOIN g ON g.id = m.goal_id
     GROUP BY 1),
lj AS (
    SELECT l.goal_id AS gid, max(GREATEST(j.created_at, j.updated_at, j.completed_at)) AS t
      FROM goal_task_links l
      JOIN g ON g.id = l.goal_id
      JOIN pipeline_jobs j ON j.job_id = l.task_id
     WHERE l.task_type = 'pipeline_job'
     GROUP BY 1),
dj AS (
    SELECT j.goal_id AS gid, max(GREATEST(j.created_at, j.updated_at, j.completed_at)) AS t
      FROM pipeline_jobs j JOIN g ON g.id = j.goal_id
     GROUP BY 1),
tj AS (
    SELECT lower(substring(j.instruction FROM 'GOAL_ID:\\s*({_UUID_RE})')) AS gid,
           max(GREATEST(j.created_at, j.updated_at, j.completed_at)) AS t
      FROM pipeline_jobs j
     WHERE j.created_at > now() - make_interval(days => $2::int)
       AND j.instruction LIKE '%GOAL_ID:%'
     GROUP BY 1),
sl AS (
    SELECT l.goal_id AS gid, max(GREATEST(l.created_at, l.updated_at)) AS t
      FROM goal_task_links l JOIN g ON g.id = l.goal_id
     WHERE l.task_type = 'chat_session'
     GROUP BY 1),
br AS (
    SELECT DISTINCT ON (a.goal_id) a.goal_id AS gid, a.note
      FROM goal_status_audit a JOIN g ON g.id = a.goal_id
     WHERE a.new_status = 'blocked'
     ORDER BY a.goal_id, a.changed_at DESC)
SELECT g.id::text AS goal_id,
       GREATEST(ms.t, lj.t, dj.t, tj.t, sl.t) AS last_activity_at,
       NULLIF(btrim(COALESCE(br.note, '')), '') AS block_note
  FROM g
  LEFT JOIN ms ON ms.gid = g.id
  LEFT JOIN lj ON lj.gid = g.id
  LEFT JOIN dj ON dj.gid = g.id
  LEFT JOIN tj ON tj.gid = g.id::text
  LEFT JOIN sl ON sl.gid = g.id
  LEFT JOIN br ON br.gid = g.id
"""

# 목표별 최신 게이트 grant. $1 = 목표 id 배열. 이관 되돌림으로 회수된 행은 뺀다.
LATEST_GATE_GRANT_SQL = f"""
SELECT DISTINCT ON (gr.goal_id)
       gr.goal_id::text AS goal_id, gr.id::text AS id, gr.status, gr.max_risk_tier,
       gr.max_executions, gr.used_executions, gr.valid_from, gr.expires_at,
       gr.issued_at, gr.conditions, gr.policy_version::text AS policy_version,
       gr.grant_version, gr.revocation_epoch, gr.tenant_id::text AS tenant_id
  FROM goal_auto_approval_grants gr
 WHERE gr.goal_id = ANY($1::uuid[])
   AND '{GATE_GRANT_ACTION}' = ANY(gr.actions)
   AND gr.revoke_reason IS DISTINCT FROM '{ROLLBACK_REVOKE_REASON}'
 ORDER BY gr.goal_id, gr.issued_at DESC
"""


def has_block_reason(block_note: Any, paused_reason: Any) -> bool:
    """차단 사유·해제 조건이 어딘가 적혀 있는가.

    goals 에는 차단 사유 칸이 없다. 차단으로 바뀐 마지막 감사행의 note 와
    일시정지 사유(paused_reason) 를 사유로 본다 — 둘 다 비면 사유 없음.
    """
    return bool(str(block_note or "").strip() or str(paused_reason or "").strip())


async def fetch_activity(
    conn: Any, goal_ids: Sequence[str], *, scan_days: int = INSTRUCTION_SCAN_DAYS,
) -> dict[str, dict[str, Any]]:
    """{goal_id: {last_activity_at, block_note}}. 실패는 호출자가 다룬다."""
    ids = [str(g) for g in goal_ids if g]
    if not ids:
        return {}
    rows = await conn.fetch(LAST_ACTIVITY_SQL, ids, int(scan_days))
    return {
        str(r["goal_id"]): {
            "last_activity_at": r["last_activity_at"],
            "block_note": r["block_note"],
        }
        for r in rows
    }


async def fetch_latest_gate_grants(conn: Any, goal_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    ids = [str(g) for g in goal_ids if g]
    if not ids:
        return {}
    rows = await conn.fetch(LATEST_GATE_GRANT_SQL, ids)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = dict(r)
        conditions = item.get("conditions")
        if isinstance(conditions, str):
            try:
                item["conditions"] = json.loads(conditions)
            except ValueError:
                item["conditions"] = {}
        out[str(item["goal_id"])] = item
    return out


def _policy(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def build_signals(
    row: Mapping[str, Any],
    activity: Optional[Mapping[str, Any]],
    grant: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """GET /goals 한 행에 덧붙일 필드."""
    activity = activity or {}
    last = activity.get("last_activity_at")
    dormant, reason = derive_dormancy(
        row.get("status"), last,
        has_block_reason=has_block_reason(activity.get("block_note"), row.get("paused_reason")),
        now=now,
    )
    return {
        "priority_rank": priority_rank(row.get("priority")),
        "last_activity_at": _iso(last),
        "dormant": dormant,
        "dormant_reason": reason,
        "cleanup_lane": cleanup_lane(row.get("status"), dormant),
        "auto_approval": auto_approval_view(
            status=row.get("status"), paused_at=row.get("paused_at"), dormant=dormant,
            policy=_policy(row.get("approval_policy")), grant=grant, now=now,
        ),
    }


async def signals_for_rows(
    conn: Any, rows: Iterable[Mapping[str, Any]], *, now: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    """{goal_id: 신호}. 조회가 실패해도 목록은 내려가야 하므로 빈 근거로 계산한다."""
    rows = list(rows)
    ids = [str(r.get("id") or r.get("goal_id")) for r in rows]
    try:
        activity = await fetch_activity(conn, ids)
    except Exception as exc:  # noqa: BLE001 — 신호 실패가 목록을 막지 않는다
        logger.warning("goal_activity_lookup_failed: %s", str(exc)[:200])
        activity = {}
    try:
        grants = await fetch_latest_gate_grants(conn, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("goal_grant_lookup_failed: %s", str(exc)[:200])
        grants = {}
    return {
        gid: build_signals(r, activity.get(gid), grants.get(gid), now=now)
        for gid, r in zip(ids, rows)
    }


# ── P0 상한 (PR-1) ──────────────────────────────────────────────────────────
async def active_p0_goals(conn: Any, tenant_id: str, *, exclude_goal_id: str = "") -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id::text AS goal_id, title, project, priority FROM goals "
        "WHERE tenant_id = $1::uuid AND status = 'active' "
        "  AND upper(COALESCE(priority, '')) LIKE 'P0%' "
        "  AND id::text <> $2 "
        "ORDER BY created_at",
        tenant_id, exclude_goal_id or "",
    )
    return [dict(r) for r in rows]


def p0_cap_violation(
    *, current_priority: Any, current_status: Any,
    new_priority: Any, new_status: Any, others: Sequence[Mapping[str, Any]],
) -> bool:
    """이 변경으로 active P0 가 상한을 넘는가.

    이미 active P0 인 목표를 그대로 두는 변경(제목 수정 등)은 막지 않는다 —
    상한 초과 상태를 정리하는 것은 첫 정리(DO-6)의 일이지 다른 수정의 일이 아니다.
    """
    becomes = str(new_status or "").lower() == "active" and is_p0(new_priority)
    already = str(current_status or "").lower() == "active" and is_p0(current_priority)
    return becomes and not already and len(others) >= P0_CAP
