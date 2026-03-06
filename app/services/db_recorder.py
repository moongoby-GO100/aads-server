"""
AADS-130: 산출물 DB 기록 공통 서비스.
모든 에이전트 산출물을 project_artifacts 테이블에 통합 저장.
"""
from __future__ import annotations

import json
import os
import structlog

logger = structlog.get_logger()


async def record_artifact(
    project_id: str,
    artifact_type: str,
    artifact_name: str,
    content: dict,
    source_agent: str | None = None,
    source_task: str | None = None,
    version: int = 1,
) -> int | None:
    """
    project_artifacts 테이블에 산출물 저장.
    성공 시 artifact id 반환, 실패 시 None 반환 (graceful degradation).
    """
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")
    if not db_url:
        logger.warning("record_artifact_no_db_url", artifact_type=artifact_type)
        return None

    try:
        import asyncpg
        conn = await asyncpg.connect(db_url, timeout=5)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO project_artifacts
                    (project_id, artifact_type, artifact_name, content,
                     source_agent, source_task, version)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                RETURNING id
                """,
                str(project_id),
                artifact_type,
                artifact_name,
                json.dumps(content, ensure_ascii=False),
                source_agent,
                source_task,
                version,
            )
            artifact_id = row["id"] if row else None
            logger.info(
                "artifact_recorded",
                project_id=str(project_id),
                artifact_type=artifact_type,
                artifact_id=artifact_id,
            )
            return artifact_id
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("record_artifact_failed", artifact_type=artifact_type, error=str(e))
        return None


async def record_ideation_artifacts(project_id: str, ideation_result: dict) -> list[int]:
    """
    아이디에이션 서브그래프 산출물 일괄 저장.
    strategy_report, prd, architecture, phase_plan, taskspec 유형.
    """
    saved_ids: list[int] = []

    strategy_report = ideation_result.get("strategy_report")
    if strategy_report:
        aid = await record_artifact(
            project_id=project_id,
            artifact_type="strategy_report",
            artifact_name="시장조사 보고서",
            content=strategy_report,
            source_agent="strategist",
        )
        if aid:
            saved_ids.append(aid)

    prd = ideation_result.get("prd")
    if prd:
        aid = await record_artifact(
            project_id=project_id,
            artifact_type="prd",
            artifact_name="PRD (제품 요구사항 문서)",
            content=prd,
            source_agent="planner",
        )
        if aid:
            saved_ids.append(aid)

    architecture = ideation_result.get("architecture")
    if architecture:
        aid = await record_artifact(
            project_id=project_id,
            artifact_type="architecture",
            artifact_name="아키텍처 설계서",
            content=architecture,
            source_agent="planner",
        )
        if aid:
            saved_ids.append(aid)

    phase_plan = ideation_result.get("phase_plan")
    if phase_plan:
        aid = await record_artifact(
            project_id=project_id,
            artifact_type="phase_plan",
            artifact_name="Phase 계획",
            content={"phases": phase_plan},
            source_agent="planner",
        )
        if aid:
            saved_ids.append(aid)

    task_specs = ideation_result.get("task_specs")
    if task_specs:
        aid = await record_artifact(
            project_id=project_id,
            artifact_type="taskspec",
            artifact_name=f"TaskSpec 목록 ({len(task_specs)}건)",
            content={"task_specs": task_specs},
            source_agent="planner",
        )
        if aid:
            saved_ids.append(aid)

    logger.info(
        "ideation_artifacts_recorded",
        project_id=str(project_id),
        count=len(saved_ids),
    )
    return saved_ids
