"""
AADS 메모리 자동 주입 시스템 — 공유 메모리 리콜 모듈
채팅(chat_service)과 에이전트(autonomous_executor, agent_sdk_service) 공통 사용.

5개 섹션 조립 (프로젝트별 필터 적용):
1. 이전 대화 요약 (session_notes, ~500 토큰) — 해당 프로젝트 우선
2. CEO 운영 원칙/선호 (ai_observations category='ceo_preference', ~300 토큰) — 공통(전역)
3. 도구 사용 전략 (ai_observations category='tool_strategy'|'project_pattern', ~400 토큰) — 프로젝트+공통
4. 활성 Directive (directive_lifecycle status IN pending/running, ~400 토큰) — 프로젝트 필터
5. 이전 작업 발견 사항 (ai_observations category='discovery'|'learning', ~400 토큰) — 프로젝트+공통

총 토큰 예산: 2,000 토큰 이내 (한국어 기준 1토큰 ≈ 1.5자)
"""
from __future__ import annotations

import asyncio
import os
import re
import structlog
from typing import Any, Dict, List, Optional

logger = structlog.get_logger(__name__)

# 토큰 예산 (한국어 1토큰 ≈ 1.5자 → 자수 상한)
_BUDGET = {
    "session_notes": 750,       # ~500 토큰
    "preferences": 450,         # ~300 토큰
    "tool_strategy": 600,       # ~400 토큰
    "directives": 600,          # ~400 토큰
    "discoveries": 600,         # ~400 토큰
}
_TOTAL_CHAR_LIMIT = 3000  # ~2000 토큰

# #14: 카테고리별 confidence 임계값 (환경변수 오버라이드 가능)
_CONFIDENCE = {
    "ceo_preference": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "decision": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "tool_strategy": float(os.getenv("CONFIDENCE_TOOL_STRATEGY", "0.3")),
    "project_pattern": float(os.getenv("CONFIDENCE_TOOL_STRATEGY", "0.3")),
    "discovery": float(os.getenv("CONFIDENCE_DISCOVERY", "0.4")),
    "learning": float(os.getenv("CONFIDENCE_DISCOVERY", "0.4")),
    "recurring_issue": float(os.getenv("CONFIDENCE_DISCOVERY", "0.4")),
}

# #25: observation key 허용 패턴 (영문/한글/숫자/밑줄/하이픈/공백/점 — 줄바꿈 제외)
_KEY_PATTERN = re.compile(r'[^a-zA-Z0-9가-힣_\-\. ]')
_KEY_MAX_LEN = 200


def _get_pool():
    """DB 커넥션 풀 반환."""
    from app.core.db_pool import get_pool
    return get_pool()


def _sanitize_key(key: str) -> str:
    """#25: observation key 정제 — 특수문자 제거, 200자 제한."""
    cleaned = _KEY_PATTERN.sub('', key).strip()
    return cleaned[:_KEY_MAX_LEN] if cleaned else "unnamed"


def _normalize_project(project: Optional[str]) -> Optional[str]:
    """#23: project 값 항상 대문자로 정규화."""
    return project.upper().strip() if project else None


def _truncate(text: str, char_limit: int) -> str:
    """텍스트를 char_limit 이내로 자르되 줄 단위로 끊기."""
    if len(text) <= char_limit:
        return text
    lines = text.split("\n")
    result = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > char_limit:
            break
        result.append(line)
        total += len(line) + 1
    return "\n".join(result)


# ── 섹션 빌더 ────────────────────────────────────────────────────────────────

async def _build_session_notes(
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """섹션 1: 이전 대화 요약 (session_notes 최근 3개, 해당 프로젝트 우선)."""
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT summary, key_decisions, created_at, projects_discussed
                    FROM session_notes
                    ORDER BY
                        CASE WHEN $1 = ANY(projects_discussed) THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT 3
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT summary, key_decisions, created_at
                    FROM session_notes
                    ORDER BY created_at DESC
                    LIMIT 3
                    """,
                )
            if not rows:
                return ""
            lines = []
            for r in rows:
                ts = r["created_at"].strftime("%m/%d %H:%M") if r["created_at"] else ""
                line = f"- [{ts}] {r['summary']}"
                decisions = list(r.get("key_decisions") or [])
                if decisions:
                    line += f" (결정: {', '.join(decisions[:2])})"
                lines.append(line)
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["session_notes"])
    except Exception as e:
        # #13: 섹션별 실패 로깅
        logger.warning("memory_recall_section_failed", section="session_notes", error=str(e))
        return ""


async def _build_preferences() -> str:
    """섹션 2: CEO 운영 원칙/선호 — 전역 공통 (프로젝트 필터 없음)."""
    try:
        _conf = _CONFIDENCE.get("ceo_preference", 0.2)
        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value FROM ai_observations
                WHERE category IN ('ceo_preference', 'decision')
                  AND confidence >= $1
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 15
                """,
                _conf,
            )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["preferences"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="preferences", error=str(e))
        return ""


async def _build_tool_strategy(project_id: Optional[str] = None) -> str:
    """섹션 3: 도구 사용 전략 — 해당 프로젝트 + 공통(project IS NULL)."""
    try:
        _conf = _CONFIDENCE.get("tool_strategy", 0.3)
        async with _get_pool().acquire() as conn:
            # #22: 프로젝트 필터링 통일 — 항상 (project = $1 OR project IS NULL)
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('project_pattern', 'tool_strategy')
                      AND confidence >= $2
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        confidence DESC, updated_at DESC
                    LIMIT 10
                    """,
                    project_id, _conf,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('project_pattern', 'tool_strategy')
                      AND confidence >= $1
                    ORDER BY confidence DESC, updated_at DESC
                    LIMIT 10
                    """,
                    _conf,
                )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["tool_strategy"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="tool_strategy", error=str(e))
        return ""


async def _build_active_directives(project_id: Optional[str] = None) -> str:
    """섹션 4: 활성 Directive (directive_lifecycle pending/running).
    #22: 프로젝트 필터 통일 — project = $1 OR project IS NULL."""
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT task_id, title, status, priority
                    FROM directive_lifecycle
                    WHERE status IN ('pending', 'running', 'queued')
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN status = 'running' THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT 10
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT task_id, title, status, priority
                    FROM directive_lifecycle
                    WHERE status IN ('pending', 'running', 'queued')
                    ORDER BY
                        CASE WHEN status = 'running' THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT 10
                    """,
                )
            if not rows:
                return ""
            lines = []
            for r in rows:
                priority = r.get("priority") or ""
                status_icon = "🔄" if r["status"] == "running" else "⏳"
                lines.append(f"- {status_icon} [{r['task_id']}] {r['title']} ({priority})")
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["directives"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="active_directives", error=str(e))
        return ""


async def _build_discoveries(project_id: Optional[str] = None) -> str:
    """섹션 5: 이전 작업 발견 사항 — 해당 프로젝트 + 공통(project IS NULL)."""
    try:
        _conf = _CONFIDENCE.get("discovery", 0.4)
        async with _get_pool().acquire() as conn:
            # #22: 프로젝트 필터 통일
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('learning', 'recurring_issue', 'discovery')
                      AND confidence >= $2
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        updated_at DESC, confidence DESC
                    LIMIT 10
                    """,
                    project_id, _conf,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('learning', 'recurring_issue', 'discovery')
                      AND confidence >= $1
                    ORDER BY updated_at DESC, confidence DESC
                    LIMIT 10
                    """,
                    _conf,
                )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["discoveries"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="discoveries", error=str(e))
        return ""


# ── 메인 빌더 ────────────────────────────────────────────────────────────────

async def build_memory_context(
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    매 턴마다 호출하여 시스템 프롬프트에 주입할 메모리 블록을 조립.
    project_id에 따라 해당 프로젝트 메모리 우선 주입.
    총 2,000 토큰 이내. 실패 시 빈 문자열 반환 (기본 프롬프트 유지).
    """
    # #23: project 정규화
    project_id = _normalize_project(project_id)
    blocks: List[str] = []

    # 5개 섹션 병렬 조회 (AADS-CRITICAL-FIX #30)
    notes, prefs, tools, dirs, disc = await asyncio.gather(
        _build_session_notes(session_id, project_id),
        _build_preferences(),
        _build_tool_strategy(project_id),
        _build_active_directives(project_id),
        _build_discoveries(project_id),
    )

    if notes:
        blocks.append(f"<memory_session_notes>\n## 이전 대화 요약\n{notes}\n</memory_session_notes>")
    if prefs:
        blocks.append(f"<memory_preferences>\n## CEO 운영 원칙 및 선호\n{prefs}\n</memory_preferences>")
    if tools:
        blocks.append(f"<memory_tool_strategy>\n## 도구 사용 전략\n{tools}\n</memory_tool_strategy>")
    if dirs:
        blocks.append(f"<memory_directives>\n## 활성 지시사항\n{dirs}\n</memory_directives>")
    if disc:
        blocks.append(f"<memory_discoveries>\n## 이전 작업에서 발견한 사항\n{disc}\n</memory_discoveries>")

    result = "\n\n".join(blocks) if blocks else ""

    # 총 예산 초과 시 뒤에서부터 제거
    if len(result) > _TOTAL_CHAR_LIMIT:
        result = _truncate(result, _TOTAL_CHAR_LIMIT)

    return result


# ── 메모리 쓰기 인터페이스 ────────────────────────────────────────────────────

async def save_observation(
    category: str,
    key: str,
    content: str,
    source: str = "chat",
    confidence: float = 0.5,
    project: Optional[str] = None,
) -> bool:
    """
    채팅 또는 에이전트가 메모리에 기록.
    #18: GREATEST로 confidence 보호 (동시 upsert race condition 방지)
    #23: project 대문자 정규화
    #25: key 특수문자 정제, 200자 제한
    """
    key = _sanitize_key(key)
    project = _normalize_project(project)
    try:
        async with _get_pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_observations (category, key, value, confidence, project, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (category, key, COALESCE(project, ''))
                DO UPDATE SET
                    value = EXCLUDED.value,
                    confidence = GREATEST(EXCLUDED.confidence, ai_observations.confidence),
                    updated_at = NOW()
                """,
                category, key, content, confidence, project,
            )
            logger.info("memory_recall_save_observation",
                        category=category, key=key[:50], project=project, source=source)
            return True
    except Exception as e:
        logger.warning("memory_recall_save_observation_failed", error=str(e), category=category, key=key[:50])
        # fallback: memory_manager 경로
        try:
            from app.services.memory_manager import get_memory_manager
            mgr = get_memory_manager()
            await mgr.observe(
                category=category,
                key=key,
                value=content,
                confidence=confidence,
            )
            logger.info("memory_recall_save_observation_fallback", category=category, key=key[:50])
            return True
        except Exception as e2:
            # #13: 최종 폴백 실패 로깅
            logger.warning("memory_recall_save_observation_all_fallbacks_failed",
                           error=str(e2), category=category, key=key[:50])
            return False
