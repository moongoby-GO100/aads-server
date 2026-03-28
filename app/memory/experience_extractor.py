"""
Experience Extractor - Agent KB Pattern
프로젝트 완료 시 Strategy(전략)와 Lesson(교훈)을 자동 추출.
AADS-P1-1: 대화 중 실시간 교훈 추출 (extract_mid_conversation_lessons) 추가.
"""
from __future__ import annotations

import json
import logging
import re
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

# 교훈 추출 키워드 패턴
_LESSON_KEYWORDS: list[tuple[str, str]] = [
    # (패턴, 교훈 유형)
    ("이렇게 하면 안", "failure_pattern"),
    ("하지 마", "failure_pattern"),
    ("실패", "failure_pattern"),
    ("오류", "failure_pattern"),
    ("에러", "failure_pattern"),
    ("이건 좋았어", "success_pattern"),
    ("잘 됐어", "success_pattern"),
    ("성공", "success_pattern"),
    ("효과적", "success_pattern"),
    ("다음부터", "future_rule"),
    ("앞으로", "future_rule"),
    ("항상", "future_rule"),
    ("절대", "future_rule"),
    ("기억해", "future_rule"),
    ("주의", "caution"),
    ("조심", "caution"),
    ("확인해", "caution"),
]

# 도구 이름 패턴 (연속 사용 감지)
_TOOL_PATTERN = re.compile(
    r"\b(patch_remote_file|write_remote_file|read_remote_file|run_remote_command"
    r"|git_remote_commit|git_remote_push|query_db|search_logs)\b",
    re.IGNORECASE,
)


def _extract_lessons_from_messages(
    messages: List[Dict[str, Any]],
) -> List[tuple[str, str]]:
    """
    키워드/패턴 기반으로 교훈 후보를 추출한다.
    Returns: [(lesson_text, lesson_type), ...]
    """
    lessons: List[tuple[str, str]] = []
    tool_usage: List[str] = []  # 연속 도구 사용 추적

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = str(msg.get("content", ""))

        # ── 도구 연속 사용 패턴 ──────────────────────────────
        found_tools = _TOOL_PATTERN.findall(content)
        tool_usage.extend(t.lower() for t in found_tools)

        # ── 키워드 기반 교훈 추출 ─────────────────────────────
        # user 메시지에서만 교훈 키워드 탐색 (CEO 지시/피드백 우선)
        if role == "user":
            for keyword, lesson_type in _LESSON_KEYWORDS:
                if keyword in content:
                    # 해당 키워드가 포함된 문장 추출 (최대 150자)
                    sentences = re.split(r"[.。\n]", content)
                    for sentence in sentences:
                        if keyword in sentence:
                            text = sentence.strip()[:150]
                            if text and len(text) > 10:
                                lessons.append((text, lesson_type))
                                break

        # ── 에러 후 성공 패턴 탐색 ───────────────────────────
        if role == "assistant" and i > 0:
            prev_content = str(messages[i - 1].get("content", ""))
            has_error = any(w in prev_content for w in ("에러", "오류", "실패", "error", "Error"))
            has_success = any(w in content for w in ("성공", "완료", "해결", "수정했", "처리했"))
            if has_error and has_success:
                # 앞 user 메시지에서 문제 맥락 추출
                ctx = prev_content.strip()[:100]
                lesson_text = f"에러 후 성공 패턴: '{ctx}' → 해결됨"
                lessons.append((lesson_text, "recovery_pattern"))

    # ── 도구 반복 사용 패턴 ──────────────────────────────────
    if len(tool_usage) >= 3:
        from collections import Counter
        tool_counts = Counter(tool_usage)
        for tool, cnt in tool_counts.most_common(2):
            if cnt >= 3:
                lessons.append(
                    (f"도구 선호 패턴: {tool}를 {cnt}회 사용 (이 대화)", "tool_preference")
                )

    # 중복 제거 (동일 텍스트)
    seen: set[str] = set()
    unique: List[tuple[str, str]] = []
    for text, ltype in lessons:
        if text not in seen:
            seen.add(text)
            unique.append((text, ltype))

    return unique[:5]  # 최대 5개


async def extract_mid_conversation_lessons(
    messages: List[Dict[str, Any]],
    project: str,
    pool=None,
) -> int:
    """
    대화 중 실시간 교훈 추출 (매 20턴마다 호출).
    LLM 호출 없이 키워드/패턴 기반으로 교훈을 추출하여
    ai_observations 테이블에 category='experience_lesson', confidence=0.6으로 저장.

    Args:
        messages: 최근 대화 메시지 리스트 (role/content dict 형태)
        project:  프로젝트명 (예: 'AADS', 'KIS', 'GO100')
        pool:     asyncpg 커넥션 풀 (None이면 중앙 풀 사용)

    Returns:
        저장된 교훈 건수 (실패 시 0)
    """
    import hashlib
    from app.core.memory_recall import save_observation

    if not messages:
        return 0

    # 최근 20턴만 분석
    recent = messages[-20:]
    project_upper = (project or "AADS").upper().strip()

    # 키워드/패턴 기반 교훈 추출 (LLM 호출 없음)
    candidates = _extract_lessons_from_messages(recent)
    if not candidates:
        logger.debug(f"extract_mid_conversation_lessons: 교훈 없음 project={project_upper}")
        return 0

    # 중복 방지용 해시 — 최근 메시지 앞 100자 기반
    convo_sample = "".join(
        str(m.get("content", ""))[:50] for m in recent[:4]
    )
    convo_hash = hashlib.md5(convo_sample.encode("utf-8", errors="ignore")).hexdigest()[:8]

    saved = 0
    for idx, (lesson_text, lesson_type) in enumerate(candidates):
        key = f"mid_conv_{convo_hash}_{lesson_type}_{idx}"
        ok = await save_observation(
            category="experience_lesson",
            key=key,
            content=lesson_text,
            source="mid_conversation",
            confidence=0.6,
            project=project_upper,
        )
        if ok:
            saved += 1

    logger.info(f"extract_mid_conversation_lessons: project={project_upper}, saved={saved}")
    return saved
