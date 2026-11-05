"""러너 작업 내부 제출 — 사람 인증과 분리된 순수 제출 함수.

HTTP 핸들러(pipeline_runner.submit_job)의 게이트(세션·목표·목업 등)는 거치지 않는다. 호출자(서버 코드)가
권한 검증을 이미 끝냈다는 전제이며, 정본 문서 수정 요청(change-request)과 이후 단계(E·F)가 재사용한다.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional

import structlog

from app.core.project_config import PROJECT_MAP
from app.services.document_refs import with_rdoc_block

logger = structlog.get_logger()

SUPPORTED_PROJECTS = frozenset(PROJECT_MAP) | {"ACCT"}
ACTIVE_STATUSES = ["queued", "claimed", "running", "awaiting_approval", "approved", "deploying", "rolling_back"]


class UnsupportedProject(ValueError):
    """러너가 실행하지 못하는 프로젝트."""


def instruction_hash(project: str, instruction: str) -> str:
    # pipeline_runner._compute_instruction_hash 와 같은 식이어야 중복 판정이 맞는다.
    return hashlib.sha256(f"{project}:{instruction}".encode()).hexdigest()[:16]


async def submit_runner_job_internal(conn: Any, *, tenant_id: str, project: str, instruction: str,
                                     session_id: Optional[str] = None, size: str = "M", max_cycles: int = 3,
                                     origin: str = "internal") -> dict:
    """작업을 queued 로 넣고 알림을 보낸다. 호출자의 트랜잭션 안에서 부를 수 있다.

    반환: {job_id, status, idempotent}. 같은 프로젝트·지시서의 활성 작업이 있으면 그 job_id 를 돌려준다
    (status='dedup', idempotent=True). 미지원 프로젝트는 UnsupportedProject.
    """
    project = project.upper()
    if project not in SUPPORTED_PROJECTS:
        raise UnsupportedProject(project)
    digest = instruction_hash(project, instruction)
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", f"pipeline_jobs:{digest}")
    existing = await conn.fetchrow(
        "SELECT job_id, status FROM pipeline_jobs WHERE project=$1 AND instruction_hash=$2 "
        "AND tenant_id=$3::uuid AND status=ANY($4::text[]) ORDER BY created_at DESC LIMIT 1",
        project, digest, tenant_id, ACTIVE_STATUSES)
    if existing:
        return {"job_id": existing["job_id"], "status": "dedup", "idempotent": True}
    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    await conn.execute(
        "INSERT INTO pipeline_jobs (job_id, project, instruction, instruction_hash, chat_session_id, "
        "status, phase, max_cycles, size, logs, created_at, updated_at, tenant_id) "
        "VALUES ($1,$2,$3,$4,$5::uuid,'queued','queued',$6,$7,"
        "jsonb_build_array(jsonb_build_object('ts', NOW()::text, 'event', 'internal_submit', 'origin', $8::text)),"
        "NOW(), NOW(), $9::uuid)",
        job_id, project, with_rdoc_block(instruction, job_id), digest, session_id or None,
        max_cycles, size, origin, tenant_id)
    await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", job_id)
    logger.info("runner_job_submit.internal", job_id=job_id, project=project, origin=origin)
    return {"job_id": job_id, "status": "queued", "idempotent": False}
