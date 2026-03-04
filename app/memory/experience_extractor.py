"""
Experience Extractor - Agent KB Pattern
프로젝트 완료 시 Strategy(전략)와 Lesson(교훈)을 자동 추출
"""
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
