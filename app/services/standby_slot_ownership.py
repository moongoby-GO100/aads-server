"""스탠바이 슬롯이 쥐고 있는 채팅 실행을 릴리스 전에 보이게 한다.

2026-09-30 실측. 00:54~02:08 사이 블루/그린 컷오버가 다섯 번 있었고, 그 사이
새로 시작된 턴 13건은 **전부 그 시각의 active 슬롯**에서 시작됐다
(chat_turn_executions.owner_instance ↔ deploy_phase_events.nginx_cutover 대조).
막은 것은 새 턴이 아니라 **컷오버 전에 시작돼 27~31분을 간 턴**이었다 —
c7fee5ba(01:03 green 시작, 01:30 완료), 14f1f6de(01:08 green 시작, 01:39 종료).
01:09:47 컷오버로 green 이 스탠바이가 된 뒤에도 두 턴은 정상 실행 중이었고,
다음 릴리스(#5290~#5292)의 target_slot_drain 이 180초씩 기다리다 막혔다.

deploy.sh 가 그 턴을 끊지 않는 것은 옳다(무중단 원칙). 빠져 있던 것은
**배포가 180초를 태우고 실패하기 전에는 아무도 모른다**는 점이다. 여기서
그것을 미리 보이게 한다.

- ``list_standby_owned_executions`` — 지금 스탠바이 슬롯이 리스를 쥔 실행
- ``record_standby_holds`` — 임계(기본 10분)를 넘은 것을 ``deploy_standby_holds``
  에 남긴다. active 슬롯의 주기 작업이 부른다.
- ``approval_standby_warning`` — 승인하는 세션의 실행이 스탠바이를 쥐고 있으면
  승인 응답에 실을 경고. **승인을 막지 않는다.**

이 모듈은 실행을 끊거나 리스를 빼앗지 않는다. 읽고 기록만 한다.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 10분인 이유 — 컷오버 간격. 2026-09-30 새벽 api 컷오버 간격은 11~32분이었다.
# 10분을 넘긴 스탠바이 실행은 다음 릴리스와 겹칠 확률이 높다.
STANDBY_HOLD_THRESHOLD_SECONDS = max(
    60, int(os.getenv("AADS_STANDBY_HOLD_THRESHOLD_SECONDS", "600"))
)
STANDBY_HOLD_MONITOR_INTERVAL_SECONDS = max(
    15, int(os.getenv("AADS_STANDBY_HOLD_MONITOR_INTERVAL_SECONDS", "60"))
)
_LIST_LIMIT = 50

_ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS deploy_standby_holds (
    execution_id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    owner_instance text NOT NULL,
    active_instance text NOT NULL,
    execution_started_at timestamptz,
    first_observed_at timestamptz NOT NULL DEFAULT NOW(),
    last_observed_at timestamptz NOT NULL DEFAULT NOW(),
    held_seconds bigint NOT NULL DEFAULT 0,
    released_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_deploy_standby_holds_open
    ON deploy_standby_holds (last_observed_at DESC)
    WHERE released_at IS NULL;
"""

# 리스가 살아 있는 실행만 본다. 리스가 끊긴 것은 deploy.sh 의 stream reconcile
# 이 stale 로 따로 다루고, drain 을 막는 것은 살아 있는 쪽이다.
_STANDBY_OWNED_SQL = """
SELECT te.id::text AS execution_id,
       te.session_id::text AS session_id,
       te.owner_instance,
       te.status,
       te.started_at,
       te.heartbeat_at,
       GREATEST(0, EXTRACT(EPOCH FROM (NOW() - te.started_at)))::bigint AS held_seconds
  FROM chat_turn_executions te
 WHERE te.status IN ('running', 'retrying')
   AND te.completed_at IS NULL
   AND te.owner_instance IS NOT NULL
   AND te.owner_instance <> $1
   AND te.lease_expires_at IS NOT NULL
   AND te.lease_expires_at > NOW()
   AND te.started_at <= NOW() - make_interval(secs => $2::int)
   AND ($3::uuid IS NULL OR te.session_id = $3::uuid)
 ORDER BY te.started_at
 LIMIT $4
"""

_UPSERT_SQL = """
INSERT INTO deploy_standby_holds (
    execution_id, session_id, owner_instance, active_instance,
    execution_started_at, held_seconds
)
VALUES (
    $1::uuid, $2::uuid, $3, $4,
    (SELECT started_at FROM chat_turn_executions WHERE id = $1::uuid), $5
)
ON CONFLICT (execution_id) DO UPDATE
   SET owner_instance = EXCLUDED.owner_instance,
       active_instance = EXCLUDED.active_instance,
       held_seconds = EXCLUDED.held_seconds,
       last_observed_at = NOW(),
       released_at = NULL
"""

# 이번 관측에 없는 열린 기록은 풀린 것이다(완료, 또는 그 슬롯이 다시 active).
_RELEASE_SQL = """
UPDATE deploy_standby_holds
   SET released_at = NOW()
 WHERE released_at IS NULL
   AND NOT (execution_id = ANY($1::uuid[]))
"""

_OPEN_HOLDS_SQL = """
SELECT execution_id::text AS execution_id,
       session_id::text AS session_id,
       owner_instance,
       active_instance,
       execution_started_at,
       first_observed_at,
       last_observed_at,
       held_seconds
  FROM deploy_standby_holds
 WHERE released_at IS NULL
 ORDER BY first_observed_at
 LIMIT $1
"""

_schema_ready = False


def read_active_instance() -> str:
    """nginx 가 가리키는 active 슬롯 이름. 모르면 빈 문자열.

    chat_service._is_local_active_api_slot 과 같은 파일을 읽는다.
    deploy.sh 가 컷오버마다 쓰고 verify_container_slot_markers 로 양쪽
    컨테이너가 같은 값을 보는지 검증한다.
    """
    path = os.getenv("AADS_ACTIVE_CONTAINER_FILE", "/app/.active_container")
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _row_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


async def ensure_schema(conn: Any) -> None:
    global _schema_ready
    if _schema_ready:
        return
    await conn.execute(_ENSURE_SQL)
    _schema_ready = True


async def list_standby_owned_executions(
    conn: Any,
    *,
    active_instance: str,
    threshold_seconds: int = 0,
    session_id: Optional[str] = None,
    limit: int = _LIST_LIMIT,
) -> List[Dict[str, Any]]:
    """active 가 아닌 슬롯이 리스를 쥐고 있는 실행.

    active 를 모르면 아무것도 돌려주지 않는다 — 모르는 상태에서 "스탠바이가
    쥐고 있다"고 말하면 오탐이다.
    """
    if not active_instance:
        return []
    sid = uuid.UUID(str(session_id)) if session_id else None
    rows = await conn.fetch(
        _STANDBY_OWNED_SQL,
        active_instance,
        max(0, int(threshold_seconds)),
        sid,
        max(1, int(limit)),
    )
    return [_row_dict(row) for row in rows]


async def record_standby_holds(
    conn: Any,
    *,
    active_instance: str,
    threshold_seconds: int = STANDBY_HOLD_THRESHOLD_SECONDS,
) -> Dict[str, Any]:
    """임계를 넘은 스탠바이 점유를 deploy_standby_holds 에 남긴다."""
    if not active_instance:
        return {"active_instance": "", "open": 0, "released": 0, "holds": [],
                "skipped": "active_instance_unknown"}
    await ensure_schema(conn)
    holds = await list_standby_owned_executions(
        conn, active_instance=active_instance, threshold_seconds=threshold_seconds,
    )
    for hold in holds:
        await conn.execute(
            _UPSERT_SQL,
            hold["execution_id"],
            hold["session_id"],
            hold["owner_instance"],
            active_instance,
            int(hold.get("held_seconds") or 0),
        )
        logger.warning(
            "standby_slot_long_hold execution=%s session=%s owner=%s active=%s held_sec=%s",
            hold["execution_id"][:8],
            hold["session_id"][:8],
            hold["owner_instance"],
            active_instance,
            hold.get("held_seconds"),
        )
    released = await conn.execute(
        _RELEASE_SQL, [uuid.UUID(h["execution_id"]) for h in holds],
    )
    return {
        "active_instance": active_instance,
        "threshold_seconds": int(threshold_seconds),
        "open": len(holds),
        "released": _affected(released),
        "holds": holds,
    }


async def list_open_holds(conn: Any, *, limit: int = _LIST_LIMIT) -> List[Dict[str, Any]]:
    await ensure_schema(conn)
    rows = await conn.fetch(_OPEN_HOLDS_SQL, max(1, int(limit)))
    return [_row_dict(row) for row in rows]


async def approval_standby_warning(
    session_id: Optional[str],
    *,
    conn: Any = None,
) -> Optional[Dict[str, Any]]:
    """승인하는 세션이 전환 대상(스탠바이) 슬롯을 쥐고 있으면 경고를 만든다.

    2026-09-30 01:03 c7fee5ba — 배포를 승인한 세션의 턴이 green 에서 돌았고,
    01:09 컷오버로 green 이 스탠바이가 되자 그 턴이 다음 릴리스의 drain 을
    막았다. 승인 세션이 도구를 길게 돌릴수록 자기 배포가 막힌다.

    알리기만 한다. 실패하면 None — 경고 조회가 승인을 망치면 안 된다.
    """
    if not session_id:
        return None
    active = read_active_instance()
    if not active:
        return None
    try:
        if conn is None:
            from app.core.db_pool import get_pool

            async with get_pool().acquire() as acquired:
                return await _approval_warning_with_conn(acquired, session_id, active)
        return await _approval_warning_with_conn(conn, session_id, active)
    except Exception as exc:
        logger.warning(
            "approval_standby_warning_failed session=%s error=%s",
            str(session_id)[:8],
            str(exc)[:160],
        )
        return None


async def _approval_warning_with_conn(
    conn: Any, session_id: str, active: str,
) -> Optional[Dict[str, Any]]:
    own = await list_standby_owned_executions(
        conn, active_instance=active, threshold_seconds=0, session_id=session_id,
    )
    if not own:
        return None
    others = await list_standby_owned_executions(
        conn, active_instance=active, threshold_seconds=0,
    )
    other_count = len([r for r in others if r["session_id"] != str(session_id)])
    owners = sorted({r["owner_instance"] for r in own})
    return {
        "code": "approving_session_holds_standby_slot",
        "message": (
            f"⚠️ 승인한 이 세션의 실행이 스탠바이 슬롯({', '.join(owners)})에서 "
            f"진행 중입니다(active={active}). 이 턴이 끝나기 전에 다음 릴리스가 "
            "그 슬롯으로 전환하려 하면 target_slot_drain 에서 대기하다 막힐 수 있습니다. "
            "승인은 그대로 처리됐습니다 — 이 턴을 짧게 마무리하면 배포가 막히지 않습니다."
        ),
        "active_instance": active,
        "approving_session_executions": own,
        "other_sessions_on_standby": other_count,
    }


async def run_standby_hold_monitor_once() -> Dict[str, Any]:
    """active 슬롯에서만 기록한다 — 두 슬롯이 같은 표를 번갈아 덮지 않게."""
    from app.services.chat_service import _is_local_active_api_slot

    if not _is_local_active_api_slot():
        return {"skipped": "not_active_slot"}
    active = read_active_instance()
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        return await record_standby_holds(conn, active_instance=active)


def _affected(result: Any) -> int:
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0
