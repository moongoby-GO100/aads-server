"""
Experience Extractor - Agent KB Pattern
프로젝트 완료 시 Strategy(전략)와 Lesson(교훈)을 자동 추출.
AADS-P1-1: 대화 중 실시간 교훈 추출 (extract_mid_conversation_lessons) 추가.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Any
from app.memory.store import memory_store

logger = logging.getLogger(__name__)

# 임베딩 없이도 동작하는 기본 구현 (Phase 2에서 pgvector 임베딩 추가)
async def extract_and_store_experience(
    project_id: str,
    project_result: Dict[str, Any],
    agent_logs: List[Dict] = None
):
    """
    프로젝트 완료 후 경험 추출 및 저장
    project_result: {
        "description": str,
        "tech_stack": list,
        "domain": str,
        "outcome": "success" | "partial" | "failed",
        "total_cost_usd": float,
        "llm_calls_count": int,
        "duration_seconds": int,
        "generated_files": list,
        "issues_encountered": list,
        "solutions_applied": list
    }
    """
    domain = project_result.get("domain", "general")
    tech_stack = project_result.get("tech_stack", [])
    outcome = project_result.get("outcome", "unknown")

    # === Strategy 추출 (고수준 접근법) ===
    strategy = {
        "project_id": project_id,
        "description": project_result.get("description", ""),
        "domain": domain,
        "tech_stack": tech_stack,
        "approach": _extract_approach(project_result),
        "outcome": outcome,
        "cost_usd": project_result.get("total_cost_usd", 0),
        "llm_calls": project_result.get("llm_calls_count", 0),
        "file_count": len(project_result.get("generated_files", [])),
        "effectiveness_score": _calculate_effectiveness(project_result)
    }
    await memory_store.store_experience(
        experience_type="strategy",
        domain=domain,
        tags=tech_stack + [outcome],
        content=strategy
    )
    logger.info(f"Strategy stored for project {project_id}")

    # === Lesson 추출 (구체적 교훈) ===
    issues = project_result.get("issues_encountered", [])
    solutions = project_result.get("solutions_applied", [])

    for i, issue in enumerate(issues):
        lesson = {
            "project_id": project_id,
            "issue": issue,
            "solution": solutions[i] if i < len(solutions) else "unresolved",
            "domain": domain,
            "tech_stack": tech_stack,
            "severity": "high" if outcome == "failed" else "medium"
        }
        await memory_store.store_experience(
            experience_type="lesson",
            domain=domain,
            tags=tech_stack + ["issue", issue.split()[0] if issue else "unknown"],
            content=lesson
        )

    # === Procedural Memory 업데이트 (에이전트별) ===
    if agent_logs:
        for log in agent_logs:
            agent_name = log.get("agent", "unknown")
            await memory_store.store_procedure(
                agent_name=agent_name,
                procedure_type="task_pattern",
                content={
                    "project_id": project_id,
                    "task": log.get("task", ""),
                    "approach": log.get("approach", ""),
                    "success": log.get("success", False),
                    "duration_ms": log.get("duration_ms", 0)
                }
            )

    # === Project Memory 요약 저장 ===
    await memory_store.store_project_memory(
        project_id=project_id,
        memory_type="completion_summary",
        content={
            "outcome": outcome,
            "tech_stack": tech_stack,
            "total_cost_usd": project_result.get("total_cost_usd", 0),
            "files_generated": len(project_result.get("generated_files", [])),
            "strategies_extracted": 1,
            "lessons_extracted": len(issues)
        }
    )

    total = 1 + len(issues)
    logger.info(f"Experience extraction complete: {total} memories stored for project {project_id}")
    return {"strategies": 1, "lessons": len(issues), "total": total}


def _extract_approach(result: Dict) -> str:
    tech = result.get("tech_stack", [])
    desc = result.get("description", "")
    files = result.get("generated_files", [])
    return f"Tech: {', '.join(tech[:5])}. Files: {len(files)}. {desc[:200]}"


def _calculate_effectiveness(result: Dict) -> float:
    score = 0.5
    outcome = result.get("outcome", "unknown")
    if outcome == "success":
        score += 0.3
    elif outcome == "failed":
        score -= 0.3
    cost = result.get("total_cost_usd", 0)
    if cost < 1.0:
        score += 0.1
    elif cost > 10.0:
        score -= 0.1
    calls = result.get("llm_calls_count", 0)
    if calls <= 15:
        score += 0.1
    return max(0.0, min(1.0, score))


# ── 대화 중 실시간 교훈 추출 (AADS-P1-1) ────────────────────────────────────────

async def extract_mid_conversation_lessons(
    messages: List[Dict[str, Any]],
    project: str,
) -> int:
    """
    대화 중 실시간 교훈 추출 — 최근 20턴 메시지를 LLM으로 분석하여
    반복 패턴, 성공/실패 전략, 새로운 발견을 추출하고
    ai_observations 테이블에 category='experience_lesson', confidence=0.6으로 저장.

    Args:
        messages: 최근 대화 메시지 리스트 (role/content dict 형태)
        project: 프로젝트명 (예: 'AADS', 'KIS', 'GO100')

    Returns:
        저장된 교훈 건수 (실패 시 0)
    """
    from app.core.anthropic_client import call_llm_with_fallback
    from app.core.memory_recall import save_observation
    import hashlib
    import re as _re

    if not messages:
        return 0

    # 최근 20턴만 사용
    recent = messages[-20:]

    # 프롬프트 조립 — 역할별 텍스트 포맷
    convo_text = "\n".join(
        f"[{m.get('role', 'unknown').upper()}] {str(m.get('content', ''))[:300]}"
        for m in recent
    )

    prompt = (
        "다음 대화에서 반복되는 패턴, 효과적/비효과적 전략, 새로운 발견을 "
        "한국어 bullet point로 추출하세요. "
        "각 항목은 '- ' 으로 시작하고 1~2문장으로 간결하게 작성하세요. "
        "최대 5개 항목만 추출하세요.\n\n"
        f"대화:\n{convo_text}"
    )

    try:
        response = await call_llm_with_fallback(
            prompt=prompt,
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="당신은 AI 대화 분석 전문가입니다. 대화에서 학습 가능한 교훈만 간결하게 추출합니다.",
        )
    except Exception as e:
        logger.warning(f"extract_mid_conversation_lessons LLM 호출 실패: {e}")
        return 0

    if not response:
        return 0

    # bullet point 파싱
    lines = [
        line.strip()
        for line in response.splitlines()
        if line.strip().startswith("-")
    ]
    if not lines:
        return 0

    # 대화 해시 — 중복 저장 방지용 키 접두사
    convo_hash = hashlib.md5(convo_text[:100].encode("utf-8", errors="ignore")).hexdigest()[:8]
    project_upper = project.upper().strip() if project else "AADS"

    saved = 0
    for idx, lesson in enumerate(lines[:5]):
        key = f"mid_conv_{convo_hash}_{idx}"
        # '- ' 접두사 제거 후 저장
        value = _re.sub(r"^-\s*", "", lesson).strip()
        if not value:
            continue
        ok = await save_observation(
            category="experience_lesson",
            key=key,
            content=value,
            source="mid_conversation",
            confidence=0.6,
            project=project_upper,
        )
        if ok:
            saved += 1

    logger.info(f"extract_mid_conversation_lessons: project={project_upper}, saved={saved}")
    return saved
