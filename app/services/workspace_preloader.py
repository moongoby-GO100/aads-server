"""
F6: Workspace Preloading — 매 턴 현재 프로젝트의 최근 facts + 활성 이슈 + 마지막 세션 요약 자동 주입.
Layer 2.5로 주입, ~1000 tokens.
"""
from __future__ import annotations

import os
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_PRELOAD_ENABLED = os.getenv("WORKSPACE_PRELOAD_ENABLED", "true").lower() == "true"
_PRELOAD_TOKEN_BUDGET = int(os.getenv("WORKSPACE_PRELOAD_TOKENS", "1000"))


async def build_workspace_preload(
    project: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """현재 프로젝트의 최근 facts + 마지막 세션 요약을 Layer 2.5로 주입.

    Returns:
        XML-wrapped preload context string.
    """
    if not _PRELOAD_ENABLED or not project:
        return ""

    try:
        import asyncio
        from app.services.ceo_pattern_tracker import get_predicted_interests

        recent_facts, last_summary, predicted_interests, error_warnings = await asyncio.gather(
            _get_recent_facts(project),
            _get_last_session_summary(project, session_id),
            get_predicted_interests(),
            _get_error_pattern_warnings(project),  # P2: 에러 패턴 자동 경고
            return_exceptions=True,
        )

        parts = []
        from app.core.token_utils import estimate_tokens
        total = 0

        # P2: 에러 패턴 경고 (최우선 주입)
        if isinstance(error_warnings, str) and error_warnings:
            t = estimate_tokens(error_warnings)
            if total + t <= _PRELOAD_TOKEN_BUDGET:
                parts.append(error_warnings)
                total += t

        # 최근 사실 (최대 10건)
        if isinstance(recent_facts, str) and recent_facts:
            t = estimate_tokens(recent_facts)
            if total + t <= _PRELOAD_TOKEN_BUDGET:
                parts.append(recent_facts)
                total += t

        # 마지막 세션 요약
        if isinstance(last_summary, str) and last_summary:
            t = estimate_tokens(last_summary)
            if total + t <= _PRELOAD_TOKEN_BUDGET:
                parts.append(last_summary)
                total += t

        # A3: CEO 패턴 기반 예상 관심사항
        if isinstance(predicted_interests, str) and predicted_interests:
            interest_block = f"예상 관심사항:\n{predicted_interests}"
            t = estimate_tokens(interest_block)
            if total + t <= _PRELOAD_TOKEN_BUDGET:
                parts.append(interest_block)
                total += t

        if not parts:
            return ""

        content = "\n".join(parts)
        block = (
            f"<workspace_preload>\n"
            f"## 프로젝트 컨텍스트 ({project})\n"
            f"{content}\n"
            f"</workspace_preload>"
        )

        logger.info("workspace_preload_injected", project=project, tokens=total)
        return block

    except Exception as e:
        logger.debug("workspace_preload_error", error=str(e))
        return ""


async def _get_error_pattern_warnings(project: str) -> str:
    """P2: 프로젝트의 최근 error_pattern 상위 3건을 경고로 주입."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject, detail, referenced_count, confidence
                FROM memory_facts
                WHERE project = $1
                  AND category = 'error_pattern'
                  AND superseded_by IS NULL
                  AND confidence > 0.5
                ORDER BY referenced_count DESC, updated_at DESC
                LIMIT 3
                """,
                project.upper(),
            )
            if not rows:
                return ""

            lines = []
            for r in rows:
                ref = r["referenced_count"] or 0
                lines.append(f"  ⚠️ [{ref}회 발생] {r['subject']}")
            return "## 반복 에러 패턴 경고 (유사 작업 시 주의):\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("workspace_preload_error_pattern_error", error=str(e))
        return ""


async def _get_recent_facts(project: str) -> str:
    """프로젝트의 최근 memory_facts 10건. P4: discovery confidence<0.5 제외."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT category, subject, detail, created_at, referenced_count, confidence
                FROM memory_facts
                WHERE project = $1
                  AND superseded_by IS NULL
                  AND confidence > 0.4
                  AND NOT (category = 'discovery' AND confidence < 0.5)
                ORDER BY (
                    confidence * 0.4
                    + LEAST(1.0, referenced_count::float / 20.0) * 0.4
                    + (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0)) * 0.2
                ) DESC
                LIMIT 10
                """,
                project.upper(),
            )
            if not rows:
                return ""

            lines = []
            for r in rows:
                ts = r["created_at"].strftime("%m/%d") if r["created_at"] else ""
                ref = r["referenced_count"] if "referenced_count" in r.keys() else 0
                lines.append(f"  - [{ts}][{r['category']}] {r['subject']}" + (f" (참조:{ref}회)" if ref > 5 else ""))
            return "최근 사실:\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("workspace_preload_facts_error", error=str(e))
        return ""


async def _get_last_session_summary(project: str, current_session_id: Optional[str]) -> str:
    """프로젝트의 마지막 세션 요약."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT summary, key_decisions, created_at
                FROM session_notes
                WHERE $1 = ANY(projects_discussed)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                project.upper(),
            )
            if not row:
                return ""

            ts = row["created_at"].strftime("%m/%d %H:%M") if row["created_at"] else ""
            summary = row["summary"] or ""
            decisions = list(row.get("key_decisions") or [])
            text = f"마지막 세션 요약 ({ts}): {summary[:200]}"
            if decisions:
                text += f"\n  결정사항: {', '.join(decisions[:3])}"
            return text
    except Exception as e:
        logger.debug("workspace_preload_summary_error", error=str(e))
        return ""
