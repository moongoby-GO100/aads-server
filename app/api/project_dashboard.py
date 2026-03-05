"""
AADS Project Dashboard API — T-048
전체 프로젝트(GO100, KIS-V41, ShortFlow, NAS, NewTalk-V2, AADS) 통합 현황 API
데이터 소스:
  - system_memory: project:* 카테고리
  - system_memory: conversation:* 카테고리 (대화 통계)
  - go100_user_memory: 매니저 정보 / inbox
"""
from fastapi import APIRouter, HTTPException
from typing import Optional, Dict, List, Any
import json
import logging
from datetime import datetime, timezone, timedelta

from app.memory.store import memory_store
from app.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()

router = APIRouter()

# ─── 프로젝트 메타 정의 ───────────────────────────────────────────────────
PROJECTS_META = {
    "go100": {
        "name": "GO100 백억이",
        "manager": "GO100_MGR",
        "server": "211",
        "category": "project:go100",
    },
    "kis_v41": {
        "name": "KIS-V41 자동매매",
        "manager": "KIS_MGR",
        "server": "68",
        "category": "project:kis_v41",
    },
    "shortflow": {
        "name": "ShortFlow 숏폼",
        "manager": "SF_MGR",
        "server": "68",
        "category": "project:shortflow",
    },
    "nas": {
        "name": "NAS 스토리지",
        "manager": "NAS_MGR",
        "server": "68",
        "category": "project:nas",
    },
    "newtalk_v2": {
        "name": "NewTalk-V2",
        "manager": "NT_MGR",
        "server": "68",
        "category": "project:newtalk_v2",
    },
    "aads": {
        "name": "AADS 자율개발",
        "manager": "AADS_MGR",
        "server": "68",
        "category": "project:aads",
    },
}

# aads_conversations.project 컬럼값 → PROJECTS_META project_id 매핑
CONV_PROJECT_MAP = {
    "sf": "shortflow",
    "sales": "newtalk_v2",
    "kis": "kis_v41",
    "aads": "aads",
    "go100": "go100",
    "nas": "nas",
    "shortflow": "shortflow",
    "newtalk_v2": "newtalk_v2",
    "kis_v41": "kis_v41",
}

KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _parse_value(raw) -> Dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


# ─── (1) GET /projects/dashboard ─────────────────────────────────────────
@router.get("/projects/dashboard")
async def get_dashboard():
    """전체 프로젝트 통합 현황"""
    try:
        async with memory_store.pool.acquire() as conn:
            # system_memory에서 project:* 카테고리 전부 조회
            sys_rows = await conn.fetch(
                "SELECT category, key, value, updated_at FROM system_memory WHERE category LIKE 'project:%' ORDER BY category, key"
            )

            # conversation:* 카테고리 대화 수·최종 갱신 집계
            conv_rows = await conn.fetch(
                """
                SELECT category, COUNT(*) as cnt, MAX(updated_at) as last_updated
                FROM system_memory
                WHERE category LIKE 'conversation:%'
                GROUP BY category
                """
            )

            # go100_user_memory에서 매니저별 inbox 수
            inbox_rows = await conn.fetch(
                """
                SELECT memory_type, COUNT(*) as cnt
                FROM go100_user_memory
                WHERE user_id = 2
                  AND memory_type LIKE 'cross_msg_%'
                  AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY memory_type
                """
            )

            # go100_user_memory에서 project_status 최신 레코드 (project_id당 최신 1건)
            status_rows = await conn.fetch(
                """
                SELECT content FROM go100_user_memory
                WHERE user_id = 2 AND memory_type = 'project_status'
                ORDER BY created_at DESC
                """
            )

            # aads_conversations 테이블에서 project별 대화 수 (존재하는 경우)
            try:
                aads_conv_rows = await conn.fetch(
                    """
                    SELECT project, COUNT(*) as cnt
                    FROM aads_conversations
                    GROUP BY project
                    """
                )
            except Exception:
                aads_conv_rows = []

            # agent_registry 가능 여부 확인 후 에이전트 수 조회
            try:
                agent_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM agent_registry"
                )
            except Exception:
                agent_count = 0

        # system_memory를 카테고리별로 그룹화
        sys_by_cat: Dict[str, List[Dict]] = {}
        for r in sys_rows:
            cat = r["category"]
            if cat not in sys_by_cat:
                sys_by_cat[cat] = []
            sys_by_cat[cat].append({
                "key": r["key"],
                "value": _parse_value(r["value"]),
                "updated_at": str(r["updated_at"]),
            })

        # project_status override 맵 (project_id당 최신 1건)
        status_map: Dict[str, Dict] = {}
        for r in status_rows:
            c = r["content"] if isinstance(r["content"], dict) else json.loads(r["content"])
            pid = c.get("project_id")
            if pid and pid not in status_map:
                status_map[pid] = c

        # conversation 통계
        conv_stats: Dict[str, Dict] = {}
        total_conversations = 0
        for r in conv_rows:
            # category: conversation:go100 → project_id: go100
            proj_key = r["category"].replace("conversation:", "")
            cnt = r["cnt"]
            total_conversations += cnt
            conv_stats[proj_key] = {
                "count": cnt,
                "last_updated": str(r["last_updated"]),
            }

        # aads_conversations 테이블 대화 수 병합 (CONV_PROJECT_MAP으로 매핑)
        for r in aads_conv_rows:
            raw_proj = r["project"] or "aads"
            mapped = CONV_PROJECT_MAP.get(raw_proj, raw_proj)
            cnt = r["cnt"]
            total_conversations += cnt
            if mapped in conv_stats:
                conv_stats[mapped]["count"] += cnt
            else:
                conv_stats[mapped] = {"count": cnt, "last_updated": _now_kst()}

        # 프로젝트 목록 조립
        projects = []
        for project_id, meta in PROJECTS_META.items():
            cat = meta["category"]
            entries = sys_by_cat.get(cat, [])

            # system_memory에서 프로젝트 상태 추출
            status = "active"
            progress_percent = 0
            total_tasks = 0
            completed_tasks = 0
            key_issues: List[str] = []
            handover_url = ""
            last_updated = _now_kst()

            for entry in entries:
                v = entry["value"]
                key = entry["key"]
                if "status" in v:
                    status = v["status"]
                if "progress_percent" in v:
                    progress_percent = v["progress_percent"]
                if "total_tasks" in v:
                    total_tasks = v["total_tasks"]
                if "completed_tasks" in v:
                    completed_tasks = v["completed_tasks"]
                if "key_issues" in v:
                    issues = v["key_issues"]
                    if isinstance(issues, list):
                        key_issues.extend(issues)
                    elif isinstance(issues, str):
                        key_issues.append(issues)
                if "handover_url" in v:
                    handover_url = v["handover_url"]
                if entry["updated_at"]:
                    last_updated = entry["updated_at"]

            # project_status 데이터로 override
            s = status_map.get(project_id, {})
            if s:
                progress_percent = s.get("progress_percent", progress_percent)
                total_tasks = s.get("total_tasks", total_tasks)
                completed_tasks = s.get("completed_tasks", completed_tasks)
                handover_url = s.get("handover_url", handover_url)
                if s.get("key_issues"):
                    key_issues = s["key_issues"]
                if s.get("status"):
                    status = s["status"]

            conv_info = conv_stats.get(project_id, {})
            conversation_count = conv_info.get("count", 0)
            if conv_info.get("last_updated"):
                last_updated = conv_info["last_updated"]

            projects.append({
                "project_id": project_id,
                "name": meta["name"],
                "manager": meta["manager"],
                "server": meta["server"],
                "status": status,
                "progress_percent": progress_percent,
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "conversation_count": conversation_count,
                "last_updated": last_updated,
                "handover_url": handover_url,
                "key_issues": key_issues,
            })

        # 시스템 헬스 간단 체크
        system_health = {"api": True, "memory": memory_store.pool is not None, "sandbox": True}

        return {
            "status": "ok",
            "total_projects": len(projects),
            "projects": projects,
            "system_health": system_health,
            "total_conversations": total_conversations,
            "total_agents": agent_count or 20,
        }

    except Exception as e:
        logger.error(f"dashboard error: {e}")
        raise HTTPException(500, f"Dashboard error: {e}")


# ─── (3) GET /projects/dashboard/timeline ────────────────────────────────
# NOTE: 반드시 /{project_id} 라우트보다 먼저 등록해야 충돌 방지
@router.get("/projects/dashboard/timeline")
async def get_timeline():
    """최근 7일 프로젝트별 활동 이벤트"""
    try:
        async with memory_store.pool.acquire() as conn:
            # conversation:* 최근 7일 이벤트
            conv_rows = await conn.fetch(
                """
                SELECT category, key, value, updated_at
                FROM system_memory
                WHERE (category LIKE 'conversation:%' OR category LIKE 'project:%')
                  AND updated_at > NOW() - INTERVAL '7 days'
                ORDER BY updated_at DESC
                LIMIT 200
                """
            )

            # go100_user_memory 최근 7일 (importance >= 7)
            mem_rows = await conn.fetch(
                """
                SELECT memory_type, content, importance, created_at
                FROM go100_user_memory
                WHERE user_id = 2
                  AND importance >= 7.0
                  AND created_at > NOW() - INTERVAL '7 days'
                ORDER BY created_at DESC
                LIMIT 100
                """
            )

        # 날짜별 이벤트 집계
        events_by_date: Dict[str, List] = {}

        for r in conv_rows:
            cat = r["category"]
            v = _parse_value(r["value"])
            dt = r["updated_at"]
            date_str = str(dt)[:10] if dt else "unknown"

            if cat.startswith("conversation:"):
                project_id = cat.replace("conversation:", "")
                event_type = "conversation"
                description = v.get("source", r["key"])
            else:
                project_id = cat.replace("project:", "")
                event_type = "project_update"
                description = r["key"]

            if date_str not in events_by_date:
                events_by_date[date_str] = []
            events_by_date[date_str].append({
                "project_id": project_id,
                "event_type": event_type,
                "description": description,
                "timestamp": str(dt),
            })

        for r in mem_rows:
            content = _parse_value(r["content"])
            dt = r["created_at"]
            date_str = str(dt)[:10] if dt else "unknown"

            if date_str not in events_by_date:
                events_by_date[date_str] = []
            events_by_date[date_str].append({
                "project_id": content.get("agent_id", "system"),
                "event_type": r["memory_type"],
                "description": content.get("event_type", r["memory_type"]),
                "importance": float(r["importance"]),
                "timestamp": str(dt),
            })

        # 날짜 내림차순 정렬
        timeline = []
        for date_str in sorted(events_by_date.keys(), reverse=True):
            timeline.append({
                "date": date_str,
                "events": events_by_date[date_str],
                "event_count": len(events_by_date[date_str]),
            })

        return {
            "status": "ok",
            "days": 7,
            "total_events": sum(len(v) for v in events_by_date.values()),
            "timeline": timeline,
        }

    except Exception as e:
        logger.error(f"timeline error: {e}")
        raise HTTPException(500, f"Timeline error: {e}")


# ─── (4) GET /projects/dashboard/alerts ──────────────────────────────────
@router.get("/projects/dashboard/alerts")
async def get_alerts():
    """주의 필요 항목: 48시간 미활동 프로젝트 / 고중요도 미처리 메시지"""
    try:
        async with memory_store.pool.acquire() as conn:
            # 대화별 최종 갱신 시각
            conv_stats = await conn.fetch(
                """
                SELECT category, MAX(updated_at) as last_updated
                FROM system_memory
                WHERE category LIKE 'conversation:%'
                GROUP BY category
                """
            )

            # importance >= 8.5 미처리 메시지 (최근 7일)
            high_imp_rows = await conn.fetch(
                """
                SELECT id, memory_type, content, importance, created_at
                FROM go100_user_memory
                WHERE user_id = 2
                  AND importance >= 8.5
                  AND created_at > NOW() - INTERVAL '7 days'
                ORDER BY importance DESC, created_at DESC
                LIMIT 50
                """
            )

            # bridge_status 확인 (system_memory bridge 카테고리)
            bridge_rows = await conn.fetch(
                """
                SELECT key, value, updated_at
                FROM system_memory
                WHERE category = 'bridge_status'
                ORDER BY key
                """
            )

        now = datetime.now(timezone.utc)
        alerts: List[Dict] = []

        # 48시간 이상 대화 없는 프로젝트
        inactive_projects = []
        active_last_updated: Dict[str, Any] = {}
        for r in conv_stats:
            proj_key = r["category"].replace("conversation:", "")
            lu = r["last_updated"]
            if lu:
                active_last_updated[proj_key] = lu
                # timezone-aware 비교
                if lu.tzinfo is None:
                    lu_aware = lu.replace(tzinfo=timezone.utc)
                else:
                    lu_aware = lu
                diff_hours = (now - lu_aware).total_seconds() / 3600
                if diff_hours >= 48:
                    inactive_projects.append({
                        "project_id": proj_key,
                        "last_conversation": str(lu),
                        "inactive_hours": round(diff_hours, 1),
                    })

        # PROJECTS_META에서 대화 없는 프로젝트도 포함
        for project_id in PROJECTS_META:
            if project_id not in active_last_updated:
                inactive_projects.append({
                    "project_id": project_id,
                    "last_conversation": None,
                    "inactive_hours": 9999,
                })

        if inactive_projects:
            alerts.append({
                "alert_type": "inactive_projects",
                "severity": "warning",
                "count": len(inactive_projects),
                "items": inactive_projects,
                "message": f"{len(inactive_projects)}개 프로젝트에 48시간 이상 대화 없음",
            })

        # bridge_status 점검 (running=0)
        stopped_services = []
        for r in bridge_rows:
            v = _parse_value(r["value"])
            running = v.get("running", v.get("status", 1))
            if running == 0 or running == "stopped" or running is False:
                stopped_services.append({
                    "service": r["key"],
                    "value": v,
                    "updated_at": str(r["updated_at"]),
                })

        if stopped_services:
            alerts.append({
                "alert_type": "stopped_services",
                "severity": "critical",
                "count": len(stopped_services),
                "items": stopped_services,
                "message": f"{len(stopped_services)}개 서비스 중지 감지",
            })

        # 고중요도 미처리 메시지
        high_importance_messages = []
        for r in high_imp_rows:
            content = _parse_value(r["content"])
            high_importance_messages.append({
                "id": r["id"],
                "memory_type": r["memory_type"],
                "importance": float(r["importance"]),
                "topic": content.get("details", {}).get("topic", r["memory_type"]) if isinstance(content.get("details"), dict) else r["memory_type"],
                "created_at": str(r["created_at"]),
            })

        if high_importance_messages:
            alerts.append({
                "alert_type": "high_importance_messages",
                "severity": "warning",
                "count": len(high_importance_messages),
                "items": high_importance_messages,
                "message": f"importance ≥ 8.5 미처리 메시지 {len(high_importance_messages)}건",
            })

        return {
            "status": "ok",
            "generated_at": _now_kst(),
            "alert_count": len(alerts),
            "alerts": alerts,
        }

    except Exception as e:
        logger.error(f"alerts error: {e}")
        raise HTTPException(500, f"Alerts error: {e}")


# ─── (2) GET /projects/dashboard/{project_id} ─────────────────────────────
# NOTE: 반드시 /timeline, /alerts 라우트 뒤에 등록 (경로 충돌 방지)
@router.get("/projects/dashboard/{project_id}")
async def get_project_detail(project_id: str):
    """단일 프로젝트 상세"""
    if project_id not in PROJECTS_META:
        raise HTTPException(404, f"Project '{project_id}' not found. Available: {list(PROJECTS_META.keys())}")

    meta = PROJECTS_META[project_id]

    try:
        async with memory_store.pool.acquire() as conn:
            # system_memory에서 해당 프로젝트 전체 조회
            sys_rows = await conn.fetch(
                "SELECT key, value, updated_at FROM system_memory WHERE category=$1 ORDER BY key",
                meta["category"],
            )

            # 대화 최근 5건
            conv_rows = await conn.fetch(
                """
                SELECT key, value, updated_at
                FROM system_memory
                WHERE category=$1
                ORDER BY updated_at DESC
                LIMIT 5
                """,
                f"conversation:{project_id}",
            )

            # 매니저 inbox 수
            inbox_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM go100_user_memory
                WHERE user_id = 2
                  AND memory_type LIKE $1
                  AND created_at > NOW() - INTERVAL '7 days'
                """,
                f"%_{meta['manager']}",
            ) or 0

        # 태스크 분류
        tasks: Dict[str, List] = {"completed": [], "in_progress": [], "blocked": []}
        handover_summary = ""
        status = "active"
        progress_percent = 0

        for r in sys_rows:
            v = _parse_value(r["value"])
            key = r["key"]

            if "status" in v:
                status = v["status"]
            if "progress_percent" in v:
                progress_percent = v["progress_percent"]
            if "handover_summary" in v:
                handover_summary = v["handover_summary"]
            elif "summary" in v:
                handover_summary = v["summary"]

            # 태스크 파싱
            if "completed_tasks" in v and isinstance(v["completed_tasks"], list):
                tasks["completed"].extend(v["completed_tasks"])
            if "in_progress_tasks" in v and isinstance(v["in_progress_tasks"], list):
                tasks["in_progress"].extend(v["in_progress_tasks"])
            if "blocked_tasks" in v and isinstance(v["blocked_tasks"], list):
                tasks["blocked"].extend(v["blocked_tasks"])

        # 최근 대화 5건 정제
        recent_conversations = []
        for r in conv_rows:
            v = _parse_value(r["value"])
            recent_conversations.append({
                "id": r["key"],
                "source": v.get("source", "unknown"),
                "snapshot": v.get("snapshot", "")[:300],
                "logged_at": v.get("logged_at", str(r["updated_at"])),
            })

        return {
            "status": "ok",
            "project_id": project_id,
            "name": meta["name"],
            "manager": meta["manager"],
            "server": meta["server"],
            "project_status": status,
            "progress_percent": progress_percent,
            "tasks": tasks,
            "recent_conversations": recent_conversations,
            "manager_info": {
                "agent_id": meta["manager"],
                "inbox_count": inbox_count,
            },
            "handover_summary": handover_summary,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"project_detail error: {e}")
        raise HTTPException(500, f"Project detail error: {e}")
