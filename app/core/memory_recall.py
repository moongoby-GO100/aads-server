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

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 토큰 예산 (한국어 1토큰 ≈ 1.5자 → 자수 상한)
_BUDGET = {
    "session_notes": 750,       # ~500 토큰
    "preferences": 450,         # ~300 토큰
    "tool_strategy": 600,       # ~400 토큰
    "directives": 600,          # ~400 토큰
    "discoveries": 600,         # ~400 토큰
}
_TOTAL_CHAR_LIMIT = 3000  # ~2000 토큰


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else url


async def _get_conn():
    import asyncpg
    return await asyncpg.connect(_db_url(), timeout=10)


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
        conn = await _get_conn()
        try:
            if project_id:
                # 해당 프로젝트가 projects_discussed에 포함된 노트 우선, 나머지 보조
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
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"memory_recall session_notes 조회 실패: {e}")
        return ""


async def _build_preferences() -> str:
    """섹션 2: CEO 운영 원칙/선호 — 전역 공통 (프로젝트 필터 없음)."""
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                """
                SELECT key, value FROM ai_observations
                WHERE category IN ('ceo_preference', 'decision')
                  AND confidence >= 0.3
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 15
                """,
            )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["preferences"])
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"memory_recall preferences 조회 실패: {e}")
        return ""


async def _build_tool_strategy(project_id: Optional[str] = None) -> str:
    """섹션 3: 도구 사용 전략 — 해당 프로젝트 + 공통(project IS NULL)."""
    try:
        conn = await _get_conn()
        try:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('project_pattern', 'tool_strategy')
                      AND confidence >= 0.3
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        confidence DESC, updated_at DESC
                    LIMIT 10
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('project_pattern', 'tool_strategy')
                      AND confidence >= 0.3
                    ORDER BY confidence DESC, updated_at DESC
                    LIMIT 10
                    """,
                )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["tool_strategy"])
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"memory_recall tool_strategy 조회 실패: {e}")
        return ""


async def _build_active_directives(project_id: Optional[str] = None) -> str:
    """섹션 4: 활성 Directive (directive_lifecycle pending/running)."""
    try:
        conn = await _get_conn()
        try:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT task_id, title, status, priority
                    FROM directive_lifecycle
                    WHERE status IN ('pending', 'running', 'queued')
                      AND project = $1
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
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"memory_recall active_directives 조회 실패: {e}")
        return ""


async def _build_discoveries(project_id: Optional[str] = None) -> str:
    """섹션 5: 이전 작업 발견 사항 — 해당 프로젝트 + 공통(project IS NULL)."""
    try:
        conn = await _get_conn()
        try:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('learning', 'recurring_issue', 'discovery')
                      AND confidence >= 0.3
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        updated_at DESC, confidence DESC
                    LIMIT 10
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category IN ('learning', 'recurring_issue', 'discovery')
                      AND confidence >= 0.3
                    ORDER BY updated_at DESC, confidence DESC
                    LIMIT 10
                    """,
                )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["discoveries"])
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"memory_recall discoveries 조회 실패: {e}")
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
    blocks: List[str] = []

    # 1. 이전 대화 요약 (프로젝트 우선)
    notes = await _build_session_notes(session_id, project_id)
    if notes:
        blocks.append(f"<memory_session_notes>\n## 이전 대화 요약\n{notes}\n</memory_session_notes>")

    # 2. CEO 운영 원칙/선호 (전역 공통)
    prefs = await _build_preferences()
    if prefs:
        blocks.append(f"<memory_preferences>\n## CEO 운영 원칙 및 선호\n{prefs}\n</memory_preferences>")

    # 3. 도구 사용 전략 (프로젝트+공통)
    tools = await _build_tool_strategy(project_id)
    if tools:
        blocks.append(f"<memory_tool_strategy>\n## 도구 사용 전략\n{tools}\n</memory_tool_strategy>")

    # 4. 활성 Directive (프로젝트 필터)
    dirs = await _build_active_directives(project_id)
    if dirs:
        blocks.append(f"<memory_directives>\n## 활성 지시사항\n{dirs}\n</memory_directives>")

    # 5. 발견 사항 (프로젝트+공통)
    disc = await _build_discoveries(project_id)
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
    category: 'ceo_preference', 'project_pattern', 'recurring_issue',
              'decision', 'learning', 'discovery', 'tool_strategy'
    project: 'AADS', 'KIS', 'GO100', 'SF', 'NTV2', 'NAS' 또는 None(전역)
    동일 (category, key, project)가 있으면 confidence 증가 + value 업데이트.
    """
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO ai_observations (category, key, value, confidence, project, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (category, key, COALESCE(project, ''))
                DO UPDATE SET
                    value = EXCLUDED.value,
                    confidence = LEAST(ai_observations.confidence + 0.1, 1.0),
                    updated_at = NOW()
                """,
                category, key, content, confidence, project,
            )
            logger.info(f"memory_recall save_observation: [{category}] {key} project={project} (source={source})")
            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.warning(f"memory_recall save_observation 실패: {e}")
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
            logger.info(f"memory_recall save_observation fallback: [{category}] {key} (source={source})")
            return True
        except Exception as e2:
            logger.warning(f"memory_recall save_observation fallback도 실패: {e2}")
            return False
