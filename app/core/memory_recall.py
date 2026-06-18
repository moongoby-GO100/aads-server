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
7. 반성 지시사항 (ai_meta_memory correction_directive, ~200 토큰) — 빈도 상위 5건

총 토큰 예산: ~2,300 토큰 이내 (한국어 기준 1토큰 ≈ 1.5자)
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
import structlog
from typing import Any, List, Optional

logger = structlog.get_logger(__name__)

# 토큰 예산 (한국어 1토큰 ≈ 1.5자 → 자수 상한)
_BUDGET = {
    "session_notes": 750,           # ~500 토큰
    "preferences": 450,             # ~300 토큰
    "tool_strategy": 600,           # ~400 토큰
    "directives": 600,              # ~400 토큰
    "discoveries": 600,             # ~400 토큰
    "learned_memory": 450,          # ~300 토큰 (ai_meta_memory)
    "correction_directives": 300,   # ~200 토큰 (Reflexion B1 반성 지시, 빈도 상위 5건)
    "experience_lessons": 450,      # ~300 토큰 (AADS-P1-1 실시간 교훈, 최근 5건)
    "visual_memories": 450,         # ~300 토큰 (이미지 분석 메모리, 최근 3건)
    "experience_memory": 450,       # ~300 토큰 (구조화 경험 메모리, RIF 기반 상위 3건)
    "procedural_memory": 450,       # ~300 토큰 (절차 메모리, 성공률 상위 3건)
    "knowledge_graph": 450,          # ~300 토큰 (P2 지식그래프, 핵심 엔티티+관계)
}
_TOTAL_CHAR_LIMIT = 4500  # ~3000 토큰 (P2 지식그래프 섹션 추가분 반영)

# #14: 카테고리별 confidence 임계값 (환경변수 오버라이드 가능)
_CONFIDENCE = {
    "ceo_preference": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "decision": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "ceo_correction": float(os.getenv("CONFIDENCE_CEO_PREF", "0.2")),
    "tool_strategy": float(os.getenv("CONFIDENCE_TOOL_STRATEGY", "0.3")),
    "project_pattern": float(os.getenv("CONFIDENCE_TOOL_STRATEGY", "0.3")),
    "discovery": float(os.getenv("CONFIDENCE_DISCOVERY", "0.40")),  # P5: 0.55→0.40 (실평균 0.41 반영, 대부분 필터링 문제 해결)
    "learning": float(os.getenv("CONFIDENCE_DISCOVERY", "0.40")),
    "recurring_issue": float(os.getenv("CONFIDENCE_DISCOVERY", "0.40")),
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


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        import json as _json
        try:
            parsed = _json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {"directive": value}
        except Exception:
            return {"directive": value}
    return {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact_inline(text: Any, limit: int) -> str:
    compacted = " ".join(str(text or "").split())
    return compacted[:limit].rstrip()


# ── 섹션 빌더 ────────────────────────────────────────────────────────────────

async def _build_session_notes(
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """섹션 1: 이전 대화 요약 (session_notes 최근 3개, 해당 프로젝트 우선)."""
    try:
        async with _get_pool().acquire() as conn:
            scope_tenant_id = tenant_id
            scope_user_id = user_id
            session_uuid: Optional[uuid.UUID] = None
            if session_id:
                try:
                    session_uuid = uuid.UUID(str(session_id))
                except (TypeError, ValueError):
                    session_uuid = None
            if session_uuid is not None and (scope_tenant_id is None or scope_user_id is None):
                scope_row = await conn.fetchrow(
                    "SELECT tenant_id, user_id FROM chat_sessions WHERE id = $1",
                    session_uuid,
                )
                if scope_row:
                    if scope_tenant_id is None and scope_row["tenant_id"]:
                        scope_tenant_id = str(scope_row["tenant_id"])
                    if scope_user_id is None and scope_row["user_id"]:
                        scope_user_id = str(scope_row["user_id"])

            if scope_tenant_id:
                params: list[Any] = [scope_tenant_id]
                where_clauses = ["cs.tenant_id = $1::uuid"]
                if scope_user_id:
                    params.append(scope_user_id)
                    where_clauses.append(f"cs.user_id = ${len(params)}::text")
                if project_id:
                    params.append(project_id)
                    project_param = len(params)
                    order_clause = f"ORDER BY CASE WHEN ${project_param} = ANY(sn.projects_discussed) THEN 0 ELSE 1 END, sn.created_at DESC"
                else:
                    order_clause = "ORDER BY sn.created_at DESC"
                rows = await conn.fetch(
                    f"""
                    SELECT sn.summary, sn.key_decisions, sn.created_at, sn.projects_discussed
                    FROM session_notes sn
                    JOIN chat_sessions cs ON sn.session_id = cs.id
                    WHERE {' AND '.join(where_clauses)}
                    {order_clause}
                    LIMIT 3
                    """,
                    *params,
                )
            elif project_id:
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


async def _build_preferences() -> tuple[str, list[int]]:
    """섹션 2: CEO 운영 원칙/선호 — 전역 공통 (프로젝트 필터 없음)."""
    try:
        _conf = _CONFIDENCE.get("ceo_preference", 0.2)
        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, key, value FROM ai_observations
                WHERE category IN ('ceo_preference', 'decision', 'ceo_correction', 'compaction_directive', 'ceo_directive')
                  AND confidence >= $1
                ORDER BY
                    CASE WHEN category IN ('compaction_directive','ceo_directive') THEN 0 ELSE 1 END,
                    (confidence * EXP(-0.1 * EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at))) / 86400)) DESC
                LIMIT 20
                """,
                _conf,
            )
            if not rows:
                return "", []
            used_ids = [r["id"] for r in rows]
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["preferences"]), used_ids
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="preferences", error=str(e))
        return "", []


async def _build_tool_strategy(project_id: Optional[str] = None) -> tuple[str, list[int]]:
    """섹션 3: 도구 사용 전략 — 해당 프로젝트 + 공통(project IS NULL)."""
    try:
        _conf = _CONFIDENCE.get("tool_strategy", 0.3)
        async with _get_pool().acquire() as conn:
            # #22: 프로젝트 필터링 통일 — 항상 (project = $1 OR project IS NULL)
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT id, key, value FROM ai_observations
                    WHERE category IN ('project_pattern', 'tool_strategy')
                      AND confidence >= $2
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        (confidence * EXP(-0.1 * EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at))) / 86400)) DESC
                    LIMIT 10
                    """,
                    project_id, _conf,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, key, value FROM ai_observations
                    WHERE category IN ('project_pattern', 'tool_strategy')
                      AND confidence >= $1
                    ORDER BY (confidence * EXP(-0.1 * EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at))) / 86400)) DESC
                    LIMIT 10
                    """,
                    _conf,
                )
            if not rows:
                return "", []
            # C: value 앞 50자 기준 중복 제거 (동일 내용 다중 프로젝트 저장 방지)
            _seen_vals: set = set()
            _deduped = []
            for r in rows:
                _vkey = (r["value"] or "")[:50]
                if _vkey not in _seen_vals:
                    _seen_vals.add(_vkey)
                    _deduped.append(r)
            used_ids = [r["id"] for r in _deduped]
            lines = [f"- {r['value']}" for r in _deduped]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["tool_strategy"]), used_ids
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="tool_strategy", error=str(e))
        return "", []


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


async def _build_discoveries(project_id: Optional[str] = None) -> tuple[str, list[int]]:
    """섹션 5: 이전 작업 발견 사항 — 해당 프로젝트 + 공통(project IS NULL)."""
    try:
        _conf = _CONFIDENCE.get("discovery", 0.4)
        async with _get_pool().acquire() as conn:
            # #22: 프로젝트 필터 통일
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT id, key, value FROM ai_observations
                    WHERE category IN ('learning', 'recurring_issue', 'discovery')
                      AND confidence >= $2
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        (confidence * EXP(-0.1 * EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at))) / 86400)) DESC
                    LIMIT 10
                    """,
                    project_id, _conf,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, key, value FROM ai_observations
                    WHERE category IN ('learning', 'recurring_issue', 'discovery')
                      AND confidence >= $1
                    ORDER BY (confidence * EXP(-0.1 * EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at))) / 86400)) DESC
                    LIMIT 10
                    """,
                    _conf,
                )
            if not rows:
                return "", []
            used_ids = [r["id"] for r in rows]
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["discoveries"]), used_ids
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="discoveries", error=str(e))
        return "", []


async def _build_correction_directives(project_id: Optional[str] = None) -> str:
    """Reflexion(B1/auto_reflexion_loop) correction_directive →
    다음 턴 시스템 프롬프트 강제 주입.
    MPR 구조화: fail_count 높은 순 + 최근 성공 항목 완화.
    failure_type/fail_count/success_count/improvement_hint 기반 구조화 포맷."""
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_meta_memory
                    WHERE category = 'correction_directive'
                      AND (project = $1 OR project IS NULL)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        CASE
                            WHEN value->>'last_outcome' = 'success'
                             AND COALESCE(NULLIF(value->>'success_count', '')::int, 0) >= 2
                            THEN 1 ELSE 0
                        END,
                        GREATEST(
                            COALESCE(NULLIF(value->>'fail_count', '')::int, NULLIF(value->>'trigger_count', '')::int, 1)
                            - COALESCE(NULLIF(value->>'success_count', '')::int, 0),
                            0
                        ) DESC,
                        COALESCE(NULLIF(value->>'fail_count', '')::int, NULLIF(value->>'trigger_count', '')::int, 1) DESC,
                        updated_at DESC
                    LIMIT 7
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_meta_memory
                    WHERE category = 'correction_directive'
                    ORDER BY
                        CASE
                            WHEN value->>'last_outcome' = 'success'
                             AND COALESCE(NULLIF(value->>'success_count', '')::int, 0) >= 2
                            THEN 1 ELSE 0
                        END,
                        GREATEST(
                            COALESCE(NULLIF(value->>'fail_count', '')::int, NULLIF(value->>'trigger_count', '')::int, 1)
                            - COALESCE(NULLIF(value->>'success_count', '')::int, 0),
                            0
                        ) DESC,
                        COALESCE(NULLIF(value->>'fail_count', '')::int, NULLIF(value->>'trigger_count', '')::int, 1) DESC,
                        updated_at DESC
                    LIMIT 7
                    """,
                )
            if not rows:
                return ""
            lines = []
            for r in rows:
                val = _json_dict(r["value"])
                if not val:
                    val = {"directive": str(r["value"])}
                ft = val.get("failure_type") or str(r["key"]).split(":")[-1] or "unknown"
                fc = _safe_int(val.get("fail_count", val.get("trigger_count")), 1)
                sc = _safe_int(val.get("success_count"), 0)
                last_outcome = str(val.get("last_outcome") or "")
                strength = str(val.get("directive_strength") or "")
                directive = _compact_inline(val.get("directive") or str(r["value"]), 90)
                hint = _compact_inline(val.get("improvement_hint"), 70)

                suffix = ""
                prefix = ""
                if last_outcome == "success" and sc >= 2:
                    suffix = ", 최근 성공"
                    prefix = "유지점검: "
                elif strength == "critical":
                    prefix = "강제교정: "
                elif strength == "strong":
                    prefix = "강화교정: "

                line = f"- [{ft} 실패 {fc}회/성공 {sc}회{suffix}] {prefix}{directive}"
                if hint:
                    line += f" 개선힌트: {hint}"
                lines.append(line)
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["correction_directives"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="correction_directives", error=str(e))
        return ""


async def _build_quality_booster(session_id: Optional[str] = None) -> str:
    """Self-Refine 품질 부스터 — 직전 응답 저품질 시 다음 턴 타겟 교정 주입.
    세션 기반으로 가장 최근 부스터 1건만 조회. 품질 회복 시 자동 삭제됨."""
    if not session_id:
        return ""
    try:
        async with _get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """SELECT value FROM ai_meta_memory
                   WHERE category = 'quality_booster'
                     AND key = $1
                     AND updated_at > NOW() - interval '2 hours'""",
                f"booster:{session_id[:16]}",
            )
            if not row:
                return ""
            import json as _json
            val = row["value"]
            if isinstance(val, str):
                try:
                    val = _json.loads(val)
                except Exception:
                    return ""
            booster = val.get("booster", "") if isinstance(val, dict) else str(val)
            return booster[:300] if booster else ""
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="quality_booster", error=str(e))
        return ""


async def _build_strategy_updates(project_id: Optional[str] = None) -> str:
    """## 전략 수정 내역 섹션 — ai_meta_memory strategy_update 최근 3건.
    auto_reflexion_loop가 3회 연속 실패 감지 시 생성한 전략 갱신 지시를 별도 섹션으로 주입.
    escalation_needed=true 항목은 강조 표시."""
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_meta_memory
                    WHERE category = 'strategy_update'
                      AND (project IS NULL OR project = $1)
                    ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
                    LIMIT 3
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_meta_memory
                    WHERE category = 'strategy_update'
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
                    strategy_str = val.get("strategy") or val.get("directive") or _json.dumps(val, ensure_ascii=False)
                    escalation = val.get("escalation_needed", False)
                    count = val.get("trigger_count", "")
                    suffix = f" (실패 {count}회, 즉시대응필요)" if escalation and count else ""
                else:
                    strategy_str = str(val)
                    suffix = ""
                lines.append(f"- {strategy_str[:200]}{suffix}")
            text = "## 전략 수정 내역\n" + "\n".join(lines)
            return _truncate(text, 500)
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="strategy_updates", error=str(e))
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


async def _build_experience_lessons(project_id: Optional[str] = None) -> str:
    """섹션 8 (AADS-P1-1): 대화 중 실시간 추출 교훈.
    category='experience_lesson' 최근 5건.
    해당 프로젝트 우선, 없으면 전역 공통 순으로 조회."""
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category = 'experience_lesson'
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        COALESCE(updated_at, created_at) DESC NULLS LAST
                    LIMIT 5
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category = 'experience_lesson'
                    ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
                    LIMIT 5
                    """,
                )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["experience_lessons"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="experience_lessons", error=str(e))
        return ""


async def _build_visual_memories(project_id: Optional[str] = None) -> str:
    """섹션 9: 시각 메모리 — CEO가 공유한 이미지 분석 결과 최근 3건.

    category='visual_memory'로 저장된 ai_observations을 조회.
    프로젝트 필터 적용 (해당 프로젝트 우선, 없으면 전역 공통 포함).
    """
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category = 'visual_memory'
                      AND (project IS NULL OR project = $1)
                    ORDER BY
                        CASE WHEN project = $1 THEN 0 ELSE 1 END,
                        COALESCE(updated_at, created_at) DESC NULLS LAST
                    LIMIT 3
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT key, value FROM ai_observations
                    WHERE category = 'visual_memory'
                    ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
                    LIMIT 3
                    """,
                )
            if not rows:
                return ""
            lines = [f"- {r['value']}" for r in rows]
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["visual_memories"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="visual_memories", error=str(e))
        return ""


# ── P1: 구조화 경험 메모리 + 절차 메모리 ────────────────────────────────────────

_DOMAIN_MAP = {
    "AADS": ["aads", "memory", "pipeline", "watchdog", "prompt"],
    "KIS": ["kis", "trading", "stock", "order"],
    "GO100": ["go100", "investment", "backtest"],
    "SF": ["sf", "shortflow", "video"],
    "NTV2": ["ntv2", "newtalk", "social", "ui_ux"],
    "NAS": ["nas", "image"],
}


async def _build_experience_memory(project_id: Optional[str] = None) -> str:
    """섹션 10: 구조화 경험 메모리 — experience_memory 테이블에서 RIF 점수 기반 상위 3건.
    domain 필터로 프로젝트 관련 경험 우선 검색. access_count 증가로 RIF 감쇠 반영."""
    try:
        async with _get_pool().acquire() as conn:
            domains = _DOMAIN_MAP.get(project_id, []) if project_id else []
            if domains:
                rows = await conn.fetch(
                    """
                    SELECT id, experience_type, title, content, rif_score, domain
                    FROM experience_memory
                    WHERE rif_score > 0.3
                    ORDER BY
                        CASE WHEN domain = ANY($1::text[]) THEN 0 ELSE 1 END,
                        rif_score DESC, updated_at DESC
                    LIMIT 3
                    """,
                    domains,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, experience_type, title, content, rif_score, domain
                    FROM experience_memory
                    WHERE rif_score > 0.3
                    ORDER BY rif_score DESC, updated_at DESC
                    LIMIT 3
                    """,
                )
            if not rows:
                return ""
            import json as _json
            lines = []
            ids_to_update = []
            for r in rows:
                content = r["content"]
                if isinstance(content, str):
                    try:
                        content = _json.loads(content)
                    except Exception:
                        content = {"summary": content}
                summary = content.get("summary") or content.get("lesson") or str(content)[:120]
                lines.append(f"- [{r['experience_type']}] {r['title']}: {summary[:150]}")
                ids_to_update.append(r["id"])
            # RIF access_count 증가 (비차단)
            if ids_to_update:
                asyncio.create_task(_update_experience_access(ids_to_update))
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["experience_memory"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="experience_memory", error=str(e))
        return ""


async def _update_experience_access(ids: list[int]):
    """experience_memory access_count 증가 + RIF 감쇠."""
    try:
        async with _get_pool().acquire() as conn:
            await conn.execute("""
                UPDATE experience_memory
                SET access_count = access_count + 1,
                    last_accessed = NOW(),
                    rif_score = GREATEST(0.1, rif_score * 0.98)
                WHERE id = ANY($1::int[])
            """, ids)
    except Exception:
        pass


async def _build_procedural_memory(project_id: Optional[str] = None) -> str:
    """섹션 11: 절차 메모리 — procedural_memory 테이블에서 성공률 상위 3건.
    반복 수행되는 작업 패턴(배포, 디버깅, 검수 등)의 최적 절차를 주입."""
    try:
        async with _get_pool().acquire() as conn:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT procedure_name, steps, success_rate, execution_count, procedure_type
                    FROM procedural_memory
                    WHERE success_rate > 0.5
                      AND (agent_name = '' OR agent_name = $1 OR agent_name IS NULL)
                    ORDER BY success_rate DESC, execution_count DESC
                    LIMIT 3
                    """,
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT procedure_name, steps, success_rate, execution_count, procedure_type
                    FROM procedural_memory
                    WHERE success_rate > 0.5
                    ORDER BY success_rate DESC, execution_count DESC
                    LIMIT 3
                    """,
                )
            if not rows:
                return ""
            import json as _json
            lines = []
            for r in rows:
                steps = r["steps"]
                if isinstance(steps, str):
                    try:
                        steps = _json.loads(steps)
                    except Exception:
                        steps = []
                if isinstance(steps, list):
                    step_summary = " → ".join(
                        s[:30] if isinstance(s, str) else str(s)[:30]
                        for s in steps[:5]
                    )
                elif isinstance(steps, dict):
                    step_summary = " → ".join(str(v)[:30] for v in list(steps.values())[:5])
                else:
                    step_summary = str(steps)[:100]
                rate_pct = int(r["success_rate"] * 100)
                lines.append(f"- [{r['procedure_type'] or 'general'}] {r['procedure_name']} ({rate_pct}%성공/{r['execution_count']}회): {step_summary}")
            text = "\n".join(lines)
            return _truncate(text, _BUDGET["procedural_memory"])
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="procedural_memory", error=str(e))
        return ""


# ── 메모리 사용 로깅 ──────────────────────────────────────────────────────────

async def _log_memory_usage(session_id: str, observation_ids: list[int]):
    """메모리 사용 이력 기록 (비차단). ai_observations의 usage_count 증가."""
    try:
        async with _get_pool().acquire() as conn:
            await conn.execute("""
                UPDATE ai_observations
                SET usage_count = COALESCE(usage_count, 0) + 1,
                    last_used_at = NOW()
                WHERE id = ANY($1::int[])
            """, observation_ids)
    except Exception as e:
        logger.warning("memory_usage_log_failed", error=str(e))


# ── 메인 빌더 ────────────────────────────────────────────────────────────────


async def _build_knowledge_graph_context(project_id: Optional[str] = None) -> str:
    """섹션 12: 지식그래프 — 프로젝트 핵심 엔티티 허브 + 관계 요약.
    kg_entities/kg_relations에서 mention_count 상위 엔티티와 1-hop 관계를 주입."""
    try:
        from app.core.knowledge_graph import query_graph_context
        text = await query_graph_context(project=project_id, top_k=5)
        return _truncate(text, _BUDGET["knowledge_graph"]) if text else ""
    except Exception as e:
        logger.warning("memory_recall_section_failed", section="knowledge_graph", error=str(e))
        return ""


async def build_memory_context(
    session_id: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """
    매 턴마다 호출하여 시스템 프롬프트에 주입할 메모리 블록을 조립.
    project_id에 따라 해당 프로젝트 메모리 우선 주입.
    총 2,000 토큰 이내. 실패 시 빈 문자열 반환 (기본 프롬프트 유지).
    """
    # #23: project 정규화
    project_id = _normalize_project(project_id)
    blocks: List[str] = []

    # 14개 섹션 병렬 조회 (P2: knowledge_graph 추가)
    (
        notes, prefs_result, tools_result, dirs,
        disc_result, learned, corrections, exp_lessons, visual_mems, strategy,
        quality_booster, exp_memory, proc_memory, kg_context,
    ) = await asyncio.gather(
        _build_session_notes(session_id, project_id, tenant_id, user_id),
        _build_preferences(),
        _build_tool_strategy(project_id),
        _build_active_directives(project_id),
        _build_discoveries(project_id),
        _build_learned_memory(project_id),
        _build_correction_directives(project_id),
        _build_experience_lessons(project_id),
        _build_visual_memories(project_id),
        _build_strategy_updates(project_id),
        _build_quality_booster(session_id),
        _build_experience_memory(project_id),
        _build_procedural_memory(project_id),
        _build_knowledge_graph_context(project_id),
    )

    # tuple unpacking: (text, used_ids)
    prefs, prefs_ids = prefs_result
    tools, tools_ids = tools_result
    disc, disc_ids = disc_result

    # 메모리 사용 이력 기록 (비차단)
    used_ids = prefs_ids + tools_ids + disc_ids
    if used_ids and session_id:
        asyncio.create_task(_log_memory_usage(session_id, used_ids))

    # Self-Refine 품질 부스터 — 직전 저품질 응답 시 최우선 주입 (corrections보다 먼저)
    if quality_booster:
        blocks.append(f"<quality_booster>\n{quality_booster}\n</quality_booster>")
        logger.info("quality_booster_injected", chars=len(quality_booster))

    # P2-FIX: correction_directive → 세션 노트(Layer2) 상단 강제 주입
    # 반성 지시사항이 세션 맥락과 함께 전달되어 행동 변화 유도력 향상
    # 별도 블록(최우선) + 세션 노트 내부 이중 배치로 절대 누락 방지
    if corrections:
        blocks.append(f"<corrections>\n⚠️ 반성지시:\n{corrections}\n</corrections>")
        correction_count = sum(1 for line in corrections.splitlines() if line.startswith("- "))
        logger.info("correction_directive_injected", count=correction_count, chars=len(corrections))
    # 세션 노트에 correction_directive 상단 강제 주입 (Layer2 통합)
    _correction_header = f"⚠️ 즉시반영:\n{corrections}" if corrections else ""
    if _correction_header and notes:
        notes = f"{_correction_header}\n---\n{notes}"
    elif _correction_header and not notes:
        notes = _correction_header
    if notes:
        blocks.append(f"<session>\n{notes}\n</session>")
    if prefs:
        blocks.append(f"<ceo_rules>\n{prefs}\n</ceo_rules>")
    if tools:
        blocks.append(f"<tools>\n{tools}\n</tools>")
    if dirs:
        blocks.append(f"<directives>\n{dirs}\n</directives>")
    if disc:
        blocks.append(f"<discoveries>\n{disc}\n</discoveries>")
    if learned:
        blocks.append(f"<learned>\n{learned}\n</learned>")
    if exp_lessons:
        blocks.append(f"<experience_lessons>\n## 최근 학습 교훈\n{exp_lessons}\n</experience_lessons>")
    if visual_mems:
        blocks.append(f"<visual_memories>\n## 시각 메모리\n{visual_mems}\n</visual_memories>")
    if strategy:
        blocks.append(f"<strategy_updates>\n{strategy}\n</strategy_updates>")
    if exp_memory:
        blocks.append(f"<experience_memory>\n## 구조화 경험\n{exp_memory}\n</experience_memory>")
    if proc_memory:
        blocks.append(f"<procedural_memory>\n## 절차 메모리\n{proc_memory}\n</procedural_memory>")
    if kg_context:
        blocks.append(f"<knowledge_graph>\n## 지식그래프\n{kg_context}\n</knowledge_graph>")

    result = "\n\n".join(blocks).strip() if blocks else ""

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


# ── 메모리 중복 제거 ────────────────────────────────────────────────────────────


async def deduplicate_observations() -> dict:
    """ai_observations 테이블의 중복 항목 정리.

    같은 category + key + COALESCE(project, '') 그룹에서:
    1. confidence 최대값을 해당 그룹 대표 행(최신)에 반영
    2. 나머지 중복 행은 memory_archive에 백업 후 삭제
    결과: {"removed": int, "kept": int} 반환
    """
    try:
        async with _get_pool().acquire() as conn:
            # 트랜잭션 내에서 백업 → 삭제 → 업데이트 원자적 실행
            async with conn.transaction():
                # 1) 중복 행 식별 (ROW_NUMBER > 1 = 삭제 대상)
                dup_rows = await conn.fetch("""
                    WITH ranked AS (
                        SELECT id, category, key, value, confidence, project, created_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY category, key, COALESCE(project, '')
                                ORDER BY confidence DESC, updated_at DESC NULLS LAST
                            ) AS rn
                        FROM ai_observations
                    )
                    SELECT id, category, key, value, confidence, project, created_at
                    FROM ranked WHERE rn > 1
                """)

                if not dup_rows:
                    kept = await conn.fetchval("SELECT COUNT(*) FROM ai_observations")
                    return {"removed": 0, "kept": kept}

                dup_ids = [r["id"] for r in dup_rows]

                # 2) memory_archive에 백업
                await conn.executemany("""
                    INSERT INTO memory_archive
                        (source_table, source_id, category, key, value, confidence, project, original_created_at)
                    VALUES ('ai_observations', $1, $2, $3, $4, $5, $6, $7)
                """, [
                    (r["id"], r["category"], r["key"], str(r["value"] or ""),
                     float(r["confidence"]) if r["confidence"] is not None else None,
                     r["project"], r["created_at"])
                    for r in dup_rows
                ])

                # 3) 중복 행 삭제
                await conn.execute("""
                    DELETE FROM ai_observations WHERE id = ANY($1::int[])
                """, dup_ids)

                # 4) 남은 행의 confidence를 그룹 내 최대값으로 업데이트
                await conn.execute("""
                    UPDATE ai_observations ao SET confidence = sub.max_conf
                    FROM (
                        SELECT category, key, COALESCE(project, '') AS proj,
                               MAX(confidence) AS max_conf
                        FROM ai_observations
                        GROUP BY category, key, COALESCE(project, '')
                    ) sub
                    WHERE ao.category = sub.category
                      AND ao.key = sub.key
                      AND COALESCE(ao.project, '') = sub.proj
                      AND ao.confidence < sub.max_conf
                """)

                kept = await conn.fetchval("SELECT COUNT(*) FROM ai_observations")

            removed = len(dup_ids)
            logger.info("deduplicate_observations_done", removed=removed, kept=kept)
            return {"removed": removed, "kept": kept}
    except Exception as e:
        logger.error("deduplicate_observations_failed", error=str(e))
        return {"removed": 0, "kept": 0, "error": str(e)}


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
                (SELECT ROUND(AVG(quality_score)::numeric, 1) FROM chat_messages WHERE quality_score IS NOT NULL AND created_at > NOW() - INTERVAL '7 days') AS avg_quality,
                (SELECT COUNT(*) FROM kg_entities) AS kg_entity_count,
                (SELECT COUNT(*) FROM kg_relations) AS kg_relation_count
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
            "kg_entities": row["kg_entity_count"] if row else 0,
            "kg_relations": row["kg_relation_count"] if row else 0,
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


# ── 학습 헬스 모니터 + 자동 재스캔 ──────────────────────────────────────────────

# 학습 트리거 키워드 (순환 임포트 방지를 위해 별도 정의)
_LEARNING_TRIGGERS = {
    "correction": [
        "아니", "틀렸", "그게 아니라", "다시 해", "잘못", "아닌데",
        "수정해", "바꿔", "변경해", "고쳐", "안돼", "왜 이래", "이상해",
    ],
    "preference": [
        "항상", "앞으로", "기억해", "절대", "반드시", "무조건", "금지",
        "좋겠", "해줘", "이렇게", "저렇게", "중요", "우선",
    ],
    "positive": [
        "잘했", "좋아", "이대로", "완벽", "훌륭", "정확",
        "맞아", "그래", "오케이", "OK", "좋네", "괜찮",
    ],
}


async def check_learning_health(hours: int = 6) -> dict:
    """최근 N시간 대화량 vs 학습량 비교.
    Returns: {"messages": int, "learnings": int, "healthy": bool, "action_needed": str|None}
    """
    try:
        async with _get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM chat_messages
                     WHERE role = 'user'
                       AND created_at > NOW() - make_interval(hours => $1)
                       AND (intent IS NULL OR intent != 'system_trigger')
                    ) AS msg_count,
                    (SELECT COUNT(*) FROM ai_observations
                     WHERE key LIKE 'chat_learning_%' OR key LIKE 'rescan_learning_%'
                       AND updated_at > NOW() - make_interval(hours => $1)
                    ) AS learn_count
                """,
                hours,
            )
            messages = row["msg_count"] if row else 0
            learnings = row["learn_count"] if row else 0

            action_needed = None
            healthy = True
            if messages >= 10 and learnings <= 1:
                action_needed = "rescan"
                healthy = False
            elif messages >= 5 and learnings == 0:
                action_needed = "rescan"
                healthy = False

            return {
                "messages": messages,
                "learnings": learnings,
                "healthy": healthy,
                "action_needed": action_needed,
            }
    except Exception as e:
        logger.warning("check_learning_health_failed", error=str(e))
        return {"messages": 0, "learnings": 0, "healthy": True, "action_needed": None, "error": str(e)}


async def rescan_recent_conversations(hours: int = 6) -> dict:
    """최근 대화를 재스캔하여 학습 트리거를 다시 분석.
    Returns: {"scanned": int, "extracted": int, "alerted": bool}
    """
    import hashlib

    extracted = 0
    alerted = False

    try:
        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content FROM chat_messages
                WHERE role = 'user'
                  AND created_at > NOW() - make_interval(hours => $1)
                  AND (intent IS NULL OR intent != 'system_trigger')
                  AND LENGTH(content) > 5
                ORDER BY created_at DESC
                LIMIT 200
                """,
                hours,
            )

        scanned = len(rows)

        for row in rows:
            content = row["content"]
            msg_hash = hashlib.md5(content[:50].encode()).hexdigest()[:8]

            for trigger_type, keywords in _LEARNING_TRIGGERS.items():
                matched = [kw for kw in keywords if kw in content]
                if not matched:
                    continue

                key = f"rescan_learning_{msg_hash}"
                if trigger_type == "correction":
                    category, confidence = "ceo_correction", 0.7
                elif trigger_type == "preference":
                    category, confidence = "ceo_preference", 0.8
                else:
                    category, confidence = "ceo_preference", 0.6

                saved = await save_observation(
                    category=category,
                    key=key,
                    content=f"[재스캔] {content[:200]}",
                    source="rescan",
                    confidence=confidence,
                    project="AADS",
                )
                if saved:
                    extracted += 1
                break  # 메시지당 1건만 추출

        # 대화 많은데 추출 0건 → 텔레그램 알림
        if scanned >= 5 and extracted == 0:
            try:
                from app.services.telegram_bot import get_telegram_bot
                bot = get_telegram_bot()
                if bot and bot.is_ready:
                    await bot.send_message(
                        f"⚠️ 메모리 학습 이상: 최근 {hours}시간 대화 {scanned}건 중 학습 추출 0건\n"
                        "학습 트리거 키워드 확장이 필요할 수 있습니다."
                    )
                    alerted = True
            except Exception as e:
                logger.warning("rescan_telegram_alert_failed", error=str(e))

        logger.info("rescan_recent_conversations_done", scanned=scanned, extracted=extracted, alerted=alerted)
        return {"scanned": scanned, "extracted": extracted, "alerted": alerted}
    except Exception as e:
        logger.warning("rescan_recent_conversations_failed", error=str(e))
        return {"scanned": 0, "extracted": 0, "alerted": False, "error": str(e)}
