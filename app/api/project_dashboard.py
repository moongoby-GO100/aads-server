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


def _classify_project(content: str) -> str:
    """보고서/지시서 내용에서 프로젝트 자동 분류 (T-074: 정확도 개선 - AADS 1순위)"""
    content_lower = content.lower()
    # 1순위: AADS 자체 작업 (가장 먼저 체크)
    aads_keywords = ['aads', 'dashboard', 'ceo chat', 'ceo 채팅', '대시보드', 'handover',
                     'tasks 페이지', 'task-history', 'project_dashboard',
                     'cost', '비용', '분석', 'remote', '원격', 'bridge', '브릿지',
                     'memory', 'context api', '계층 메모리', '모델 분기', '실행 엔진']
    if any(kw in content_lower for kw in aads_keywords):
        return 'AADS'
    # 2순위: 프로젝트별 (정확 매칭)
    if any(kw in content_lower for kw in ['kis-autotrade', 'kis_autotrade', '주식', 'autotrade', '백억이']):
        return 'KIS'
    if any(kw in content_lower for kw in ['shortflow', '쇼츠', 'shorts', '템빨', 'youtube short']):
        return 'ShortFlow'
    if any(kw in content_lower for kw in ['newtalk', '뉴톡', 'newtalk_v2']):
        return 'NewTalk'
    if any(kw in content_lower for kw in ['nasync', 'nas동기화']):
        return 'NAS'
    if any(kw in content_lower for kw in ['go100', 'go_100']):
        return 'GO100'
    # 기본값
    return 'AADS'


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
    if yaml_match:
        yaml_block = yaml_match.group(1)
        for line in yaml_block.splitlines():
            line = line.strip()
            if line.startswith("task_id:"):
                val = line.split(":", 1)[1].strip()
                if re.match(r"T-\d+", val):
                    task_id = val
            elif line.startswith("project:"):
                project = line.split(":", 1)[1].strip()
            elif line.startswith("status:"):
                status = line.split(":", 1)[1].strip()
            elif line.startswith("completed_at:"):
                created_at = line.split(":", 1)[1].strip()
        # 제목은 파일명에서 유추
        title_match = re.search(r"제목[:\s]+(.+)", raw)
        if title_match:
            title = title_match.group(1).strip()
        else:
            title = filename
    else:
        # 일반 텍스트 형식
        m_title = re.search(r"제목\s*[:\s]+(.+)", raw)
        if m_title:
            title = m_title.group(1).strip()
        m_proj = re.search(r"프로젝트\s*[:\s]+(.+)", raw)
        if m_proj:
            project = m_proj.group(1).strip()

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
        project = _classify_project(raw[:2000])

    # 파일명에서 날짜 추출 (AADS_YYYYMMDD_HHMMSS_...)
    fname_dt = re.search(r"(\d{8}_\d{6})", filename)
    if fname_dt and not created_at:
        dt_str = fname_dt.group(1)
        try:
            dt = datetime.strptime(dt_str, "%Y%m%d_%H%M%S").replace(tzinfo=KST)
            created_at = dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except Exception:
            pass

    # 에러 유형 분류 (T-072: 지시서에도 error_type 포함)
    error_type = _classify_error(raw[:2000]) if status == "error" else None

    return {
        "task_id": task_id,
        "title": title,
        "status": status,
        "project": project,
        "error_type": error_type or "",
        "created_at": created_at,
        "started_at": created_at,
        "completed_at": created_at if default_status == "completed" else "",
        "duration_seconds": None,
        "file_path": str(filepath),
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
                project = line.split(":", 1)[1].strip()
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

    # 프로젝트 자동 분류 (project가 기본값이면 내용+제목으로 분류)
    if project == "AADS":
        project = _classify_project(head + " " + filename)

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


# ─── (5) GET /dashboard/directives ───────────────────────────────────────────
@router.get("/dashboard/directives")
async def get_directives(project: Optional[str] = None):
    """작업지시서 현황: running + done 디렉터리 스캔"""
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

    # by_project / project_breakdown 집계
    by_project: Dict[str, int] = {}
    for d in unique_directives:
        proj = d["project"]
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
    """작업결과보고서 목록"""
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

    # by_project 집계
    by_project: Dict[str, int] = {}
    for r in unique_reports:
        proj = r["project"]
        by_project[proj] = by_project.get(proj, 0) + 1

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
@router.get("/dashboard/reports/{filename}")
async def get_report_detail(filename: str):
    """특정 보고서 전문 반환 (마크다운)"""
    # 경로 순회 방지
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")

    # 로컬 reports 디렉터리 우선
    candidate = REPORTS_LOCAL_DIR / filename
    if not candidate.exists():
        candidate = DIRECTIVES_DONE_DIR / filename
    if not candidate.exists():
        raise HTTPException(404, f"Report '{filename}' not found")

    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, f"Failed to read report: {e}")

    return {
        "status": "ok",
        "filename": filename,
        "content": content,
    }


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


# ─── (9) GET /dashboard/analytics ─────────────────────────────────────────── T-070
@router.get("/dashboard/analytics")
async def get_analytics():
    """비용/시간 분석 — aads_conversations + cross_msg + directives 집계 (T-070)"""
    try:
        async with memory_store.pool.acquire() as conn:
            # aads_conversations: project별 대화수/토큰/비용
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

            # go100_user_memory cross_msg 타입별 집계
            cross_type_rows = await conn.fetch(
                """
                SELECT memory_type, COUNT(*) AS cnt, MAX(created_at) AS last_at
                FROM go100_user_memory
                WHERE user_id = 2
                  AND memory_type LIKE 'cross_msg_%'
                GROUP BY memory_type
                ORDER BY cnt DESC
                """
            )

            # 태스크 상태 집계 (task_result + cross_msg)
            task_rows = await conn.fetch(
                """
                SELECT memory_type, content, created_at
                FROM go100_user_memory
                WHERE user_id = 2
                  AND (memory_type LIKE 'task_result%' OR memory_type LIKE 'cross_msg_%')
                ORDER BY created_at DESC
                LIMIT 500
                """
            )

            # 일별 트렌드 (최근 7일)
            daily_rows = await conn.fetch(
                """
                SELECT DATE(created_at) AS d, COUNT(*) AS cnt
                FROM go100_user_memory
                WHERE user_id = 2
                  AND memory_type LIKE 'cross_msg_%'
                  AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY DATE(created_at)
                ORDER BY d
                """
            )

            # 원격 서버별 집계
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

        now_utc = datetime.now(timezone.utc)

        # aads_conversations 집계
        total_tokens = 0
        total_cost_usd = 0.0
        total_conversations = 0
        by_project_map: Dict[str, Dict] = {}
        for r in aads_conv_rows:
            proj = CONV_PROJECT_MAP.get(r["project"] or "", r["project"] or "unknown")
            cnt = int(r["cnt"])
            tok = int(r["tokens"])
            cost = float(r["cost"])
            total_conversations += cnt
            total_tokens += tok
            total_cost_usd += cost
            if proj not in by_project_map:
                by_project_map[proj] = {"conversations": 0, "tokens": 0, "cost_usd": 0.0}
            by_project_map[proj]["conversations"] += cnt
            by_project_map[proj]["tokens"] += tok
            by_project_map[proj]["cost_usd"] += cost

        # 태스크 상태 집계
        total_tasks = 0
        completed_tasks = 0
        error_tasks = 0
        for r in task_rows:
            total_tasks += 1
            content = r["content"] if isinstance(r["content"], dict) else _parse_value(r["content"])
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
            mt = (
                body_parsed.get("message_type") or content.get("message_type", "")
            ).lower()
            raw_s = (content.get("status") or body_parsed.get("status", "")).lower()
            if r["memory_type"].startswith("task_result"):
                if raw_s in ("error", "fail", "failed"):
                    error_tasks += 1
                else:
                    completed_tasks += 1
            elif mt in ("notify", "auto_report"):
                completed_tasks += 1

        success_rate = round(completed_tasks / total_tasks * 100, 1) if total_tasks > 0 else 0.0

        # 지시서 폴더 통계 (done 디렉터리)
        dir_completed = 0
        dir_error = 0
        if DIRECTIVES_DONE_DIR.exists():
            for f in DIRECTIVES_DONE_DIR.glob("*.md"):
                parsed = _parse_directive_file(f, "completed")
                if parsed["status"] == "error":
                    dir_error += 1
                else:
                    dir_completed += 1

        # 활성 서버 수 (최근 5분 cross_msg)
        active_servers = 0
        by_server = []
        for r in server_rows:
            lr = r["last_report"]
            lr_aware = lr.replace(tzinfo=timezone.utc) if lr and not lr.tzinfo else lr
            diff_min = (now_utc - lr_aware).total_seconds() / 60 if lr_aware else 9999
            if diff_min < 5:
                active_servers += 1
            by_server.append({
                "server": r["server"],
                "tasks": r["tasks"],
                "status": "online" if diff_min < 5 else "offline",
                "last_report": _to_kst_str(lr_aware) if lr else "",
            })

        # 프로젝트별 정리
        by_project = [
            {
                "project": proj,
                "conversations": info["conversations"],
                "cost_usd": round(info["cost_usd"], 6),
                "tokens": info["tokens"],
            }
            for proj, info in by_project_map.items()
        ]

        # 일별 트렌드
        daily_trend = [
            {"date": str(r["d"]), "tasks": r["cnt"], "cost_usd": 0.0}
            for r in daily_rows
        ]

        # cross_msg 타입별 분포 (error_distribution) — array format for frontend (T-074)
        error_distribution = [
            {"error_type": r["memory_type"], "count": int(r["cnt"])}
            for r in cross_type_rows
        ]

        # avg_task_duration_min — 현재 집계 없음: 0.0 반환
        avg_task_duration_min = 0.0

        return {
            "status": "ok",
            "generated_at": _now_kst(),
            "summary": {
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "error_tasks": error_tasks,
                "success_rate": success_rate,
                "total_conversations": total_conversations,
                "total_cost_usd": round(total_cost_usd, 6),
                "total_tokens": total_tokens,
                "avg_task_duration_min": avg_task_duration_min,
                "active_servers": active_servers,
                "directives_completed": dir_completed,
                "directives_error": dir_error,
            },
            "by_project": by_project,
            "by_server": by_server,
            "daily_trend": daily_trend,
            "error_distribution": error_distribution,
        }

    except Exception as e:
        logger.error(f"analytics error: {e}")
        raise HTTPException(500, f"Analytics error: {e}")
