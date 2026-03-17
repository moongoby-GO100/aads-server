"""
AADS 메모리 자동 주입 시스템 — 공유 메모리 리콜 모듈
채팅(chat_service)과 에이전트(autonomous_executor, agent_sdk_service) 공통 사용.

7개 섹션 조립 (프로젝트별 필터 적용):
1. 이전 대화 요약 (session_notes, ~500 토큰) — 해당 프로젝트 우선
   + correction_directive 상단 강제 주입 (Reflexion B1 반성 → 행동 변화)
2. CEO 운영 원칙/선호 (ai_observations category='ceo_preference', ~300 토큰) — 공통(전역)
3. 도구 사용 전략 (ai_observations category='tool_strategy'|'project_pattern', ~400 토큰) — 프로젝트+공통
4. 활성 Directive (directive_lifecycle status IN pending/running, ~400 토큰) — 프로젝트 필터
5. 이전 작업 발견 사항 (ai_observations category='discovery'|'learning', ~400 토큰) — 프로젝트+공통
6. AI 학습 메모리 (ai_meta_memory, ~300 토큰) — CEO 선호/프로젝트 패턴
7. 반성 지시사항 (ai_meta_memory correction_directive, ~200 토큰) — 최근 3건

총 토큰 예산: ~2,300 토큰 이내 (한국어 기준 1토큰 ≈ 1.5자)
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
    "learned_memory": 450,      # ~300 토큰 (ai_meta_memory)
    "correction_directives": 300,  # ~200 토큰 (Reflexion B1 반성 지시, 최근 3건)
}
_TOTAL_CHAR_LIMIT = 4000  # ~2700 토큰 (correction_directive 이중 배치 + 세션노트 통합분 반영)

# #14: 카테고리별 confidence 임계값 (환경변수 오버라이드 가능)
_CONFIDENCE = {
    "ceo_preference": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "decision": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "tool_strategy": float(os.getenv("CONFIDENCE_TOOL_STRATEGY", "0.3")),
    "project_pattern": float(os.getenv("CONFIDENCE_TOOL_STRATEGY", "0.3")),
    "discovery": float(os.getenv("CONFIDENCE_DISCOVERY", "0.55")),  # P4: 0.4→0.55 (불확실 발견 필터)
    "learning": float(os.getenv("CONFIDENCE_DISCOVERY", "0.55")),
    "recurring_issue": float(os.getenv("CONFIDENCE_DISCOVERY", "0.55")),
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


async def _build_correction_directives() -> str:
    """Reflexion(B1) correction_directive → 다음 턴 시스템 프롬프트 강제 주입.
    ai_meta_memory에서 최근 3건만 조회하여 토큰 절약.
    P2-FIX: COALESCE(updated_at, created_at)로 NULL 안전 정렬, 즉시 반영 보장."""
    try:
        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT key, value FROM ai_meta_memory
                WHERE category = 'correction_directive'
                ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
                LIMIT 3
                """,
            )
            if not rows:
                return ""
            import json as _json
            lines = []
            for r in rows:
                val = r["value"]
                if isinstance(val, str):
                    try:
                        val = _json.loads(val)
                    except Exception:
                        pass
                if isinstance(val, dict):
                    val_str = val.get("directive") or val.get("summary") or val.get("description") or _json.dumps(val, ensure_ascii=False)
                else:
                    val_str = str(val)
                lines.append(f"- [반성지시] {r['key']}: {val_str[:150]}")
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["correction_directives"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="correction_directives", error=str(e))
        return ""


async def _build_learned_memory(project_id: Optional[str] = None) -> str:
    """섹션 6: learn_pattern으로 저장된 AI 학습 메모리 (ai_meta_memory).
    CEO 선호 + 프로젝트 패턴 + 결정 이력을 프로젝트 무관하게 전체 주입.
    """
    try:
        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT category, key, value FROM ai_meta_memory
                WHERE category IN ('ceo_preference', 'project_pattern', 'known_issue', 'decision_history', 'prompt_optimization')
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 15
                """,
            )
            if not rows:
                return ""
            import json as _json
            lines = []
            for r in rows:
                val = r["value"]
                if isinstance(val, str):
                    try:
                        val = _json.loads(val)
                    except Exception:
                        pass
                if isinstance(val, dict):
                    val_str = val.get("summary") or val.get("description") or _json.dumps(val, ensure_ascii=False)
                else:
                    val_str = str(val)
                lines.append(f"- [{r['category']}] {r['key']}: {val_str[:100]}")
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["learned_memory"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="learned_memory", error=str(e))
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

    # 7개 섹션 병렬 조회 (AADS-CRITICAL-FIX #30 + 섹션6 ai_meta_memory + 섹션7 correction_directive)
    notes, prefs, tools, dirs, disc, learned, corrections = await asyncio.gather(
        _build_session_notes(session_id, project_id),
        _build_preferences(),
        _build_tool_strategy(project_id),
        _build_active_directives(project_id),
        _build_discoveries(project_id),
        _build_learned_memory(project_id),
        _build_correction_directives(),
    )

    # P2-FIX: correction_directive → 세션 노트(Layer2) 상단 강제 주입
    # 반성 지시사항이 세션 맥락과 함께 전달되어 행동 변화 유도력 향상
    # 별도 블록(최우선) + 세션 노트 내부 이중 배치로 절대 누락 방지
    if corrections:
        blocks.append(f"<memory_correction_directives>\n## ⚠️ 반성 지시사항 (이번 턴에서 즉시 반영할 것)\n{corrections}\n</memory_correction_directives>")
        logger.info("correction_directive_injected", count=corrections.count("[반성지시]"), chars=len(corrections))
    # 세션 노트에 correction_directive 상단 강제 주입 (Layer2 통합) — 우선순위 최상위
    _correction_header = f"⚠️ [즉시반영 필수] 이전 턴 반성 결과:\n{corrections}" if corrections else ""
    if _correction_header and notes:
        notes = f"{_correction_header}\n---\n{notes}"
    elif _correction_header and not notes:
        notes = _correction_header
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
    if learned:
        blocks.append(f"<memory_learned>\n## AI 학습 메모리 (learn_pattern)\n{learned}\n</memory_learned>")

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


# ── 진화 프로세스 수치 조회 ────────────────────────────────────────────────────

async def get_evolution_stats(db) -> dict:
    """진화 프로세스 실시간 수치 조회 — LAYER4 시스템 프롬프트 주입용.
    모든 워크스페이스(AADS/KIS/GO100/SF/NTV2/NAS) 공통 호출.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM memory_facts) AS fact_count,
                (SELECT COUNT(*) FROM ai_observations) AS obs_count,
                (SELECT COUNT(*) FROM memory_facts WHERE category = 'error_pattern') AS error_count,
                (SELECT COUNT(*) FROM chat_messages WHERE quality_score IS NOT NULL AND created_at > NOW() - INTERVAL '7 days') AS quality_count,
                (SELECT ROUND(AVG(quality_score)::numeric, 1) FROM chat_messages WHERE quality_score IS NOT NULL AND created_at > NOW() - INTERVAL '7 days') AS avg_quality
            """
        )
        avg_q = row["avg_quality"] if row and row["avg_quality"] is not None else "?"
        q_cnt = row["quality_count"] if row and row["quality_count"] is not None else "?"
        return {
            "fact_count": row["fact_count"] if row else "?",
            "obs_count": row["obs_count"] if row else "?",
            "avg_quality": f"{float(avg_q)*100:.0f}%" if avg_q != "?" else "?",
            "quality_count": q_cnt,
            "error_pattern_count": row["error_count"] if row else "?",
        }
    except Exception as e:
        logger.warning("get_evolution_stats_failed", error=str(e))
        return {
            "fact_count": "?",
            "obs_count": "?",
            "avg_quality": "?",
            "quality_count": "?",
            "error_pattern_count": "?",
        }
