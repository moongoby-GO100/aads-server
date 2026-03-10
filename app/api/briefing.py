"""
프로액티브 CEO 브리핑 API — CEO 접속 시 자동 표시.

미완료 Directive, 서버 상태, 알림, 최근 에러, 세션 요약을 수집하여
한 번의 호출로 브리핑 메시지를 반환한다.

기존 서비스(alert_manager, watchdog, health_checker, memory_recall)를
읽기 전용으로 조회만 한다.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Query

from app.memory.store import memory_store

router = APIRouter()
logger = structlog.get_logger(__name__)

KST = timezone(timedelta(hours=9))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads_dev_local@aads-postgres:5432/aads",
)


# ── 개별 데이터 수집 함수 ─────────────────────────────────────────────


async def _fetch_pending_alerts(
    conn, since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """alert_history에서 미확인(acknowledged=FALSE) 알림 조회."""
    try:
        tbl = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name='alert_history')"
        )
        if not tbl:
            return {"items": [], "counts": {}}

        where = "WHERE acknowledged = FALSE"
        params: list = []
        if since:
            where += " AND created_at > $1"
            params.append(since)

        rows = await conn.fetch(
            f"SELECT severity, category, title, message, server, created_at "
            f"FROM alert_history {where} "
            f"ORDER BY CASE severity "
            f"  WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END, "
            f"created_at DESC LIMIT 20",
            *params,
        )

        counts: Dict[str, int] = {}
        items = []
        for r in rows:
            sev = r["severity"] or "INFO"
            counts[sev] = counts.get(sev, 0) + 1
            items.append({
                "severity": sev,
                "category": r["category"],
                "title": r["title"],
                "message": r["message"],
                "server": r["server"],
            })
        return {"items": items[:10], "counts": counts}
    except Exception as e:
        logger.warning("briefing_alerts_failed", error=str(e))
        return {"items": [], "counts": {}}


async def _fetch_pending_directives(conn) -> Dict[str, Any]:
    """directive_lifecycle에서 pending/running/queued 조회."""
    try:
        tbl = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name='directive_lifecycle')"
        )
        if not tbl:
            return {"items": [], "count": 0}

        rows = await conn.fetch(
            "SELECT task_id, title, status, priority, project "
            "FROM directive_lifecycle "
            "WHERE status IN ('pending', 'running', 'queued') "
            "ORDER BY CASE WHEN status='running' THEN 0 "
            "  WHEN status='queued' THEN 1 ELSE 2 END, "
            "created_at DESC LIMIT 10",
        )
        items = []
        for r in rows:
            icon = "🔄" if r["status"] == "running" else "⏳"
            items.append({
                "icon": icon,
                "task_id": r["task_id"],
                "title": r["title"],
                "status": r["status"],
                "priority": r.get("priority") or "",
                "project": r.get("project") or "",
            })
        return {"items": items, "count": len(items)}
    except Exception as e:
        logger.warning("briefing_directives_failed", error=str(e))
        return {"items": [], "count": 0}


async def _fetch_recent_errors(
    conn, since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """error_log에서 최근 에러 조회."""
    try:
        tbl = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name='error_log')"
        )
        if not tbl:
            return {"items": [], "count": 0}

        where = "WHERE 1=1"
        params: list = []
        if since:
            where += " AND last_seen > $1"
            params.append(since)

        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM error_log {where}", *params,
        )

        rows = await conn.fetch(
            f"SELECT error_type, source, server, message, occurrence_count, last_seen "
            f"FROM error_log {where} "
            f"ORDER BY last_seen DESC LIMIT 5",
            *params,
        )
        items = []
        for r in rows:
            items.append({
                "error_type": r["error_type"],
                "source": r["source"],
                "server": r["server"],
                "message": (r["message"] or "")[:120],
                "count": r["occurrence_count"],
            })
        return {"items": items, "count": int(count or 0)}
    except Exception as e:
        logger.warning("briefing_errors_failed", error=str(e))
        return {"items": [], "count": 0}


async def _fetch_server_health(conn) -> Dict[str, Any]:
    """최근 저장된 monitored_services 또는 quick_health 결과 활용."""
    try:
        tbl = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name='monitored_services')"
        )
        if not tbl:
            return {"status": "UNKNOWN", "servers": {}}

        rows = await conn.fetch(
            "SELECT server, service_name, last_status, consecutive_failures "
            "FROM monitored_services WHERE enabled = TRUE "
            "ORDER BY server, service_name",
        )
        servers: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            srv = r["server"]
            if srv not in servers:
                servers[srv] = {"ok": 0, "fail": 0, "services": []}
            if r["last_status"] == "ok":
                servers[srv]["ok"] += 1
            else:
                servers[srv]["fail"] += 1
                servers[srv]["services"].append(r["service_name"])

        overall = "정상"
        for srv, info in servers.items():
            if info["fail"] > 0:
                overall = "주의"
                break

        return {"status": overall, "servers": servers}
    except Exception as e:
        logger.warning("briefing_health_failed", error=str(e))
        return {"status": "UNKNOWN", "servers": {}}


async def _fetch_last_session_summary(conn) -> Optional[str]:
    """가장 최근 세션 요약 반환."""
    try:
        tbl = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name='session_notes')"
        )
        if not tbl:
            return None

        row = await conn.fetchrow(
            "SELECT summary FROM session_notes "
            "ORDER BY created_at DESC LIMIT 1",
        )
        return row["summary"] if row else None
    except Exception as e:
        logger.warning("briefing_session_summary_failed", error=str(e))
        return None


async def _get_last_briefing_at(conn, user_id: str = "ceo") -> Optional[datetime]:
    """마지막 브리핑 시간 조회."""
    try:
        tbl = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name='briefing_log')"
        )
        if not tbl:
            return None

        row = await conn.fetchrow(
            "SELECT briefed_at FROM briefing_log "
            "WHERE user_id = $1 ORDER BY briefed_at DESC LIMIT 1",
            user_id,
        )
        return row["briefed_at"] if row else None
    except Exception:
        return None


async def _update_last_briefing_at(conn, user_id: str = "ceo") -> None:
    """브리핑 시간 기록."""
    try:
        await conn.execute(
            "INSERT INTO briefing_log (user_id, briefed_at) VALUES ($1, NOW())",
            user_id,
        )
    except Exception as e:
        logger.warning("briefing_log_update_failed", error=str(e))


# ── 브리핑 메시지 포맷팅 ──────────────────────────────────────────────


def _format_briefing(
    alerts: Dict[str, Any],
    directives: Dict[str, Any],
    errors: Dict[str, Any],
    health: Dict[str, Any],
    session_summary: Optional[str],
) -> str:
    """브리핑 메시지를 마크다운으로 포맷팅. 항목이 없는 카테고리는 생략."""
    lines: List[str] = []
    lines.append("## 📋 브리핑")
    lines.append("")

    # 알림
    alert_items = alerts.get("items", [])
    if alert_items:
        counts = alerts.get("counts", {})
        count_parts = []
        for sev in ("CRITICAL", "WARNING", "INFO"):
            c = counts.get(sev, 0)
            if c > 0:
                label = {"CRITICAL": "긴급", "WARNING": "주의", "INFO": "참고"}.get(sev, sev)
                count_parts.append(f"{label} {c}건")
        lines.append(f"⚠️ **알림 {len(alert_items)}건** ({', '.join(count_parts)})")
        for a in alert_items[:5]:
            lines.append(f"  - {a['title']}: {a['message']}")
        lines.append("")

    # 미완료 Directive
    dir_items = directives.get("items", [])
    if dir_items:
        lines.append(f"📌 **미완료 지시서 {len(dir_items)}건**")
        for d in dir_items[:5]:
            status_label = {"running": "진행중", "queued": "대기", "pending": "대기"}.get(d["status"], d["status"])
            lines.append(f"  - {d['icon']} [{d['task_id']}] {d['title']} ({status_label})")
        if len(dir_items) > 5:
            lines.append(f"  - ... 외 {len(dir_items) - 5}건")
        lines.append("")

    # 에러
    error_items = errors.get("items", [])
    error_count = errors.get("count", 0)
    if error_count > 0:
        lines.append(f"🔴 **에러 {error_count}건**")
        for e in error_items[:3]:
            cnt_str = f" ({e['count']}회)" if e.get("count", 1) > 1 else ""
            lines.append(f"  - {e['error_type']}: {e['message']}{cnt_str}")
        lines.append("")

    # 서버 상태
    servers = health.get("servers", {})
    if servers:
        parts = []
        for srv, info in servers.items():
            if info["fail"] == 0:
                parts.append(f"{srv} 정상")
            else:
                failed = ", ".join(info["services"][:2])
                parts.append(f"{srv} ⚠️{failed}")
        lines.append(f"🖥️ **서버**: {' | '.join(parts)}")
        lines.append("")

    # 마지막 세션 요약
    if session_summary:
        summary_short = session_summary[:200]
        if len(session_summary) > 200:
            summary_short += "..."
        lines.append(f"💬 **이전 세션 요약**: {summary_short}")
        lines.append("")

    # 아무 항목도 없으면 빈 문자열
    if len(lines) <= 2:
        return ""

    return "\n".join(lines)


# ── API 엔드포인트 ────────────────────────────────────────────────────


@router.get("/briefing")
async def get_briefing(
    session_id: Optional[str] = Query(None, description="현재 세션 ID"),
):
    """
    CEO 접속 시 프로액티브 브리핑 데이터를 반환.

    has_briefing이 false면 프론트엔드에서 아무것도 표시하지 않는다.
    """
    try:
        async with memory_store.pool.acquire() as conn:
            # 마지막 브리핑 시간 조회
            last_briefing = await _get_last_briefing_at(conn)

            # 병렬로 데이터 수집
            alerts_task = _fetch_pending_alerts(conn, since=last_briefing)
            directives_task = _fetch_pending_directives(conn)
            errors_task = _fetch_recent_errors(conn, since=last_briefing)
            health_task = _fetch_server_health(conn)
            summary_task = _fetch_last_session_summary(conn)

            alerts, directives, errors, health, session_summary = await asyncio.gather(
                alerts_task, directives_task, errors_task, health_task, summary_task,
            )

            # 브리핑 텍스트 생성
            briefing_message = _format_briefing(
                alerts, directives, errors, health, session_summary,
            )

            has_briefing = bool(briefing_message)

            alert_counts = alerts.get("counts", {})
            directive_count = directives.get("count", 0)
            error_count = errors.get("count", 0)

            # 브리핑 시간 기록 (has_briefing 여부와 무관하게 갱신)
            await _update_last_briefing_at(conn)

            logger.info(
                "briefing_served",
                has_briefing=has_briefing,
                alert_count=sum(alert_counts.values()),
                directive_count=directive_count,
                error_count=error_count,
            )

            return {
                "has_briefing": has_briefing,
                "briefing_message": briefing_message,
                "alert_count": {
                    "emergency": alert_counts.get("CRITICAL", 0),
                    "warning": alert_counts.get("WARNING", 0),
                    "info": alert_counts.get("INFO", 0),
                },
                "directive_pending_count": directive_count,
                "error_count_since_last": error_count,
            }
    except Exception as e:
        logger.error("briefing_failed", error=str(e))
        return {
            "has_briefing": False,
            "briefing_message": "",
            "alert_count": {"emergency": 0, "warning": 0, "info": 0},
            "directive_pending_count": 0,
            "error_count_since_last": 0,
        }
