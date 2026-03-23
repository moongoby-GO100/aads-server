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
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
        "server": "211",
        "category": "project:kis_v41",
    },
    "shortflow": {
        "name": "ShortFlow 숏폼",
        "manager": "SF_MGR",
        "server": "114",
        "category": "project:shortflow",
    },
    "nas": {
        "name": "NAS 스토리지",
        "manager": "NAS_MGR",
        "server": "114",
        "category": "project:nas",
    },
    "newtalk_v2": {
        "name": "NewTalk-V2",
        "manager": "NT_MGR",
        "server": "114",
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


# ─── T-089: VALID_PROJECTS 화이트리스트 ────────────────────────────────────
VALID_PROJECTS = {'AADS', 'KIS', 'GO100', 'ShortFlow', 'NewTalk', 'NAS', 'SALES'}


def _validate_project_name(raw: str) -> str:
    """T-089: 화이트리스트 기반 프로젝트명 정규화"""
    if not raw or not isinstance(raw, str):
        return 'AADS'
    cleaned = raw.strip()
    if len(cleaned) > 30:
        return 'AADS'
    upper = cleaned.upper()
    MAPPING = {
        'AADS': 'AADS', 'AADS-SERVER': 'AADS', 'AADS-DASHBOARD': 'AADS',
        'KIS': 'KIS', 'KIS-AUTOTRADE-V41': 'KIS', 'KIS-AUTOTRADE-V4.1': 'KIS',
        'GO100': 'GO100', 'SHORTFLOW': 'ShortFlow', 'SF': 'ShortFlow',
        'NEWTALK': 'NewTalk', 'NAS': 'NAS', 'SALES': 'SALES',
    }
    if upper in MAPPING:
        return MAPPING[upper]
    for key, val in MAPPING.items():
        if key in upper:
            return val
    return 'AADS'


# ─── T-082: 프로젝트명 정규화 맵 ───────────────────────────────────────────
_PROJECT_NORM_MAP = {
    "aads": "AADS",
    "aads-server": "AADS",
    "aads_server": "AADS",
    "kis": "KIS",
    "kis-autotrade-v41": "KIS",
    "kis-autotrade-v4.1": "KIS",
    "kis-v41": "KIS",
    "go100": "GO100",
    "shortflow": "ShortFlow",
    "sf": "ShortFlow",
    "newtalk": "NewTalk",
    "newtalk_v2": "NewTalk",
    "ntv2": "NewTalk",
    "nas": "NAS",
}


def _normalize_project(proj: str) -> str:
    """프로젝트명 정규화 (T-082): 소문자·변형 → 표준명으로 통일"""
    v = proj.strip()
    return _PROJECT_NORM_MAP.get(v.lower(), v)


def _to_kst_str(dt_or_str) -> str:
    """datetime 또는 문자열을 KST ISO 형식으로 변환 (T-074)"""
    if not dt_or_str:
        return ""
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str if dt_or_str.tzinfo else dt_or_str.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    s = str(dt_or_str)
    try:
        # try parsing ISO format
        s_clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s_clean)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        return s


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
            # category: conversation:sf → proj_key: sf → mapped: shortflow (CONV_PROJECT_MAP)
            proj_key = r["category"].replace("conversation:", "")
            proj_key = CONV_PROJECT_MAP.get(proj_key, proj_key)
            cnt = r["cnt"]
            total_conversations += cnt
            if proj_key in conv_stats:
                conv_stats[proj_key]["count"] += cnt
            else:
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

            # go100_user_memory project_status 최신 레코드 조회
            status_row = await conn.fetchrow(
                """
                SELECT content FROM go100_user_memory
                WHERE user_id = 2 AND memory_type = 'project_status'
                  AND content->>'project_id' = $1
                ORDER BY created_at DESC LIMIT 1
                """, project_id
            )

        # 태스크 분류
        tasks: Dict[str, List] = {"completed": [], "in_progress": [], "blocked": []}
        handover_summary = ""
        status = "active"
        progress_percent = 0
        total_tasks = 0
        completed_tasks = 0
        handover_url = ""
        key_issues: List[str] = []

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

        # project_status override from go100_user_memory
        if status_row:
            s = status_row["content"] if isinstance(status_row["content"], dict) else json.loads(status_row["content"])
            progress_percent = s.get("progress_percent", progress_percent)
            total_tasks = s.get("total_tasks", total_tasks)
            completed_tasks = s.get("completed_tasks", completed_tasks)
            handover_url = s.get("handover_url", handover_url)
            key_issues = s.get("key_issues", key_issues)
            if s.get("status"):
                status = s["status"]

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
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "handover_url": handover_url,
            "key_issues": key_issues,
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


# ─── T-066: Directives / Reports / Task-History APIs ─────────────────────────

DIRECTIVES_RUNNING_DIR = Path("/root/.genspark/directives/running")
DIRECTIVES_DONE_DIR = Path("/root/.genspark/directives/done")
REPORTS_LOCAL_DIR = Path("/root/project-docs/aads/reports")

GITHUB_REPORTS_BASE = "https://github.com/moongoby/project-docs/blob/main/aads/reports"


VALID_PROJECT_NAMES = frozenset({
    "AADS", "KIS", "GO100", "ShortFlow", "NewTalk", "NAS", "SALES",
    "aads-server", "aads-dashboard",
})


def validate_project_name(project: str) -> str:
    """프로젝트명 유효성 검사 — 한글 문장 등 비정상 값은 AADS로 대체 (T-082)

    허용값: AADS, KIS, GO100, ShortFlow, NewTalk, NAS, SALES, aads-server, aads-dashboard
    """
    if not project or len(project) > 30:
        return "AADS"
    if project in VALID_PROJECT_NAMES:
        return project
    project_lower = project.lower()
    for valid in VALID_PROJECT_NAMES:
        if project_lower == valid.lower():
            return valid
    return "AADS"


def _project_from_task_id(task_id: str):
    """T-107: task_id에서 프로젝트 직접 판별.
    AADS-095 → 'AADS', KIS-168 → 'KIS', T-095 → None (기존 _classify_project 폴백 필요)
    """
    REVERSE_MAP = {
        "AADS": "AADS", "KIS": "KIS", "GO100": "GO100",
        "SF": "ShortFlow", "NT": "NewTalk", "SALES": "SALES", "NAS": "NAS",
    }
    for prefix, project in REVERSE_MAP.items():
        if task_id.startswith(f"{prefix}-"):
            return project
    return None  # T-xxx는 기존 _classify_project 사용


def _classify_project(filename: str, content: str) -> str:
    """파일명 접두사 + 본문 키워드로 프로젝트 자동 분류 (T-082 전면 재작성)

    1단계: 파일명 접두사 매칭 (최우선) → 해당 프로젝트
    2단계: AADS 인프라 키워드 리스트 매칭 → AADS
    3단계: 프로젝트 고유 키워드 매칭
    4단계: 기본값 AADS
    """
    content_lower = content.lower()

    # 1단계: 파일명 접두사 매칭 (최우선)
    fname = filename.upper()
    if fname.startswith("KIS_"):
        return _validate_project_name("KIS")
    if fname.startswith("GO100_"):
        return _validate_project_name("GO100")
    if fname.startswith("SF_"):
        return _validate_project_name("ShortFlow")
    if fname.startswith("NT_"):
        return _validate_project_name("NewTalk")
    if fname.startswith("SALES_"):
        return _validate_project_name("SALES")
    if fname.startswith("NAS_"):
        return _validate_project_name("NAS")

    # 2단계: AADS 인프라 키워드 → AADS
    aads_keywords = [
        'dashboard', 'bridge', 'handover', 'ceo_chat', 'context', 'memory',
        'supervisor', 'agent', 'pipeline', 'docker', 'nginx', 'remote_agent',
        'classify_project', 'saferender', 'parse_engine', 'visual_qa', 'mobile_qa',
        'mcp', 'langgraph', 'sandbox', 'directives', 'deploy',
        'typescript', 'npm build', 'git push', 'aads-server', 'aads-dashboard', 'aads-docs',
        'project_dashboard', 'bridge.py', 'genspark_bridge', 'auto_trigger', 'claude_exec',
        'docker-compose', 'aads_remote', 'cross-message', 'cross_msg',
        'system_memory', 'context.py', 'task id:', 'directive',
        'error_breakdown', 'frontend', 'npm run build',
        '대시보드', 'ceo chat', '원격 에이전트', 'remote agent', '프론트엔드',
    ]
    for kw in aads_keywords:
        if kw in content_lower:
            return _validate_project_name("AADS")

    # 3단계: 프로젝트 고유 키워드 매칭
    # KIS
    kis_keywords = ['kis', 'autotrade', '자동매매', '피라미딩', 'desk', '한국투자',
                    'fractal trend', 'pyramiding']
    if any(kw.lower() in content_lower for kw in kis_keywords):
        return _validate_project_name("KIS")

    # GO100
    go100_keywords = ['go100', '지오백', '100세']
    if any(kw.lower() in content_lower for kw in go100_keywords):
        return _validate_project_name("GO100")

    # ShortFlow
    sf_keywords = ['shortflow', 'sf', '숏폼', '영상', 'economy', 'finance', 'tech',
                   'ffmpeg', 'shortform video', 'run_v4_pipeline']
    if any(kw.lower() in content_lower for kw in sf_keywords):
        return _validate_project_name("ShortFlow")

    # NewTalk
    nt_keywords = ['newtalk', '뉴톡', 'v1fix', 'v2', '이미지', 'goods']
    if any(kw.lower() in content_lower for kw in nt_keywords):
        return _validate_project_name("NewTalk")

    # NAS
    nas_keywords = ['nas', 'nasync', 'n2']
    if any(kw.lower() in content_lower for kw in nas_keywords):
        return _validate_project_name("NAS")

    # SALES
    sales_keywords = ['sales', 'marketing', '마케팅', '영업']
    if any(kw.lower() in content_lower for kw in sales_keywords):
        return _validate_project_name("SALES")

    # 4단계: 기본값 AADS
    return _validate_project_name("AADS")


def _classify_error(content: str) -> Optional[str]:
    """에러 내용에서 에러 유형 분류 (T-072: 패턴 강화)"""
    if any(x in content for x in ['401', 'OAuth', 'token expired', 'Unauthorized']):
        return 'auth_expired'
    if any(x in content for x in ['Permission denied', 'EACCES', 'permission']):
        return 'permission_denied'
    if any(x in content for x in ['env', 'environment', 'variable not set']):
        return 'env_error'
    if any(x in content for x in ['timeout', 'watchdog', 'TIMEOUT']):
        return 'timeout'
    if any(x in content for x in ['error', 'failed', 'failure', 'ERROR']):
        return 'task_failure'
    return None


def _parse_directive_file(filepath: Path, default_status: str) -> Dict:
    """지시서 파일 파싱 — running/done 양쪽 지원"""
    filename = filepath.name
    task_id = "UNKNOWN"
    title = filename
    project = "AADS"
    status = default_status
    created_at = ""

    # 파일 수정 시각 (생성시각 대용)
    try:
        mtime = filepath.stat().st_mtime
        created_at = datetime.fromtimestamp(mtime, tz=KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        created_at = _now_kst()

    # 파일 내용 파싱
    try:
        raw = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {
            "task_id": task_id, "title": title, "status": status,
            "project": project, "created_at": created_at, "file_path": str(filepath),
            "started_at": created_at, "completed_at": created_at if default_status == "completed" else "",
            "duration_seconds": None, "error_type": "",
        }

    # YAML 프런트매터 (--- ... ---)
    yaml_match = re.match(r"^---\s*\n(.*?)\n---", raw, re.DOTALL)
    completed_at_from_yaml = ""
    if yaml_match:
        yaml_block = yaml_match.group(1)
        for line in yaml_block.splitlines():
            line = line.strip()
            if line.startswith("task_id:"):
                val = line.split(":", 1)[1].strip()
                if re.match(r"T-\d+", val):
                    task_id = val
            elif line.startswith("project:"):
                project = validate_project_name(_normalize_project(line.split(":", 1)[1].strip()))
            elif line.startswith("status:"):
                status = line.split(":", 1)[1].strip()
            elif line.startswith("completed_at:"):
                completed_at_from_yaml = line.split(":", 1)[1].strip()
        # 제목은 파일명에서 유추 (T-082: 50자 초과 시 description으로 취급)
        title_match = re.search(r"^제목\s*:\s*(.+)", raw, re.MULTILINE)
        if title_match:
            _t = title_match.group(1).strip()
            title = _t[:100] if _t else filename
        else:
            title = filename
    else:
        # 일반 텍스트 형식
        m_title = re.search(r"^제목\s*:\s*(.+)", raw, re.MULTILINE)
        if m_title:
            _t = m_title.group(1).strip()
            title = _t[:100] if _t else filename
        # T-082: 콜론 필수 + 50자 이하만 허용 (본문 내용 오분류 방지)
        m_proj = re.search(r"^프로젝트\s*:\s*([^\n]{1,50})", raw, re.MULTILINE)
        if m_proj:
            project = validate_project_name(_normalize_project(m_proj.group(1).strip()))

    # Task ID 파싱 우선순위 (YAML에서 못 찾은 경우)
    if task_id == "UNKNOWN":
        _INVALID_IDS = {"ERROR", "UNKNOWN", "TIMEOUT"}
        # 1순위: "Task ID: T-NNN"
        m1 = re.search(r"Task\s*ID\s*:\s*(T-\d+)", raw, re.IGNORECASE)
        if m1 and m1.group(1) not in _INVALID_IDS:
            task_id = m1.group(1)
        else:
            # 2순위: "task_id: T-NNN"
            m2 = re.search(r"task_id\s*:\s*(T-\d+)", raw, re.IGNORECASE)
            if m2 and m2.group(1) not in _INVALID_IDS:
                task_id = m2.group(1)
            else:
                # 3순위: "# T-NNN"
                m3 = re.search(r"#\s*(T-\d+)", raw)
                if m3 and m3.group(1) not in _INVALID_IDS:
                    task_id = m3.group(1)
                else:
                    # 4순위: 파일명에서 T-NNN
                    m4 = re.search(r"(T-\d+)", filename)
                    if m4 and m4.group(1) not in _INVALID_IDS:
                        task_id = m4.group(1)
                    else:
                        # 최종: UNTAGGED-{파일명 앞8자}
                        task_id = f"UNTAGGED-{filename[:8]}"

    # 프로젝트 자동 분류 (project가 기본값이면 내용으로 분류)
    if project == "AADS":
        project = _classify_project(filename, raw[:2000])

    # 파일명에서 시작 시각 추출 (AADS_YYYYMMDD_HHMMSS_...) — started_at 기준
    started_at_from_fname = ""
    fname_dt = re.search(r"(\d{8}_\d{6})", filename)
    if fname_dt:
        dt_str = fname_dt.group(1)
        try:
            dt = datetime.strptime(dt_str, "%Y%m%d_%H%M%S").replace(tzinfo=KST)
            started_at_from_fname = dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except Exception:
            pass
    if started_at_from_fname and not created_at:
        created_at = started_at_from_fname

    # 에러 유형 분류 (T-072: 지시서에도 error_type 포함)
    error_type = _classify_error(raw[:2000]) if status == "error" else None

    # Summary: first 500 chars of non-frontmatter content
    summary_text = ""
    try:
        raw_no_front = re.sub(r"^---\s*\n.*?\n---\s*\n", "", raw, count=1, flags=re.DOTALL)
        summary_text = raw_no_front.strip()[:500]
    except Exception:
        pass

    # started_at: 파일명 타임스탬프 우선 (작업 시작 시각)
    # completed_at: YAML completed_at 우선, 없으면 파일 mtime
    _started = started_at_from_fname or created_at
    _completed = completed_at_from_yaml or (created_at if default_status == "completed" else "")
    return {
        "task_id": task_id,
        "title": title,
        "status": status,
        "project": project,
        "error_type": error_type or "",
        "created_at": _started,
        "started_at": _started,
        "completed_at": _completed,
        "duration_seconds": None,
        "file_path": str(filepath),
        "summary": summary_text,
    }


def _parse_report_file(filepath: Path) -> Dict:
    """보고서 파일 파싱"""
    filename = filepath.name
    task_id = "UNKNOWN"
    completed_at = ""
    status = "success"
    project = "AADS"
    summary = ""

    try:
        mtime = filepath.stat().st_mtime
        completed_at = datetime.fromtimestamp(mtime, tz=KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        completed_at = _now_kst()

    try:
        raw = filepath.read_text(encoding="utf-8", errors="replace")
        # 처음 2000자만 파싱
        head = raw[:2000]
    except Exception:
        head = ""

    # YAML 프런트매터
    yaml_match = re.match(r"^---\s*\n(.*?)\n---", head, re.DOTALL)
    if yaml_match:
        yaml_block = yaml_match.group(1)
        for line in yaml_block.splitlines():
            line = line.strip()
            if line.startswith("task_id:"):
                val = line.split(":", 1)[1].strip()
                if re.match(r"T-\d+", val):
                    task_id = val
            elif line.startswith("project:"):
                project = validate_project_name(_normalize_project(line.split(":", 1)[1].strip()))
            elif line.startswith("status:"):
                v = line.split(":", 1)[1].strip().lower()
                status = "error" if v in ("error", "fail", "failed") else "success"
            elif line.startswith("completed_at:"):
                completed_at = line.split(":", 1)[1].strip()

    # 요약: 첫 비YAML 단락
    body = re.sub(r"^---.*?---\s*\n", "", head, flags=re.DOTALL).strip()
    summary = body[:200].replace("\n", " ") if body else filename

    # Task ID 파싱 우선순위
    if task_id == "UNKNOWN":
        _INVALID_IDS = {"ERROR", "UNKNOWN", "TIMEOUT"}
        # 1순위: "Task ID: T-NNN"
        m1 = re.search(r"Task\s*ID\s*:\s*(T-\d+)", head, re.IGNORECASE)
        if m1 and m1.group(1) not in _INVALID_IDS:
            task_id = m1.group(1)
        else:
            # 2순위: "task_id: T-NNN"
            m2 = re.search(r"task_id\s*:\s*(T-\d+)", head, re.IGNORECASE)
            if m2 and m2.group(1) not in _INVALID_IDS:
                task_id = m2.group(1)
            else:
                # 3순위: "# T-NNN"
                m3 = re.search(r"#\s*(T-\d+)", head)
                if m3 and m3.group(1) not in _INVALID_IDS:
                    task_id = m3.group(1)
                else:
                    # 4순위: 파일명에서 T-NNN
                    m4 = re.search(r"(T-\d+)", filename)
                    if m4 and m4.group(1) not in _INVALID_IDS:
                        task_id = m4.group(1)
                    else:
                        # 최종: UNTAGGED-{파일명 앞8자}
                        task_id = f"UNTAGGED-{filename[:8]}"

    # 에러 유형 분류
    error_type = _classify_error(head) if status == "error" else ""

    # 프로젝트 자동 분류 (project가 기본값이면 내용+파일명으로 분류)
    if project == "AADS":
        project = _classify_project(filename, head)

    github_url = f"{GITHUB_REPORTS_BASE}/{filename}" if REPORTS_LOCAL_DIR.exists() else ""

    return {
        "task_id": task_id,
        "filename": filename,
        "status": status,
        "error_type": error_type,
        "completed_at": completed_at,
        "project": project,
        "github_url": github_url,
        "summary": summary,
    }


# ─── T-090: project_tasks → Directive 형식 변환 헬퍼 ─────────────────────────
def _extract_dt_from_id(task_id: str) -> str:
    """task_id / filename에서 날짜 추출: YYYYMMDD_HHMMSS → KST ISO"""
    m = re.search(r"(\d{8}_\d{6})", task_id or "")
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=KST)
            return dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except Exception:
            pass
    return ""


def _pt_row_to_directive(row) -> Dict:
    """project_tasks 레코드를 directive 형식으로 변환"""
    raw = row.get("raw_data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    status_raw = (row.get("status") or "reported").lower()
    if status_raw in ("done", "finished", "success", "completed"):
        status = "completed"
    elif status_raw in ("running", "active"):
        status = "running"
    elif status_raw in ("error", "failed", "fail"):
        status = "error"
    else:
        status = "completed"
    return {
        "task_id": row.get("task_id") or "UNKNOWN",
        "title": row.get("title") or "",
        "status": status,
        "project": _validate_project_name(row.get("project") or "AADS"),
        "error_type": "",
        "created_at": _to_kst_str(row.get("started_at") or _extract_dt_from_id(row.get("task_id","")) or row.get("created_at")),
        "started_at": _to_kst_str(row.get("started_at") or _extract_dt_from_id(row.get("task_id","")) or row.get("created_at")),
        "completed_at": _to_kst_str(row.get("completed_at")),
        "duration_seconds": None,
        "file_path": f"[{row.get('source','remote')}] {row.get('task_id','')}",
        "source": row.get("source") or "remote",
        "summary": row.get("summary") or "",
    }


def _pt_row_to_report(row) -> Dict:
    """project_tasks 레코드를 report 형식으로 변환"""
    return {
        "task_id": row.get("task_id") or "UNKNOWN",
        "filename": f"[{row.get('source','remote')}] {row.get('task_id','')}",
        "status": "success" if (row.get("status") or "") in ("completed", "done", "success") else "reported",
        "error_type": "",
        "completed_at": _to_kst_str(row.get("completed_at")),
        "project": _validate_project_name(row.get("project") or "AADS"),
        "github_url": "",
        "summary": row.get("summary") or "",
        "source": row.get("source") or "remote",
        "cost_usd": 0.0,
    }


# ─── (5) GET /dashboard/directives ───────────────────────────────────────────
@router.get("/dashboard/directives")
async def get_directives(project: Optional[str] = None):
    """작업지시서 현황: running + done 디렉터리 스캔 + project_tasks UNION (T-090)"""
    directives: List[Dict] = []

    # running 디렉터리
    if DIRECTIVES_RUNNING_DIR.exists():
        for f in sorted(DIRECTIVES_RUNNING_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            directives.append(_parse_directive_file(f, "running"))

    # done 디렉터리
    if DIRECTIVES_DONE_DIR.exists():
        for f in sorted(DIRECTIVES_DONE_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            item = _parse_directive_file(f, "completed")
            directives.append(item)

    # T-090: project_tasks 테이블에서 원격 보고서 UNION
    try:
        async with memory_store.pool.acquire() as conn:
            # 테이블 존재 확인
            tbl_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='project_tasks')"
            )
            if tbl_exists:
                query = "SELECT task_id, project, source, title, status, summary, started_at, completed_at FROM project_tasks"
                params: list = []
                if project and project.upper() not in ("ALL", ""):
                    query += " WHERE project ILIKE $1"
                    params.append(project)
                query += " ORDER BY (CASE WHEN status='running' THEN 0 ELSE 1 END), COALESCE(completed_at, started_at) DESC NULLS LAST LIMIT 2000"
                pt_rows = await conn.fetch(query, *params)
                for row in pt_rows:
                    directives.append(_pt_row_to_directive(dict(row)))
    except Exception as e:
        logger.warning(f"project_tasks 조회 실패 (무시): {e}")

    running_count = sum(1 for d in directives if d["status"] == "running")
    completed_count = sum(1 for d in directives if d["status"] == "completed")
    error_count = sum(1 for d in directives if d["status"] == "error")

    # 중복 제거: 같은 task_id는 가장 최신 1건만
    seen_task_ids: Dict[str, Dict] = {}
    duplicates: List[Dict] = []
    for d in directives:
        tid = d["task_id"]
        if tid.startswith("UNTAGGED-"):
            # UNTAGGED는 중복 처리 없이 그대로
            seen_task_ids[f"__uniq_{d['file_path']}"] = d
        elif tid not in seen_task_ids:
            seen_task_ids[tid] = d
        else:
            # 이미 있으면 최신 것을 유지
            existing = seen_task_ids[tid]
            if d["created_at"] > existing["created_at"]:
                duplicates.append(existing)
                seen_task_ids[tid] = d
            else:
                duplicates.append(d)

    unique_directives = list(seen_task_ids.values())

    # 프로젝트 필터 적용 (T-072)
    if project and project.upper() not in ("ALL", ""):
        unique_directives = [d for d in unique_directives if d.get("project", "").upper() == project.upper()]

    # by_project / project_breakdown 집계 — T-082: validate_project_name 적용
    by_project: Dict[str, int] = {}
    for d in unique_directives:
        proj = validate_project_name(d["project"])
        by_project[proj] = by_project.get(proj, 0) + 1

    # error 유형 집계 — T-072: error_breakdown 분리, "error" 키는 숫자로만
    error_items = [d for d in unique_directives if d["status"] == "error"]
    error_breakdown: Dict[str, int] = {
        "auth_expired": 0,
        "permission_denied": 0,
        "env_error": 0,
        "timeout": 0,
        "task_failure": 0,
    }
    for d in error_items:
        et = d.get("error_type", "") or "task_failure"
        if et in error_breakdown:
            error_breakdown[et] += 1
        else:
            error_breakdown["task_failure"] += 1

    # 필터 적용 후 카운트 재계산 (T-072)
    f_running = sum(1 for d in unique_directives if d["status"] == "running")
    f_completed = sum(1 for d in unique_directives if d["status"] == "completed")
    f_error = sum(1 for d in unique_directives if d["status"] == "error")

    # T-089: 반환 전 일괄 정규화
    for item in unique_directives:
        item['project'] = _validate_project_name(item.get('project', 'AADS'))

    return {
        "status": "ok",
        "total": len(unique_directives),
        "unique_tasks": len(unique_directives),
        "running": f_running,
        "completed": f_completed,
        "error": f_error,               # T-072: 숫자로만 (React Error #31 방지)
        "error_breakdown": error_breakdown,  # T-072: 별도 키로 분리
        "summary": {
            "completed": f_completed,
            "error": f_error,
            "running": f_running,
            "timeout": error_breakdown.get("timeout", 0),
            "pending": 0,
        },
        "project_breakdown": by_project,
        "by_project": by_project,
        "items": unique_directives,      # T-072: items 키 추가 (별칭)
        "directives": unique_directives,
    }


# ─── (6) GET /dashboard/reports ──────────────────────────────────────────────
@router.get("/dashboard/reports")
async def get_reports(project: Optional[str] = None):
    """작업결과보고서 목록 + project_tasks UNION (T-090)"""
    reports: List[Dict] = []

    # 로컬 reports 디렉터리
    if REPORTS_LOCAL_DIR.exists():
        for f in sorted(REPORTS_LOCAL_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            reports.append(_parse_report_file(f))
    else:
        # fallback: done 디렉터리에서 RESULT 파일만
        if DIRECTIVES_DONE_DIR.exists():
            result_files = sorted(
                [f for f in DIRECTIVES_DONE_DIR.glob("*RESULT*.md")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in result_files:
                reports.append(_parse_report_file(f))

    # T-090: project_tasks 테이블에서 완료된 원격 작업 보고서 UNION
    try:
        async with memory_store.pool.acquire() as conn:
            tbl_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='project_tasks')"
            )
            if tbl_exists:
                query = (
                    "SELECT task_id, project, source, title, status, summary, started_at, completed_at "
                    "FROM project_tasks WHERE source != 'local'"
                )
                params: list = []
                if project and project.upper() not in ("ALL", ""):
                    query += " AND project ILIKE $1"
                    params.append(project)
                query += " ORDER BY completed_at DESC NULLS LAST LIMIT 600"
                pt_rows = await conn.fetch(query, *params)
                # T-095: task_cost_log에서 task_id별 비용 조회
                task_ids = [r["task_id"] for r in pt_rows if r["task_id"]]
                cost_by_task: Dict[str, float] = {}
                if task_ids:
                    try:
                        cost_log_rows = await conn.fetch(
                            """
                            SELECT task_id, COALESCE(SUM(cost_usd), 0) AS cost
                            FROM task_cost_log
                            WHERE task_id = ANY($1::varchar[])
                            GROUP BY task_id
                            """,
                            task_ids
                        )
                        cost_by_task = {r["task_id"]: float(r["cost"]) for r in cost_log_rows}
                    except Exception:
                        cost_by_task = {}
                for row in pt_rows:
                    r = _pt_row_to_report(dict(row))
                    r["cost_usd"] = cost_by_task.get(r["task_id"], 0.0)
                    reports.append(r)
    except Exception as e:
        logger.warning(f"project_tasks reports 조회 실패 (무시): {e}")

    # 중복 제거: 같은 task_id는 가장 최신 1건만
    seen_task_ids: Dict[str, Dict] = {}
    duplicates: List[Dict] = []
    for r in reports:
        tid = r["task_id"]
        if tid.startswith("UNTAGGED-"):
            seen_task_ids[f"__uniq_{r['filename']}"] = r
        elif tid not in seen_task_ids:
            seen_task_ids[tid] = r
        else:
            existing = seen_task_ids[tid]
            if r["completed_at"] > existing["completed_at"]:
                duplicates.append(existing)
                seen_task_ids[tid] = r
            else:
                duplicates.append(r)

    unique_reports = list(seen_task_ids.values())

    # 프로젝트 필터 적용 (T-072)
    if project and project.upper() not in ("ALL", ""):
        unique_reports = [r for r in unique_reports if r.get("project", "").upper() == project.upper()]

    # 집계
    success_count = sum(1 for r in unique_reports if r["status"] == "success")
    error_reports = [r for r in unique_reports if r["status"] == "error"]

    error_breakdown: Dict[str, int] = {
        "total": len(error_reports),
        "auth_expired": 0,
        "permission_denied": 0,
        "env_error": 0,
        "timeout": 0,
        "task_failure": 0,
    }
    for r in error_reports:
        et = r.get("error_type", "task_failure") or "task_failure"
        if et in error_breakdown:
            error_breakdown[et] += 1
        else:
            error_breakdown["task_failure"] += 1

    # by_project 집계 — T-082: validate_project_name 적용
    by_project: Dict[str, int] = {}
    for r in unique_reports:
        proj = validate_project_name(r["project"])
        by_project[proj] = by_project.get(proj, 0) + 1

    # T-089: 반환 전 일괄 정규화
    for item in unique_reports:
        item['project'] = _validate_project_name(item.get('project', 'AADS'))

    return {
        "status": "ok",
        "total": len(unique_reports),
        "unique_reports": len(unique_reports),
        "success": success_count,
        "error": len(error_reports),     # T-072: 숫자로만 (React Error #31 방지)
        "error_breakdown": error_breakdown,  # T-072: 별도 키로 분리
        "project_breakdown": by_project,
        "by_project": by_project,
        "reports": unique_reports,
    }


# ─── (7) GET /dashboard/reports/{filename} ───────────────────────────────────
@router.get("/dashboard/reports/{filename:path}")
async def get_report_detail(filename: str):
    """특정 보고서 전문 반환 (마크다운 또는 project_tasks summary)"""
    # 경로 순회 방지
    if ".." in filename:
        raise HTTPException(400, "Invalid filename")

    # [REMOTE_...] 형식 처리 → project_tasks 조회
    import re as _re
    _remote_match = _re.match(r"^\[(.+?)\]\s+(.+)$", filename)
    if _remote_match:
        _source = _remote_match.group(1)
        _task_id = _remote_match.group(2).strip()
        try:
            async with memory_store.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT task_id, project, title, summary, status, completed_at FROM project_tasks WHERE task_id=$1 LIMIT 1",
                    _task_id
                )
                if row:
                    d = dict(row)
                    content_text = ("# " + str(d.get('title', _task_id)) + "\n\n" +
                        "**Task ID**: " + str(d.get('task_id','')) + "\n" +
                        "**Project**: " + str(d.get('project','')) + "\n" +
                        "**Status**: " + str(d.get('status','')) + "\n" +
                        "**Source**: " + _source + "\n" +
                        "**Completed**: " + _to_kst_str(d.get('completed_at','')) + "\n\n" +
                        "## Summary\n\n" + str(d.get('summary', '')))
                    return {"status": "ok", "filename": filename, "content": content_text, "source": "project_tasks"}
        except Exception as e:
            logger.warning(f"report detail DB 조회 실패: {e}")
        raise HTTPException(404, f"Report '{filename}' not found in DB")

    # 로컬 파일 검색
    if "/" in filename:
        raise HTTPException(400, "Invalid filename")
    candidate = REPORTS_LOCAL_DIR / filename
    if not candidate.exists():
        candidate = DIRECTIVES_DONE_DIR / filename
    if not candidate.exists():
        # Archived 검색
        archived = Path("/root/.genspark/directives/archived")
        if archived.exists():
            matches = list(archived.rglob(filename))
            if matches:
                candidate = matches[0]
            else:
                candidate = None
        else:
            candidate = None

    if not candidate or not candidate.exists():
        # project_tasks fallback: task_id로 검색
        try:
            async with memory_store.pool.acquire() as conn:
                _tid = filename.replace("_RESULT.md", "").replace(".md", "")
                row = await conn.fetchrow(
                    "SELECT task_id, project, title, summary, status, completed_at FROM project_tasks WHERE task_id=$1 LIMIT 1",
                    _tid
                )
                if row:
                    d = dict(row)
                    content_text = ("# " + str(d.get('title', _tid)) + "\n\n" +
                        "**Summary**\n\n" + str(d.get('summary', '내용 없음')))
                    return {"status": "ok", "filename": filename, "content": content_text, "source": "project_tasks_fallback"}
        except Exception:
            pass
        raise HTTPException(404, f"Report '{filename}' not found")

    try:
        file_content = candidate.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, f"Failed to read report: {e}")

    return {
        "status": "ok",
        "filename": filename,
        "content": file_content,
    }


# ─── (7b) GET /dashboard/directives/{task_id} ────────────────────────────────
@router.get("/dashboard/directives/{task_id}")
async def get_directive_detail(task_id: str):
    """특정 지시서/결과 내용 반환 (task_id 또는 파일명)"""
    if ".." in task_id or "/" in task_id:
        raise HTTPException(400, "Invalid task_id")

    # 1) 로컬 파일 검색 (done 디렉터리)
    if task_id.endswith(".md"):
        for candidate in [DIRECTIVES_DONE_DIR / task_id]:
            if candidate.exists():
                try:
                    return {"status": "ok", "task_id": task_id, "content": candidate.read_text(encoding="utf-8", errors="replace"), "source": "file"}
                except Exception as e:
                    raise HTTPException(500, str(e))

    # 2) 파일 패턴 검색 (done 디렉터리에서 task_id 포함 파일)
    if DIRECTIVES_DONE_DIR.exists():
        matches = list(DIRECTIVES_DONE_DIR.glob(f"*{task_id}*"))
        if matches:
            try:
                return {"status": "ok", "task_id": task_id, "content": matches[0].read_text(encoding="utf-8", errors="replace"), "source": "file", "filename": matches[0].name}
            except Exception:
                pass

    # 3) archived 디렉터리 검색
    archived = Path("/root/.genspark/directives/archived")
    if archived.exists():
        matches = list(archived.rglob(f"*{task_id}*"))
        if matches:
            try:
                return {"status": "ok", "task_id": task_id, "content": matches[0].read_text(encoding="utf-8", errors="replace"), "source": "archived", "filename": matches[0].name}
            except Exception:
                pass

    # 4) project_tasks DB 조회 (summary 반환)
    try:
        async with memory_store.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT task_id, project, title, summary, status, completed_at FROM project_tasks WHERE task_id=$1 LIMIT 1",
                task_id
            )
            if row:
                d = dict(row)
                content_text = ("# " + str(d.get('title','')) + "\n\n" +
                    "**Task ID**: " + str(d.get('task_id','')) + "\n" +
                    "**Project**: " + str(d.get('project','')) + "\n" +
                    "**Status**: " + str(d.get('status','')) + "\n" +
                    "**Completed**: " + _to_kst_str(d.get('completed_at','')) + "\n\n" +
                    "## Summary\n\n" + str(d.get('summary','')))
                return {"status": "ok", "task_id": task_id, "content": content_text, "source": "project_tasks"}
    except Exception as e:
        logger.warning(f"directive detail DB 조회 실패: {e}")

    raise HTTPException(404, f"Directive '{task_id}' not found")


# ─── (8) GET /dashboard/task-history ─────────────────────────────────────────
@router.get("/dashboard/task-history")
async def get_task_history():
    """원격 서버 작업 이력 (go100_user_memory task_result 타입)"""
    try:
        async with memory_store.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, memory_type, content, importance, created_at
                FROM go100_user_memory
                WHERE user_id = 2
                  AND (memory_type LIKE 'task_result%' OR memory_type LIKE 'cross_msg_%')
                ORDER BY created_at DESC
                LIMIT 200
                """
            )

            # go100_user_memory의 cross_msg 최신 시각으로 원격 서버 health 확인
            cross_msg_rows = await conn.fetch(
                """
                SELECT
                    CASE WHEN memory_type LIKE '%REMOTE_211%' THEN 'REMOTE_211'
                         WHEN memory_type LIKE '%REMOTE_114%' THEN 'REMOTE_114' END AS agent,
                    MAX(created_at) AS last_msg
                FROM go100_user_memory
                WHERE user_id = 2
                  AND (memory_type LIKE '%REMOTE_211%' OR memory_type LIKE '%REMOTE_114%')
                GROUP BY 1
                """
            )

        tasks: List[Dict] = []
        for r in rows:
            content = r["content"] if isinstance(r["content"], dict) else _parse_value(r["content"])
            task_id = content.get("task_id", content.get("id", str(r["id"])))
            server = content.get("server", content.get("agent_id", "unknown"))
            started_at = _to_kst_str(content.get("started_at") or r["created_at"])
            finished_at = _to_kst_str(content.get("finished_at") or content.get("completed_at", ""))
            from_agent = content.get("from_agent", content.get("agent_id", ""))

            # body / details.body에서 JSON 파싱
            body_raw = content.get("body", "")
            if not body_raw and isinstance(content.get("details"), dict):
                body_raw = content["details"].get("body", "")
            body_parsed: Dict = {}
            if isinstance(body_raw, str) and body_raw:
                try:
                    body_parsed = json.loads(body_raw)
                except Exception:
                    pass
            elif isinstance(body_raw, dict):
                body_parsed = body_raw

            # message_type: body > content > memory_type에서 추출
            message_type = (
                body_parsed.get("message_type")
                or content.get("message_type")
                or content.get("details", {}).get("message_type", "")
                if isinstance(content.get("details"), dict) else
                body_parsed.get("message_type") or content.get("message_type", "")
            )
            mem_type = r["memory_type"]  # e.g. task_result, cross_msg_...

            # status / finished_at 결정 로직
            mt_lower = (message_type or "").lower()
            if mt_lower in ("notify", "auto_report") or "auto_report" in mt_lower:
                status = "reported"
                finished_at = started_at  # 보고 시점 = 완료 시점
            elif mt_lower == "install_complete" or "install_complete" in mt_lower:
                status = "completed"
                if not finished_at:
                    finished_at = started_at
            elif mem_type.startswith("task_result") or mt_lower == "task_result":
                # task_result: content의 success/error 여부로 판단
                raw_status = (
                    content.get("status")
                    or content.get("result_status")
                    or body_parsed.get("status", "")
                )
                raw_lower = (raw_status or "").lower()
                if raw_lower in ("error", "fail", "failed"):
                    status = "error"
                else:
                    status = "completed"
                if not finished_at:
                    finished_at = started_at
            else:
                # 기본: content 직접 → body → 'active'
                raw_status = (
                    content.get("status")
                    or content.get("result_status")
                    or body_parsed.get("status", "")
                )
                status = raw_status if raw_status else "active"

            tasks.append({
                "task_id": task_id,
                "server": server,
                "status": status,
                "message_type": message_type or mem_type,
                "started_at": started_at,
                "finished_at": finished_at,
                "from_agent": from_agent,
                "memory_type": mem_type,
            })

        # 원격 서버 health: cross_msg 최신 시각 기준 5분 이내 → online
        remote_servers: Dict[str, Dict] = {
            "REMOTE_211": {"name": "REMOTE_211", "health": "offline", "last_msg": None},
            "REMOTE_114": {"name": "REMOTE_114", "health": "offline", "last_msg": None},
        }
        now = datetime.now(timezone.utc)
        for a in cross_msg_rows:
            agent = a["agent"]
            if not agent or agent not in remote_servers:
                continue
            lm = a["last_msg"]
            if lm:
                lm_aware = lm if lm.tzinfo else lm.replace(tzinfo=timezone.utc)
                diff_min = (now - lm_aware).total_seconds() / 60
                health = "online" if diff_min < 5 else "offline"
                remote_servers[agent] = {
                    "name": agent,
                    "health": health,
                    "last_msg": str(lm),
                    "minutes_ago": round(diff_min, 1),
                }

        return {
            "status": "ok",
            "total": len(tasks),
            "tasks": tasks,
            "remote_servers": list(remote_servers.values()),
        }

    except Exception as e:
        logger.error(f"task_history error: {e}")
        raise HTTPException(500, f"Task history error: {e}")


# ─── (9) GET /dashboard/analytics ─────────────────────────────────────────── T-080
@router.get("/dashboard/analytics")
async def get_analytics():
    """비용/시간 분석 — directives 파일 기반 + DB 보조 (T-080)"""
    try:
        # ── Part 1: 지시서 파일 기반 통계 ─────────────────────────────────────
        all_directives: List[Dict] = []
        if DIRECTIVES_RUNNING_DIR.exists():
            for f in DIRECTIVES_RUNNING_DIR.glob("*.md"):
                all_directives.append(_parse_directive_file(f, "running"))
        if DIRECTIVES_DONE_DIR.exists():
            for f in DIRECTIVES_DONE_DIR.glob("*.md"):
                all_directives.append(_parse_directive_file(f, "completed"))

        total_tasks = len(all_directives)
        completed_tasks = sum(1 for d in all_directives if d["status"] == "completed")
        error_tasks = sum(1 for d in all_directives if d["status"] == "error")
        running_tasks = sum(1 for d in all_directives if d["status"] == "running")

        # success_rate = completed / (completed + error) * 100 (T-080)
        denom = completed_tasks + error_tasks
        success_rate = round(completed_tasks / denom * 100, 1) if denom > 0 else 0.0

        # avg_task_duration_min — 파일명 날짜 ~ mtime 기반 추정
        durations = []
        if DIRECTIVES_DONE_DIR.exists():
            for f_path in DIRECTIVES_DONE_DIR.glob("*.md"):
                fname_match = re.search(r"(\d{8}_\d{6})", f_path.name)
                if fname_match:
                    try:
                        dt_start = datetime.strptime(fname_match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=KST)
                        dt_end = datetime.fromtimestamp(f_path.stat().st_mtime, tz=KST)
                        diff_min = (dt_end - dt_start).total_seconds() / 60
                        if 0 < diff_min < 480:  # 0~8시간 범위만 유효값으로 처리
                            durations.append(diff_min)
                    except Exception:
                        pass
        avg_task_duration_min = round(sum(durations) / len(durations), 1) if durations else -1.0

        # by_project 집계 (directives 기반) — T-082: validate_project_name 적용
        by_project_dir: Dict[str, Dict] = defaultdict(lambda: {"completed": 0, "error": 0, "total": 0})
        for d in all_directives:
            proj = validate_project_name(d["project"])
            by_project_dir[proj]["total"] += 1
            if d["status"] == "completed":
                by_project_dir[proj]["completed"] += 1
            elif d["status"] == "error":
                by_project_dir[proj]["error"] += 1

        # daily_trend — 지시서 파일 created_at 기반 (최근 7일) — cost는 DB 쿼리 후 채움
        now_kst = datetime.now(KST)
        daily_count: Dict[str, int] = defaultdict(int)
        for d in all_directives:
            ca = d.get("created_at", "")
            if ca and len(ca) >= 10:
                daily_count[ca[:10]] += 1
        daily_cost_map: Dict[str, float] = {}  # T-095: async block에서 채워짐

        # error_distribution — directive error_type 분류
        error_type_count: Dict[str, int] = defaultdict(int)
        for d in all_directives:
            if d["status"] == "error" and d.get("error_type"):
                error_type_count[d["error_type"]] += 1
        error_distribution = [
            {"error_type": et, "count": cnt}
            for et, cnt in sorted(error_type_count.items(), key=lambda x: -x[1])
        ]

        # ── Part 2: DB 쿼리 (비용/토큰, 서버별) ───────────────────────────────
        async with memory_store.pool.acquire() as conn:
            try:
                aads_conv_rows = await conn.fetch(
                    """
                    SELECT project,
                           COUNT(*) AS cnt,
                           COALESCE(SUM(total_tokens), 0) AS tokens,
                           COALESCE(SUM(total_cost), 0) AS cost
                    FROM aads_conversations
                    GROUP BY project
                    """
                )
            except Exception:
                aads_conv_rows = []

            server_rows = await conn.fetch(
                """
                SELECT
                    CASE
                        WHEN memory_type LIKE '%REMOTE_211%' THEN 'REMOTE_211'
                        WHEN memory_type LIKE '%REMOTE_114%' THEN 'REMOTE_114'
                        ELSE SPLIT_PART(REPLACE(memory_type, 'cross_msg_', ''), '_AADS_MGR', 1)
                    END AS server,
                    COUNT(*) AS tasks,
                    MAX(created_at) AS last_report
                FROM go100_user_memory
                WHERE user_id = 2
                  AND memory_type LIKE 'cross_msg_%'
                GROUP BY 1
                ORDER BY tasks DESC
                """
            )

            # T-083: task_cost_log 테이블에서 실제 비용 집계 (async with 블록 내)
            try:
                cost_rows = await conn.fetch(
                    """
                    SELECT project,
                           COALESCE(SUM(total_tokens),0) AS tot_tok,
                           COALESCE(SUM(cost_usd),0) AS cost
                    FROM task_cost_log
                    GROUP BY project
                    """
                )
            except Exception:
                cost_rows = []

            # T-095: 일별 비용 집계 (daily_trend에 반영)
            try:
                daily_cost_rows = await conn.fetch(
                    """
                    SELECT DATE(logged_at AT TIME ZONE 'Asia/Seoul') AS day,
                           COALESCE(SUM(cost_usd), 0) AS cost
                    FROM task_cost_log
                    GROUP BY 1
                    """
                )
                daily_cost_map = {str(r["day"]): float(r["cost"]) for r in daily_cost_rows}
            except Exception:
                daily_cost_map = {}

        # T-095: daily_trend 완성 (cost는 DB 쿼리 결과 반영)
        daily_trend = []
        for i in range(6, -1, -1):
            day = (now_kst - timedelta(days=i)).strftime("%Y-%m-%d")
            daily_trend.append({"date": day, "tasks": daily_count.get(day, 0), "cost_usd": daily_cost_map.get(day, 0.0)})

        now_utc = datetime.now(timezone.utc)

        # aads_conversations 집계 (비용/토큰)
        total_tokens = 0
        total_cost_usd = 0.0
        total_conversations = 0
        by_project_conv: Dict[str, Dict] = {}
        for r in aads_conv_rows:
            proj = validate_project_name(CONV_PROJECT_MAP.get(r["project"] or "", r["project"] or "AADS"))
            cnt = int(r["cnt"])
            tok = int(r["tokens"])
            cost = float(r["cost"])
            total_conversations += cnt
            total_tokens += tok
            total_cost_usd += cost
            if proj not in by_project_conv:
                by_project_conv[proj] = {"conversations": 0, "tokens": 0, "cost_usd": 0.0}
            by_project_conv[proj]["conversations"] += cnt
            by_project_conv[proj]["tokens"] += tok
            by_project_conv[proj]["cost_usd"] += cost

        cost_total_usd = 0.0
        cost_total_tokens = 0
        for cr in cost_rows:
            proj = _normalize_project(cr["project"] or "AADS")
            cost = float(cr["cost"])
            tok = int(cr["tot_tok"])
            cost_total_usd += cost
            cost_total_tokens += tok
            mapped = CONV_PROJECT_MAP.get(proj.lower(), proj)
            if mapped not in by_project_conv:
                by_project_conv[mapped] = {"conversations": 0, "tokens": 0, "cost_usd": 0.0}
            by_project_conv[mapped]["tokens"] += tok
            by_project_conv[mapped]["cost_usd"] += cost

        # task_cost_log 우선, 없으면 aads_conversations fallback
        # T-092: cost_status = 'active' if count > 0 else 'no_data'
        if cost_total_usd > 0:
            total_cost_usd = cost_total_usd
            total_tokens = cost_total_tokens
            cost_status = "active"
            cost_message = ""
        elif len(cost_rows) > 0:
            # 레코드는 있지만 비용이 0인 경우
            cost_status = "active"
            cost_message = ""
        elif total_cost_usd == 0.0 and not aads_conv_rows:
            total_cost_usd = 0.0
            cost_status = "no_data"
            cost_message = "비용 데이터 없음"
        else:
            cost_status = "active"
            cost_message = ""

        # T-090: project_tasks 기반 집계 추가
        try:
            async with memory_store.pool.acquire() as conn:
                tbl_exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='project_tasks')"
                )
                if tbl_exists:
                    pt_agg_rows = await conn.fetch(
                        """
                        SELECT project,
                               COUNT(*) AS total,
                               SUM(CASE WHEN status IN ('completed','done','success') THEN 1 ELSE 0 END) AS completed,
                               SUM(CASE WHEN status IN ('error','failed','fail') THEN 1 ELSE 0 END) AS error
                        FROM project_tasks
                        GROUP BY project
                        """
                    )
                    for r in pt_agg_rows:
                        proj = _validate_project_name(r["project"] or "AADS")
                        if proj not in by_project_dir:
                            by_project_dir[proj] = {"completed": 0, "error": 0, "total": 0}
                        # project_tasks 수치 병합 (중복 방지: local는 이미 directives 파일에서 집계됨)
                        by_project_dir[proj]["total"] = max(by_project_dir[proj]["total"], int(r["total"]))
                        by_project_dir[proj]["completed"] = max(by_project_dir[proj]["completed"], int(r["completed"]))
                        by_project_dir[proj]["error"] = max(by_project_dir[proj]["error"], int(r["error"]))
        except Exception as e:
            logger.warning(f"project_tasks analytics 조회 실패 (무시): {e}")

        # by_project 최종 병합 (directives + conversations)
        all_projs = set(by_project_dir.keys()) | set(by_project_conv.keys())
        _merged: Dict[str, Dict] = {}
        for proj in all_projs:
            norm = _validate_project_name(proj)
            dir_info = by_project_dir.get(proj, {})
            conv_info = by_project_conv.get(proj, {})
            if norm not in _merged:
                _merged[norm] = {"conversations": 0, "cost_usd": 0.0, "tokens": 0,
                                  "completed": 0, "error": 0, "total": 0}
            _merged[norm]["conversations"] += conv_info.get("conversations", 0)
            _merged[norm]["cost_usd"] += conv_info.get("cost_usd", 0.0)
            _merged[norm]["tokens"] += conv_info.get("tokens", 0)
            _merged[norm]["completed"] = max(_merged[norm]["completed"], dir_info.get("completed", 0))
            _merged[norm]["error"] = max(_merged[norm]["error"], dir_info.get("error", 0))
            _merged[norm]["total"] = max(_merged[norm]["total"], dir_info.get("total", 0))
        by_project = [
            {"project": k, "conversations": v["conversations"],
             "cost_usd": round(v["cost_usd"], 6), "tokens": v["tokens"],
             "last_activity": "", "completed": v["completed"],
             "error": v["error"], "total": v["total"]}
            for k, v in sorted(_merged.items())
            if v["total"] > 0 or v["conversations"] > 0
        ]

        # 서버별 현황 (cross_msg 기반)
        active_servers = 0
        by_server = []
        _seen_servers: set = set()
        for r in server_rows:
            svr = r["server"] or ""
            # 오염된 서버 이름 필터링: 25자 초과, AADS_MGR 포함, 중복
            if len(svr) > 25 or "AADS_MGR" in svr or svr in _seen_servers:
                continue
            _seen_servers.add(svr)
            lr = r["last_report"]
            lr_aware = lr.replace(tzinfo=timezone.utc) if lr and not lr.tzinfo else lr
            diff_min = (now_utc - lr_aware).total_seconds() / 60 if lr_aware else 9999
            if diff_min < 5:
                active_servers += 1
            by_server.append({
                "server": svr,
                "tasks": int(r["tasks"]),
                "status": "online" if diff_min < 5 else "offline",
                "last_report": _to_kst_str(lr_aware) if lr else "",
            })

        return {
            "status": "ok",
            "generated_at": _now_kst(),
            "summary": {
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "error_tasks": error_tasks,
                "running_tasks": running_tasks,
                "success_rate": success_rate,
                "total_conversations": total_conversations,
                "total_cost_usd": round(total_cost_usd, 6),
                "cost_status": cost_status,
                "cost_message": cost_message,
                "total_tokens": total_tokens,
                "avg_task_duration_min": avg_task_duration_min,
                "active_servers": active_servers,
            },
            "by_project": by_project,
            "by_server": by_server,
            "daily_trend": daily_trend,
            "error_distribution": error_distribution,
        }

    except Exception as e:
        logger.error(f"analytics error: {e}")
        raise HTTPException(500, f"Analytics error: {e}")


# ─── (10-a) POST /dashboard/complete-running ─────────────────────────────────
# 프로젝트 → 담당 서버 SSH 매핑
_PROJECT_SERVER_MAP = {
    "KIS": "root@211.188.53.126",
    "GO100": "root@211.188.51.113",
    "AADS": "local",  # aads-server 자신의 호스트 마운트 없음 → SSH로 68 접근
    "SF": "root@114.203.209.93",
    "SHORTFLOW": "root@114.203.209.93",
    "NAS": "root@114.203.209.93",
    "NEWTALK": "root@114.203.209.93",
    "NTV2": "root@114.203.209.93",
    "SALES": "root@114.203.209.93",
}
_RUNNING_DIR = "/root/.genspark/directives/running"

def _is_task_still_running_on_server(task_id: str, server_ssh: str) -> bool:
    """해당 서버의 running/ 디렉토리에 task_id가 포함된 파일이 있으면 True(실제 실행 중)"""
    import subprocess, shlex
    try:
        clean_id = task_id.replace("KIS-", "T-").replace("GO100-", "T-").replace("AADS-", "T-")
        raw_id = task_id.split("-", 1)[-1] if "-" in task_id else task_id
        safe_raw_id = shlex.quote(raw_id)
        safe_task_id = shlex.quote(task_id)
        if server_ssh == "local":
            result = subprocess.run(
                ["sh", "-c", f"grep -rl {safe_raw_id}'\\|'{safe_task_id} {_RUNNING_DIR}/ 2>/dev/null | head -1"],
                capture_output=True, text=True, timeout=5
            )
        else:
            result = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5", server_ssh,
                 f"grep -rl {safe_raw_id} {_RUNNING_DIR}/ 2>/dev/null | head -1"],
                capture_output=True, text=True, timeout=8
            )
        return bool(result.stdout.strip())
    except Exception as e:
        logger.warning(f"running 확인 실패({task_id}@{server_ssh}): {e}")
        return False  # 확인 불가 시 완료 처리 안 함 (보수적)

@router.post("/dashboard/complete-running")
async def complete_running_tasks(project: Optional[str] = None):
    """running 태스크 중 서버 running/ 디렉토리에 없는 것만 completed 처리 (실제 완료 검증)"""
    import asyncio
    skipped_ids: List[str] = []   # 실제 실행 중 → 건너뜀
    verified_ids: List[str] = []  # 파일 없음 = 완료 확인
    unverified_ids: List[str] = []  # SSH 실패 등 확인 불가

    try:
        async with memory_store.pool.acquire() as conn:
            if project and project != "all":
                rows = await conn.fetch(
                    "SELECT task_id, project, source FROM project_tasks WHERE status='running' AND project=$1",
                    project
                )
            else:
                rows = await conn.fetch(
                    "SELECT task_id, project, source FROM project_tasks WHERE status='running'"
                )

        # 각 task 실제 실행 여부 병렬 확인
        loop = asyncio.get_event_loop()
        for row in rows:
            tid = row["task_id"]
            proj = (row["project"] or "").upper()
            server_ssh = _PROJECT_SERVER_MAP.get(proj, "root@211.188.53.126")
            still_running = await loop.run_in_executor(
                None, _is_task_still_running_on_server, tid, server_ssh
            )
            if still_running:
                skipped_ids.append(tid)
            else:
                verified_ids.append(tid)

        # 검증 완료된 것만 DB 업데이트
        updated = 0
        if verified_ids:
            async with memory_store.pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE project_tasks SET status='completed', completed_at=NOW() WHERE task_id = ANY($1::varchar[]) AND status='running'",
                    verified_ids
                )
                updated = int(result.split()[-1]) if result else 0

    except Exception as e:
        logger.error(f"complete_running error: {e}")
        raise HTTPException(500, str(e))

    msg_parts = []
    if updated:
        msg_parts.append(f"{updated}건 완료 처리")
    if skipped_ids:
        msg_parts.append(f"{len(skipped_ids)}건 실행 중 (유지): {', '.join(skipped_ids)}")
    if not msg_parts:
        msg_parts.append("완료 처리할 항목 없음")

    logger.info(f"complete_running: 완료={updated}, 실행중유지={len(skipped_ids)}, 확인불가={len(unverified_ids)}")
    return {
        "status": "ok",
        "updated": updated,
        "skipped": skipped_ids,
        "message": " / ".join(msg_parts),
    }


# ─── (10) POST /dashboard/cost-log — T-083 ───────────────────────────────────
from pydantic import BaseModel

class CostLogEntry(BaseModel):
    task_id: str
    session_id: str = ""
    model: str = "claude-sonnet-4-6"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    project: str = "AADS"
    server: str = ""


@router.get("/dashboard/costs")
async def get_costs():
    """비용 추적 현황 — task_cost_log 기반 (T-089, T-092)"""
    try:
        async with memory_store.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT task_id, project, model, input_tokens, output_tokens,
                       total_tokens, cost_usd, logged_at
                FROM task_cost_log
                ORDER BY logged_at DESC
                LIMIT 100
                """
            )
            summary_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total_entries,
                    COALESCE(SUM(cost_usd), 0) AS total_cost_usd,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM task_cost_log
                """
            )
            by_project_rows = await conn.fetch(
                """
                SELECT project,
                       COUNT(*) AS entries,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd,
                       COALESCE(SUM(total_tokens), 0) AS tokens
                FROM task_cost_log
                GROUP BY project
                ORDER BY cost_usd DESC
                """
            )
            # T-092: project + model_id 기반 집계
            by_project_model_rows = await conn.fetch(
                """
                SELECT project,
                       COALESCE(model, 'unknown') AS model_id,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM task_cost_log
                GROUP BY project, model
                ORDER BY cost_usd DESC
                """
            )

        entries = []
        for r in rows:
            entries.append({
                "task_id": r["task_id"],
                "project": _validate_project_name(r["project"] or "AADS"),
                "model": r["model"] or "",
                "input_tokens": r["input_tokens"] or 0,
                "output_tokens": r["output_tokens"] or 0,
                "total_tokens": r["total_tokens"] or 0,
                "cost_usd": float(r["cost_usd"] or 0),
                "logged_at": _to_kst_str(r["logged_at"]),
            })

        by_project = []
        for r in by_project_rows:
            by_project.append({
                "project": _validate_project_name(r["project"] or "AADS"),
                "entries": int(r["entries"]),
                "cost_usd": float(r["cost_usd"] or 0),
                "tokens": int(r["tokens"] or 0),
            })

        # T-092: project + model_id 집계 응답 추가
        by_project_model = []
        for r in by_project_model_rows:
            by_project_model.append({
                "project": _validate_project_name(r["project"] or "AADS"),
                "model_id": r["model_id"] or "unknown",
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cost_usd": round(float(r["cost_usd"] or 0), 6),
            })

        cost_count = int(summary_row["total_entries"])
        return {
            "status": "ok",
            "summary": {
                "total_entries": cost_count,
                "total_cost_usd": round(float(summary_row["total_cost_usd"]), 6),
                "total_tokens": int(summary_row["total_tokens"]),
                "cost_status": "active" if cost_count > 0 else "no_data",
            },
            "by_project": by_project,
            "by_project_model": by_project_model,
            "entries": entries,
        }
    except Exception as e:
        logger.error(f"costs error: {e}")
        raise HTTPException(500, f"Costs error: {e}")


@router.post("/dashboard/cost-log")
async def post_cost_log(entry: CostLogEntry):
    """Claude API 비용 로그 기록 (T-083).
    원격 에이전트/브릿지에서 task 완료 시 호출.
    """
    total_tokens = entry.input_tokens + entry.output_tokens
    proj = _normalize_project(entry.project)
    try:
        async with memory_store.pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO task_cost_log
                    (task_id, session_id, model, input_tokens, output_tokens,
                     total_tokens, cost_usd, project, server, logged_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())
                RETURNING id
                """,
                entry.task_id, entry.session_id, entry.model,
                entry.input_tokens, entry.output_tokens,
                total_tokens, entry.cost_usd, proj, entry.server,
            )
        return {"status": "ok", "id": row_id, "total_tokens": total_tokens, "cost_usd": entry.cost_usd}
    except Exception as e:
        logger.error(f"cost_log error: {e}")
        raise HTTPException(500, f"cost_log error: {e}")
