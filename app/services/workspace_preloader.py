"""
F6: Workspace Preloading — 매 턴 현재 프로젝트의 최근 facts + 활성 이슈 + 마지막 세션 요약 자동 주입.
Layer 2.5로 주입, ~1000 tokens.

오케스트레이터 워크스페이스(CEO 통합지시 등)는 자기 프로젝트 facts만이 아니라
핵심 프로젝트(AADS/KIS/GO100/SF/NTV2/NAS) 전체의 최신 변경을 프로젝트별로 고르게 주입한다.
일반 프로젝트 워크스페이스는 기존처럼 해당 프로젝트만 격리 주입한다(세션 간 맥락 섞임 방지).
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

_PRELOAD_ENABLED = os.getenv("WORKSPACE_PRELOAD_ENABLED", "true").lower() == "true"
_PRELOAD_TOKEN_BUDGET = int(os.getenv("WORKSPACE_PRELOAD_TOKENS", "1000"))
# 오케스트레이터 워크스페이스는 전 프로젝트를 담아야 하므로 예산을 별도로 둔다.
_PRELOAD_TOKEN_BUDGET_ORCHESTRATOR = int(os.getenv("WORKSPACE_PRELOAD_TOKENS_ORCHESTRATOR", "1800"))

# 전 프로젝트 통합 관리 워크스페이스 키 (쉼표 구분, 기본 CEO)
_ORCHESTRATOR_WORKSPACES = {
    k.strip().upper()
    for k in os.getenv("WORKSPACE_ORCHESTRATOR_KEYS", "CEO").split(",")
    if k.strip()
}
# 오케스트레이터가 항상 인지해야 하는 핵심 프로젝트
_CORE_PROJECTS = [
    p.strip().upper()
    for p in os.getenv("WORKSPACE_ORCHESTRATOR_PROJECTS", "AADS,KIS,GO100,SF,NTV2,NAS").split(",")
    if p.strip()
]
# 오케스트레이터 모드에서 프로젝트당 최대 주입 건수 (특정 프로젝트 편중 방지)
_PER_PROJECT_STRATEGIC = 2
_PER_PROJECT_FACTS = 2


def is_orchestrator_workspace(project: Optional[str]) -> bool:
    return bool(project) and project.strip().upper() in _ORCHESTRATOR_WORKSPACES


def _scope_projects(project: str) -> list[str]:
    """주입 대상 프로젝트 목록. 오케스트레이터면 자기 자신 + 핵심 프로젝트 전체."""
    key = project.strip().upper()
    if key in _ORCHESTRATOR_WORKSPACES:
        seen: list[str] = [key]
        for p in _CORE_PROJECTS:
            if p not in seen:
                seen.append(p)
        return seen
    return [key]


def _fmt_line(r, orchestrator: bool, label: str) -> str:
    ts = r["created_at"].strftime("%m/%d") if r["created_at"] else ""
    proj = f"[{r['project']}]" if orchestrator and r["project"] else ""
    return f"  - [{ts}]{proj}[{label}] {r['subject']}"


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

        orchestrator = is_orchestrator_workspace(project)
        scope = _scope_projects(project)
        budget = _PRELOAD_TOKEN_BUDGET_ORCHESTRATOR if orchestrator else _PRELOAD_TOKEN_BUDGET

        strategic_changes, recent_facts, last_summary, predicted_interests, error_warnings = await asyncio.gather(
            _get_strategic_project_changes(scope, orchestrator),
            _get_recent_facts(scope, orchestrator),
            _get_last_session_summary(scope, session_id),
            get_predicted_interests(),
            _get_error_pattern_warnings(scope, orchestrator),  # P2: 에러 패턴 자동 경고
            return_exceptions=True,
        )

        parts = []
        from app.core.token_utils import estimate_tokens
        total = 0

        # P2: 에러 패턴 경고 (최우선 주입)
        if isinstance(error_warnings, str) and error_warnings:
            t = estimate_tokens(error_warnings)
            if total + t <= budget:
                parts.append(error_warnings)
                total += t

        # 중요 아키텍처/기능/API/데이터 모델 변경 (세션 자동 인지 핵심)
        if isinstance(strategic_changes, str) and strategic_changes:
            t = estimate_tokens(strategic_changes)
            if total + t <= budget:
                parts.append(strategic_changes)
                total += t

        # 최근 사실 (최대 10건)
        if isinstance(recent_facts, str) and recent_facts:
            t = estimate_tokens(recent_facts)
            if total + t <= budget:
                parts.append(recent_facts)
                total += t

        # 마지막 세션 요약
        if isinstance(last_summary, str) and last_summary:
            t = estimate_tokens(last_summary)
            if total + t <= budget:
                parts.append(last_summary)
                total += t

        # A3: CEO 패턴 기반 예상 관심사항
        if isinstance(predicted_interests, str) and predicted_interests:
            interest_block = f"예상 관심사항:\n{predicted_interests}"
            t = estimate_tokens(interest_block)
            if total + t <= budget:
                parts.append(interest_block)
                total += t

        if not parts:
            return ""

        content = "\n".join(parts)
        header = (
            f"## 프로젝트 컨텍스트 ({project} · 통합 감시: {','.join(scope[1:])})"
            if orchestrator and len(scope) > 1
            else f"## 프로젝트 컨텍스트 ({project})"
        )
        block = (
            f"<workspace_preload>\n"
            f"{header}\n"
            f"{content}\n"
            f"</workspace_preload>"
        )

        logger.info(
            "workspace_preload_injected",
            project=project, tokens=total, orchestrator=orchestrator, scope=scope,
        )
        return block

    except Exception as e:
        logger.debug("workspace_preload_error", error=str(e))
        return ""


async def _get_strategic_project_changes(scope: list[str], orchestrator: bool) -> str:
    """세션이 반드시 알아야 하는 프로젝트 중요 변경을 우선 주입.

    오케스트레이터 모드: 프로젝트별 최신 _PER_PROJECT_STRATEGIC건씩 고르게 주입.
    """
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        categories = [
            "architecture_decision",
            "feature_change",
            "api_contract",
            "data_model_change",
        ]
        async with pool.acquire() as conn:
            if orchestrator:
                rows = await conn.fetch(
                    """
                    SELECT project, category, subject, detail, created_at, referenced_count, confidence
                    FROM (
                        SELECT project, category, subject, detail, created_at, referenced_count, confidence,
                               ROW_NUMBER() OVER (
                                   PARTITION BY project
                                   ORDER BY updated_at DESC, created_at DESC, confidence DESC
                               ) AS rn
                        FROM memory_facts
                        WHERE project = ANY($1::text[])
                          AND category = ANY($2::text[])
                          AND superseded_by IS NULL
                          AND confidence >= 0.7
                    ) ranked
                    WHERE rn <= $3
                    ORDER BY created_at DESC
                    LIMIT 12
                    """,
                    scope, categories, _PER_PROJECT_STRATEGIC,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT project, category, subject, detail, created_at, referenced_count, confidence
                    FROM memory_facts
                    WHERE project = ANY($1::text[])
                      AND category = ANY($2::text[])
                      AND superseded_by IS NULL
                      AND confidence >= 0.7
                    ORDER BY confidence DESC, updated_at DESC, created_at DESC
                    LIMIT 6
                    """,
                    scope, categories,
                )
            if not rows:
                return ""

            labels = {
                "architecture_decision": "구조",
                "feature_change": "기능",
                "api_contract": "API",
                "data_model_change": "DB",
            }
            lines = [_fmt_line(r, orchestrator, labels.get(r["category"], r["category"])) for r in rows]
            title = "## 최근 중요 변경 자동 인지 (전 프로젝트):" if orchestrator else "## 최근 중요 변경 자동 인지:"
            return title + "\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("workspace_preload_strategic_changes_error", error=str(e))
        return ""


async def _get_error_pattern_warnings(scope: list[str], orchestrator: bool) -> str:
    """P2: 프로젝트의 최근 error_pattern 상위 3건(오케스트레이터는 프로젝트당 1건, 최대 6건)을 경고로 주입."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            if orchestrator:
                rows = await conn.fetch(
                    """
                    SELECT project, subject, detail, referenced_count, confidence
                    FROM (
                        SELECT project, subject, detail, referenced_count, confidence,
                               ROW_NUMBER() OVER (
                                   PARTITION BY project
                                   ORDER BY referenced_count DESC, updated_at DESC
                               ) AS rn
                        FROM memory_facts
                        WHERE project = ANY($1::text[])
                          AND category = 'error_pattern'
                          AND superseded_by IS NULL
                          AND confidence > 0.5
                    ) ranked
                    WHERE rn <= 1
                    ORDER BY referenced_count DESC
                    LIMIT 6
                    """,
                    scope,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT project, subject, detail, referenced_count, confidence
                    FROM memory_facts
                    WHERE project = ANY($1::text[])
                      AND category = 'error_pattern'
                      AND superseded_by IS NULL
                      AND confidence > 0.5
                    ORDER BY referenced_count DESC, updated_at DESC
                    LIMIT 3
                    """,
                    scope,
                )
            if not rows:
                return ""

            lines = []
            for r in rows:
                ref = r["referenced_count"] or 0
                proj = f"[{r['project']}] " if orchestrator and r["project"] else ""
                lines.append(f"  ⚠️ {proj}[{ref}회 발생] {r['subject']}")
            return "## 반복 에러 패턴 경고 (유사 작업 시 주의):\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("workspace_preload_error_pattern_error", error=str(e))
        return ""


async def _get_recent_facts(scope: list[str], orchestrator: bool) -> str:
    """프로젝트의 최근 memory_facts 10건. P4: discovery confidence<0.5 제외.

    오케스트레이터 모드: 프로젝트별 상위 _PER_PROJECT_FACTS건씩 고르게 주입(최대 12건).
    """
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        score_sql = """(
                    confidence * 0.4
                    + LEAST(1.0, referenced_count::float / 20.0) * 0.4
                    + (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0)) * 0.2
                )"""
        async with pool.acquire() as conn:
            if orchestrator:
                rows = await conn.fetch(
                    f"""
                    SELECT project, category, subject, detail, created_at, referenced_count, confidence
                    FROM (
                        SELECT project, category, subject, detail, created_at, referenced_count, confidence,
                               ROW_NUMBER() OVER (PARTITION BY project ORDER BY {score_sql} DESC) AS rn
                        FROM memory_facts
                        WHERE project = ANY($1::text[])
                          AND superseded_by IS NULL
                          AND confidence > 0.4
                          AND NOT (category = 'discovery' AND confidence < 0.5)
                    ) ranked
                    WHERE rn <= $2
                    ORDER BY created_at DESC
                    LIMIT 12
                    """,
                    scope, _PER_PROJECT_FACTS,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT project, category, subject, detail, created_at, referenced_count, confidence
                    FROM memory_facts
                    WHERE project = ANY($1::text[])
                      AND superseded_by IS NULL
                      AND confidence > 0.4
                      AND NOT (category = 'discovery' AND confidence < 0.5)
                    ORDER BY {score_sql} DESC
                    LIMIT 10
                    """,
                    scope,
                )
            if not rows:
                return ""

            lines = []
            for r in rows:
                ref = r["referenced_count"] if "referenced_count" in r.keys() else 0
                line = _fmt_line(r, orchestrator, r["category"])
                if ref and ref > 5:
                    line += f" (참조:{ref}회)"
                lines.append(line)
            title = "최근 사실 (전 프로젝트):" if orchestrator else "최근 사실:"
            return title + "\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("workspace_preload_facts_error", error=str(e))
        return ""


async def _get_last_session_summary(scope: list[str], current_session_id: Optional[str]) -> str:
    """프로젝트의 마지막 세션 요약 (오케스트레이터는 범위 내 어느 프로젝트든 최신 1건)."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()

        async with pool.acquire() as conn:
            scope_tenant_id = None
            scope_user_id = None
            current_uuid = None
            if current_session_id:
                try:
                    current_uuid = uuid.UUID(str(current_session_id))
                except (TypeError, ValueError):
                    current_uuid = None
                if current_uuid is not None:
                    scope_row = await conn.fetchrow(
                        "SELECT tenant_id, user_id FROM chat_sessions WHERE id = $1",
                        current_uuid,
                    )
                    if scope_row:
                        scope_tenant_id = str(scope_row["tenant_id"]) if scope_row["tenant_id"] else None
                        scope_user_id = str(scope_row["user_id"]) if scope_row["user_id"] else None

            if scope_tenant_id or scope_user_id or current_uuid is not None:
                row = await conn.fetchrow(
                    """
                    SELECT sn.summary, sn.key_decisions, sn.created_at
                    FROM session_notes sn
                    JOIN chat_sessions cs ON sn.session_id = cs.id
                    WHERE sn.projects_discussed::text[] && $1::text[]
                      AND ($2::uuid IS NULL OR cs.tenant_id = $2::uuid)
                      AND ($3::text IS NULL OR cs.user_id = $3::text)
                      AND ($4::uuid IS NULL OR cs.id <> $4::uuid)
                    ORDER BY sn.created_at DESC
                    LIMIT 1
                    """,
                    scope,
                    scope_tenant_id,
                    scope_user_id,
                    current_uuid,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT summary, key_decisions, created_at
                    FROM session_notes
                    WHERE projects_discussed::text[] && $1::text[]
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    scope,
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
