"""
Pipeline Runner API v2 — DB 기반 작업 제출/승인/조회.

보안: 입력 검증(H6), 파라미터화 쿼리(C1), JWT 인증(C2 — main.py 미들웨어)
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from app.auth import TenantRole, require_tenant_role

router = APIRouter()
logger = structlog.get_logger(__name__)
TenantContext = dict[str, object]
require_tenant_viewer = require_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])  # type: ignore[index]

# H6 + M4: 허용 프로젝트 화이트리스트
_VALID_PROJECTS = {"AADS", "KIS", "GO100", "SF", "NTV2"}
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_JOB_ID_RE = re.compile(r'^runner-[0-9a-zA-Z_-]+$')
_ACTIVE_PIPELINE_STATUSES = (
    "queued",
    "claimed",
    "running",
    "awaiting_approval",
    "approved",
    "deploying",
    "rolling_back",
)
_TERMINAL_BLOCKING_STATUSES = (
    "error",
    "rejected",
    "rejected_done",
    "cancelled",
    "build_fail",
    "deploy_failed",
    "review_failed",
    "auth_unavailable",
    "tool_timeout",
    "dedup_blocked",
    "blocked_dependency",
)
_DISPLAY_STATUS_LABELS = {
    "no_changes": "변경 없음",
    "dedup_blocked": "중복 차단",
    "blocked_dependency": "의존 차단",
    "build_fail": "빌드 실패",
    "deploy_failed": "배포 실패",
    "review_failed": "검수 실패",
    "auth_unavailable": "인증 필요",
    "tool_timeout": "도구 타임아웃",
}
_DISPLAY_STATUS_GROUPS = {
    "no_changes": "complete",
    "dedup_blocked": "blocked",
    "blocked_dependency": "blocked",
    "build_fail": "action_required",
    "deploy_failed": "action_required",
    "review_failed": "action_required",
    "auth_unavailable": "action_required",
    "tool_timeout": "action_required",
}
_DEFAULT_LOCAL_PID_PROJECTS = {"AADS"}
_TARGET_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9@])"
    r"(?:/root/aads/(?:aads-server|aads-dashboard)/)?"
    r"[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*"
    r"\.(?:py|tsx|ts|jsx|js|sh|sql|ya?ml|json|md|html|css|toml|ini|conf)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SPECIAL_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9@])"
    r"(?:/root/aads/(?:aads-server|aads-dashboard)/)?"
    r"(?:Dockerfile|docker-compose(?:\.[A-Za-z0-9_-]+)?\.ya?ml|package-lock\.json|package\.json|deploy\.sh)"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_PATH_TRAILING_CHARS = ".,;:)]}'\"`"


def _max_concurrent_per_project() -> int:
    """API 표시/잠금 판단용 동시 실행 상한. Shell runner 기본값과 맞춘다."""
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_PER_PROJECT", "6")))
    except ValueError:
        return 6


def _record_get(row, key: str, default=None):
    """asyncpg.Record 안전 조회. Record는 dict.get을 보장하지 않는다."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _normalize_target_file_path(path: str) -> str:
    """Normalize file references so jobs touching the same file serialize."""
    value = (path or "").strip().strip(_PATH_TRAILING_CHARS)
    value = value.replace("\\", "/")
    if not value:
        return ""
    if value.startswith("/root/aads/aads-server/"):
        return f"server:{value.removeprefix('/root/aads/aads-server/')}"
    if value.startswith("/root/aads/aads-dashboard/"):
        return f"dashboard:{value.removeprefix('/root/aads/aads-dashboard/')}"
    value = re.sub(r"^\./+", "", value)
    if value.startswith("aads-server/"):
        return f"server:{value.removeprefix('aads-server/')}"
    if value.startswith("aads-dashboard/"):
        return f"dashboard:{value.removeprefix('aads-dashboard/')}"
    if value.startswith(("src/", "public/")) or value in {"package.json", "package-lock.json"}:
        return f"dashboard:{value}"
    return f"server:{value}"


def _extract_target_files(instruction: str) -> set[str]:
    """Extract explicit target files from a runner instruction."""
    files: set[str] = set()
    matches = list(_TARGET_FILE_RE.finditer(instruction or "")) + list(_SPECIAL_TARGET_RE.finditer(instruction or ""))
    for match in matches:
        normalized = _normalize_target_file_path(match.group(0))
        if normalized:
            files.add(normalized)
    return files


async def _find_active_file_conflict(
    conn,
    *,
    project: str,
    target_files: set[str],
    tenant_id: str,
    ignore_job_ids: set[str] | None = None,
) -> dict | None:
    """Return an active job touching one of target_files, if instruction paths overlap."""
    if not target_files:
        return None
    ignored = ignore_job_ids or set()
    rows = await conn.fetch(
        """
        SELECT job_id, instruction, status, phase
        FROM pipeline_jobs
        WHERE project = $1
          AND tenant_id = $2::uuid
          AND status = ANY($3::text[])
        ORDER BY created_at DESC
        LIMIT 100
        """,
        project,
        tenant_id,
        list(_ACTIVE_PIPELINE_STATUSES),
    )
    for row in rows:
        existing_job_id = row["job_id"]
        if existing_job_id in ignored:
            continue
        existing_files = _extract_target_files(row["instruction"] or "")
        overlap = target_files & existing_files
        if overlap:
            return {
                "job_id": existing_job_id,
                "status": row["status"],
                "phase": row["phase"],
                "overlap": sorted(overlap),
            }
    return None


def _local_pid_projects() -> set[str]:
    """Projects whose runner_pid belongs to this API host.

    KIS/GO100/SF/NTV2 runners execute on remote servers, so checking their
    runner_pid against this host's /proc would create false stale positives.
    """
    raw = os.getenv("PIPELINE_RUNNER_LOCAL_PID_PROJECTS", "AADS")
    projects = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return projects or set(_DEFAULT_LOCAL_PID_PROJECTS)


def _is_local_runner_project(project: str | None) -> bool:
    return (project or "").upper() in _local_pid_projects()


def _local_pid_alive(pid) -> bool | None:
    if not pid:
        return None
    try:
        return os.path.exists(f"/proc/{int(pid)}")
    except (TypeError, ValueError):
        return False


def _compute_instruction_hash(project: str, instruction: str) -> str:
    return hashlib.sha256(f"{project}:{instruction}".encode()).hexdigest()[:16]


async def _lock_instruction_hash(conn, instruction_hash: str) -> None:
    """동일 instruction_hash 제출을 트랜잭션 단위로 직렬화한다."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
        f"pipeline_jobs:{instruction_hash}",
    )


def _parallel_scope(parallel_group: str | None) -> str:
    return (parallel_group or "").strip()


async def _find_active_duplicate(conn, project: str, instruction_hash: str, parallel_group: str = "", tenant_id: str = ""):
    scope = _parallel_scope(parallel_group)
    return await conn.fetchrow(
        """
        SELECT job_id, status, phase, parallel_group
        FROM pipeline_jobs
        WHERE project = $1
          AND instruction_hash = $2
          AND COALESCE(parallel_group, '') = $3
          AND tenant_id = $4::uuid
          AND status = ANY($5::text[])
        ORDER BY
          CASE status
            WHEN 'running' THEN 0
            WHEN 'claimed' THEN 1
            WHEN 'awaiting_approval' THEN 2
            WHEN 'approved' THEN 3
            WHEN 'deploying' THEN 4
            WHEN 'rolling_back' THEN 5
            WHEN 'queued' THEN 6
            ELSE 7
          END,
          created_at ASC
        LIMIT 1
        """,
        project,
        instruction_hash,
        scope,
        tenant_id,
        list(_ACTIVE_PIPELINE_STATUSES),
    )


async def _record_dedup_blocked(conn, *, job_id: str, req: "JobSubmitRequest",
                                instruction_hash: str, existing, tenant_id: str) -> str:
    detail = (
        f"dedup_blocked: existing job {existing['job_id']} "
        f"is {existing['status']}/{existing['phase']}"
    )
    await conn.execute(
        """
        INSERT INTO pipeline_jobs
          (job_id, project, instruction, instruction_hash, chat_session_id,
           status, phase, max_cycles, size, parallel_group, depends_on,
           error_detail, review_feedback, logs, created_at, updated_at, tenant_id)
        VALUES ($1, $2, $3, $4, $5,
                'cancelled', 'dedup_blocked', $6, $7, $8, $9,
                $10, $11,
                jsonb_build_array(jsonb_build_object(
                  'ts', NOW()::text,
                  'event', 'dedup_blocked',
                  'existing_job_id', $12,
                  'existing_status', $13,
                  'existing_phase', $14,
                  'parallel_scope', $15,
                  'auto_retryable', false
                )),
                NOW(), NOW(), $16::uuid)
        """,
        job_id,
        req.project,
        req.instruction,
        instruction_hash,
        req.session_id,
        req.max_cycles,
        req.size,
        req.parallel_group or None,
        req.depends_on or None,
        detail,
        f"[Runner Guard] {detail}; auto_retryable=false",
        existing["job_id"],
        existing["status"],
        existing["phase"],
        _parallel_scope(req.parallel_group),
        tenant_id,
    )
    logger.info("pipeline_runner.submit_dedup_blocked",
                blocked_job_id=job_id,
                existing_job_id=existing["job_id"],
                instruction_hash=instruction_hash,
                parallel_scope=_parallel_scope(req.parallel_group))
    return detail


async def _record_blocked_dependency(conn, *, job_id: str, req: "JobSubmitRequest",
                                     instruction_hash: str, dep_status: str, tenant_id: str) -> str:
    detail = f"blocked_dependency: parent {req.depends_on} is {dep_status}"
    await conn.execute(
        """
        INSERT INTO pipeline_jobs
          (job_id, project, instruction, instruction_hash, chat_session_id,
           status, phase, max_cycles, size, parallel_group, depends_on,
           error_detail, review_feedback, logs, created_at, updated_at, tenant_id)
        VALUES ($1, $2, $3, $4, $5,
                'cancelled', 'blocked_dependency', $6, $7, $8, $9,
                $10, $11,
                jsonb_build_array(jsonb_build_object(
                  'ts', NOW()::text,
                  'event', 'blocked_dependency',
                  'depends_on', $12,
                  'upstream_status', $13,
                  'auto_retryable', false
                )),
                NOW(), NOW(), $14::uuid)
        """,
        job_id,
        req.project,
        req.instruction,
        instruction_hash,
        req.session_id,
        req.max_cycles,
        req.size,
        req.parallel_group or None,
        req.depends_on or None,
        detail,
        f"[Runner Guard] {detail}; auto_retryable=false",
        req.depends_on,
        dep_status,
        tenant_id,
    )
    logger.info("pipeline_runner.submit_blocked_dependency",
                blocked_job_id=job_id,
                depends_on=req.depends_on,
                upstream_status=dep_status)
    return detail


async def _get_model_for_size(conn, size: str) -> str:
    """작업 규모 → DB(runner_model_config)에서 1순위 모델 조회."""
    import json as _json_model
    from app.services.model_registry import filter_executable_models

    _size = (size or "M").upper()
    row = await conn.fetchrow(
        "SELECT models FROM runner_model_config WHERE size = $1", _size
    )
    if row and row["models"]:
        raw = row["models"]
        models = _json_model.loads(raw) if isinstance(raw, str) else raw
        if models:
            executable = await filter_executable_models(models)
            if executable:
                return executable[0]
    # DB 조회 실패 시 안전망
    return {"XS": "claude-haiku-4-5-20251001", "S": "claude-haiku-4-5-20251001",
            "M": "claude-sonnet-4-6", "L": "claude-opus-4-6",
            "XL": "claude-opus-4-6"}.get(_size, "claude-sonnet-4-6")



def _parse_size_from_instruction(instruction: str) -> str:
    """instruction 텍스트에서 규모 파싱 (AADS-206B 폴백)."""
    m = re.search(r'(?:규모|SIZE)[:\s=]*\s*(XL|XS|[SML])\b', instruction, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def _estimate_size(instruction: str) -> str:
    """instruction 복잡도 자동 추정 (P1-2 AADS-229)."""
    text = instruction.lower()
    length = len(instruction)
    complex_kw = ["리팩토링", "마이그레이션", "아키텍처", "전체", "모든 파일",
                  "refactor", "migration", "architecture", "all files",
                  "다중 서버", "multi-server", "전수", "대규모"]
    simple_kw = ["오타", "typo", "주석", "comment", "버전", "version",
                 "설정 변경", "config", "로그", "log level", "1줄", "한 줄"]
    cx = sum(1 for kw in complex_kw if kw in text)
    sx = sum(1 for kw in simple_kw if kw in text)
    fr = len(__import__("re").findall(r'[\w/]+\.(?:py|ts|tsx|js|sh|sql|yml|yaml)', text))
    if sx >= 2 or (length < 200 and cx == 0 and fr <= 1):
        return "S"
    if cx >= 3 or fr >= 10 or length > 5000:
        return "XL"
    if cx >= 2 or fr >= 5 or length > 3000:
        return "L"
    return "M"

class JobSubmitRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드")
    instruction: str = Field(..., max_length=50000, description="Claude Code에 전달할 지시")
    session_id: str = Field(..., description="채팅 세션 ID (필수 — 완료 보고 대상)")
    max_cycles: int = Field(3, ge=1, le=10, description="최대 검수 사이클")
    size: str = Field("M", description="작업 규모 (XS/S/M/L/XL) — 모델 자동 선택")
    worker_model: str = Field("", description="직접 모델 지정 (빈 문자열이면 size 기반 자동 선택)")
    worker_model_reason: str = Field("", max_length=500, description="직접 모델 지정 사유")
    parallel_group: str = Field("", description="병렬 실행 그룹 — 같은 그룹 내 작업은 동시 실행")
    depends_on: str = Field("", description="의존 작업 job_id ��� 해당 작업 완료 후에만 실행")

    @field_validator('project')
    @classmethod
    def validate_project(cls, v):
        if v not in _VALID_PROJECTS:
            raise ValueError(f"허용 프로젝트: {', '.join(sorted(_VALID_PROJECTS))}")
        return v

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v):
        if not v or not _UUID_RE.match(v):
            raise ValueError("session_id는 필수이며 UUID 형식이어야 합니다")
        return v


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str


def _normalize_worker_model_override(worker_model: str, reason: str) -> tuple[str, str]:
    """Require an explicit reason before persisting a worker_model override."""
    model = (worker_model or "").strip()
    override_reason = (reason or "").strip()
    if not model:
        return "", ""
    if not override_reason:
        logger.warning("pipeline_runner.worker_model_ignored_no_reason", worker_model=model)
        return "", ""
    return model, override_reason


class JobApproveRequest(BaseModel):
    action: str = Field(..., description="approve 또는 reject")
    feedback: str = Field("", max_length=2000, description="피드백")

    @field_validator('action')
    @classmethod
    def validate_action(cls, v):
        if v not in ("approve", "reject"):
            raise ValueError("action은 approve 또는 reject만 가능")
        return v


async def check_project_lock(conn, project: str, exclude_job_id: str | None = None, parallel_group: str = "", tenant_id: str = "") -> bool:
    """프로젝트에 실행 중인(running/claimed) 작업이 상한에 도달했는지 확인. True면 잠김.
    AADS-211: parallel_group이 지정되면 같은 그룹 내 작업은 동시 실행 허용."""
    max_concurrent = _max_concurrent_per_project()
    # parallel_group이 있으면 같은 그룹이 아닌 작업만 lock으로 간주
    if parallel_group:
        row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed') "
            "AND (parallel_group IS NULL OR parallel_group != $3)",
            project, tenant_id, parallel_group,
        )
        return (row["cnt"] or 0) >= max_concurrent
    if exclude_job_id:
        row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed') AND job_id != $3",
            project, tenant_id, exclude_job_id,
        )
    else:
        row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed')",
            project, tenant_id,
        )
    return (row["cnt"] or 0) >= max_concurrent


async def cascade_cleanup_orphans(conn, failed_job_id: str) -> int:
    """실패한 작업에 의존하는 모든 queued 작업을 재귀적으로 blocked 처리.
    P1-A: 고아 방지 — 의존 트리 전체를 한 번에 정리."""
    total = 0
    to_process = [failed_job_id]
    while to_process:
        current_id = to_process.pop(0)
        result = await conn.fetch(
            "UPDATE pipeline_jobs SET status = 'cancelled', phase = 'blocked_dependency', "
            "error_detail = $2, updated_at = NOW() "
            "WHERE depends_on = $1 AND status = 'queued' "
            "RETURNING job_id",
            current_id,
            f"orphaned_dependency: parent {current_id} failed",
        )
        for r in result:
            total += 1
            to_process.append(r["job_id"])
            logger.info("pipeline_runner.orphan_cascade_cleaned",
                        orphan_job_id=r["job_id"], parent=current_id)
    if total:
        logger.info("pipeline_runner.orphan_cascade_total", count=total, root=failed_job_id)
    return total


async def promote_next_queued(conn, project: str) -> str | None:
    """프로젝트 Lock 해제 후 다음 queued 작업 확인.
    AADS-211: depends_on이 설정된 작업은 의존 작업이 done일 때만 승격.
    P1-A: 의존 작업 실패 시 자동 고아 처리."""
    rows = await conn.fetch(
        "SELECT job_id, depends_on, parallel_group FROM pipeline_jobs "
        "WHERE project = $1 AND status = 'queued' "
        "ORDER BY created_at ASC LIMIT 10",
        project,
    )
    for row in rows:
        dep = row["depends_on"]
        if dep:
            # 의존 작업 상태 확인
            dep_row = await conn.fetchrow(
                "SELECT status FROM pipeline_jobs WHERE job_id = $1", dep,
            )
            if dep_row and dep_row["status"] in ("error", "rejected", "rejected_done", "cancelled"):
                # P1-A: 의존 작업 실패 → 자동 고아 처리
                await conn.execute(
                    "UPDATE pipeline_jobs SET status = 'cancelled', phase = 'blocked_dependency', "
                    "error_detail = $2, updated_at = NOW() "
                    "WHERE job_id = $1 AND status = 'queued'",
                    row["job_id"],
                    f"orphaned_dependency: parent {dep} was {dep_row['status']}",
                )
                logger.info("pipeline_runner.orphan_auto_cleaned",
                            job_id=row["job_id"], parent=dep, parent_status=dep_row["status"])
                continue
            if not dep_row or dep_row["status"] != "done":
                logger.debug("pipeline_runner.dep_not_ready",
                             job_id=row["job_id"], depends_on=dep,
                             dep_status=dep_row["status"] if dep_row else "not_found")
                continue  # 의존 작업 미완료 → 스킵
        logger.info("pipeline_runner.lock_released_next_ready",
                     next_job_id=row["job_id"], project=project)
        return row["job_id"]
    return None


def _runner_display_status(status: str, phase: str | None, error_detail: str | None) -> dict[str, object]:
    """UI가 terminal-but-not-error 상태를 빨간 실패로만 표시하지 않도록 분류한다."""
    phase = phase or ""
    error_detail = error_detail or ""
    candidates = (phase, status, error_detail.split(":", 1)[0])
    for candidate in candidates:
        if candidate in _DISPLAY_STATUS_LABELS:
            return {
                "display_status": candidate,
                "status_label": _DISPLAY_STATUS_LABELS[candidate],
                "status_group": _DISPLAY_STATUS_GROUPS[candidate],
                "auto_retryable": candidate == "tool_timeout",
            }
    if status == "cancelled":
        return {"display_status": "cancelled", "status_label": "종결",
                "status_group": "blocked", "auto_retryable": False}
    group = "active" if status in ("queued", "claimed", "running", "deploying", "rolling_back") else "unknown"
    if status == "awaiting_approval":
        group = "action_required"
    elif status in ("done", "approved", "rejected_done"):
        group = "complete"
    elif status in ("error", "rejected"):
        group = "action_required"
    return {"display_status": status, "status_label": status,
            "status_group": group, "auto_retryable": False}


async def _runner_health_probe(conn, row) -> dict | None:
    status = _record_get(row, "status") or ""
    if status not in ("running", "claimed"):
        return None
    job_id = _record_get(row, "job_id") or ""
    project = _record_get(row, "project") or ""
    logs_row = await conn.fetchrow(
        "SELECT EXISTS(SELECT 1 FROM task_logs WHERE task_id = $1 LIMIT 1) AS has_logs",
        job_id,
    )
    has_logs = bool(logs_row and logs_row["has_logs"])
    pid = _record_get(row, "runner_pid")
    proc_alive = None
    proc_scope = "no_pid"
    if pid and _is_local_runner_project(project):
        proc_alive = _local_pid_alive(pid)
        proc_scope = "local_proc"
    elif pid:
        proc_scope = "remote_proc_not_checked_by_api"

    reasons = []
    if not has_logs:
        reasons.append("empty_task_logs")
    if proc_alive is False:
        reasons.append("dead_local_pid")
    if not reasons:
        return None
    return {
        "task_logs": "present" if has_logs else "empty",
        "runner_pid": pid,
        "proc_alive": proc_alive,
        "proc_scope": proc_scope,
        "suspect_stale": proc_alive is False,
        "reasons": reasons,
        "systemd": "not_checked_by_api",
    }


async def _cleanup_dead_local_runner_processes(conn, project: str, min_age_seconds: int = 120) -> int:
    """Clear dead local runner rows before submit/dedup decisions.

    Only local projects are mutated here. Remote-project runner PIDs are owned
    by their runner host and must be cleaned by the remote watchdog.
    """
    if not _is_local_runner_project(project):
        return 0
    rows = await conn.fetch(
        """
        SELECT job_id, runner_pid
        FROM pipeline_jobs
        WHERE project = $1
          AND status IN ('running', 'claimed')
          AND runner_pid IS NOT NULL
          AND updated_at < NOW() - ($2::int * INTERVAL '1 second')
        """,
        project,
        min_age_seconds,
    )
    cleaned = 0
    for row in rows:
        pid = _record_get(row, "runner_pid")
        if _local_pid_alive(pid) is not False:
            continue
        result = await conn.execute(
            """
            UPDATE pipeline_jobs
            SET status = 'error',
                phase = 'error',
                error_detail = 'process_died',
                runner_pid = NULL,
                review_feedback = COALESCE(review_feedback, '') || $2,
                updated_at = NOW()
            WHERE job_id = $1
              AND status IN ('running', 'claimed')
            """,
            row["job_id"],
            f"\n[API stale guard] Local runner process PID={pid} is not alive; marked error before dedup/lock.",
        )
        if result and result != "UPDATE 0":
            cleaned += 1
            logger.warning(
                "pipeline_runner.local_dead_pid_cleaned",
                job_id=row["job_id"],
                runner_pid=pid,
            )
    return cleaned


@router.post("/pipeline/jobs", response_model=JobSubmitResponse, tags=["pipeline-runner"])
async def submit_job(
    req: JobSubmitRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """작업 제출 — 같은 프로젝트에 running 작업이 있으면 queued 대기, 없으면 즉시 running."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    session_id = req.session_id  # 필수 필드 — validator에서 이미 검증됨
    instruction_hash = _compute_instruction_hash(req.project, req.instruction)
    target_files = _extract_target_files(req.instruction)
    auto_depends_on = ""
    auto_dependency_reason = ""

    try:
        async with pool.acquire() as conn:
            # 트랜잭션으로 lock 체크 + INSERT 원자성 보장
            async with conn.transaction():
                tenant_id = _tenant_id(context)
                session_tenant = await conn.fetchval(
                    "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    session_id,
                    tenant_id,
                )
                if not session_tenant:
                    raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
                await _lock_instruction_hash(conn, instruction_hash)
                await _cleanup_dead_local_runner_processes(conn, req.project)
                # AADS-239: 중복 재사용 — 기존 작업 활용 (죽이기 → 재사용)
                # Step 1: 동일 hash + 활성 상태 → 기존 작업 정보 반환
                existing = await _find_active_duplicate(
                    conn,
                    req.project,
                    instruction_hash,
                    req.parallel_group,
                    tenant_id=tenant_id,
                )
                if existing:
                    detail = await _record_dedup_blocked(
                        conn,
                        job_id=job_id,
                        req=req,
                        instruction_hash=instruction_hash,
                        existing=existing,
                        tenant_id=tenant_id,
                    )
                    return JobSubmitResponse(
                        job_id=job_id,
                        status="dedup_blocked",
                        message=f"{detail}. 기존 작업을 계속 진행합니다.",
                    )
                # Step 2: 동일 hash + error + 2시간 내 → 기존 작업 queued로 리셋하여 재시도
                failed = await conn.fetchrow(
                    """
                    SELECT job_id FROM pipeline_jobs
                    WHERE instruction_hash = $1
                      AND tenant_id = $2::uuid
                      AND status = 'error'
                      AND created_at > NOW() - INTERVAL '2 hours'
                    ORDER BY created_at DESC LIMIT 1
                    FOR UPDATE
                    """,
                    instruction_hash,
                    tenant_id,
                )
                if failed:
                    await conn.execute(
                        "UPDATE pipeline_jobs SET status = 'queued', phase = 'queued', "
                        "error_detail = NULL, runner_pid = NULL, updated_at = NOW() "
                        "WHERE job_id = $1 AND tenant_id = $2::uuid",
                        failed["job_id"],
                        tenant_id,
                    )
                    await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", failed["job_id"])
                    return JobSubmitResponse(
                        job_id=failed["job_id"],
                        status="retrying",
                        message=f"이전 실패 작업을 재시도합니다: {failed['job_id']}",
                    )
                locked = await check_project_lock(conn, req.project, parallel_group=req.parallel_group, tenant_id=tenant_id)
                worker_model, worker_model_reason = _normalize_worker_model_override(
                    req.worker_model,
                    req.worker_model_reason,
                )
                # AADS-211: worker_model 직접 지정 시 size 무시
                if worker_model:
                    model = worker_model
                    size = req.size  # worker_model 지정 시에도 size 초기화
                else:
                    # AADS-206B: size 명시 시 우선, 기본값이면 instruction 파���
                    size = req.size
                    if size == "M":
                        parsed = _parse_size_from_instruction(req.instruction)
                        size = parsed or _estimate_size(req.instruction)
                    model = await _get_model_for_size(conn, size)
                # AADS-211: depends_on 유효성 검사
                if req.depends_on:
                    dep_row = await conn.fetchrow(
                        "SELECT job_id, status FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
                        req.depends_on,
                        tenant_id,
                    )
                    if not dep_row:
                        detail = await _record_blocked_dependency(
                            conn,
                            job_id=job_id,
                            req=req,
                            instruction_hash=instruction_hash,
                            dep_status="missing",
                            tenant_id=tenant_id,
                        )
                        return JobSubmitResponse(
                            job_id=job_id,
                            status="blocked_dependency",
                            message=detail,
                        )
                    # P1-B: 의존 작업이 이미 실패 상태이면 즉시 거부
                    if dep_row["status"] in _TERMINAL_BLOCKING_STATUSES:
                        detail = await _record_blocked_dependency(
                            conn,
                            job_id=job_id,
                            req=req,
                            instruction_hash=instruction_hash,
                            dep_status=dep_row["status"],
                            tenant_id=tenant_id,
                        )
                        return JobSubmitResponse(
                            job_id=job_id,
                            status="blocked_dependency",
                            message=detail,
                        )
                else:
                    conflict = await _find_active_file_conflict(
                        conn,
                        project=req.project,
                        target_files=target_files,
                        tenant_id=tenant_id,
                    )
                    if conflict:
                        auto_depends_on = conflict["job_id"]
                        overlap = ", ".join(conflict["overlap"])
                        auto_dependency_reason = (
                            f"[Runner Guard] 동일 파일 충돌 감지: {overlap}; "
                            f"{auto_depends_on} 완료 후 자동 실행"
                        )
                        logger.info(
                            "pipeline_runner.file_conflict_auto_dependency",
                            job_id=job_id,
                            project=req.project,
                            depends_on=auto_depends_on,
                            overlap=conflict["overlap"],
                        )
                effective_depends_on = req.depends_on or auto_depends_on or None
                await conn.execute(
                    """
                    INSERT INTO pipeline_jobs
                      (job_id, project, instruction, instruction_hash, chat_session_id,
                       status, phase, max_cycles, model, size,
                       worker_model, model_override_reason, parallel_group, depends_on,
                       review_feedback, logs, created_at, updated_at, tenant_id)
                    VALUES ($1, $2, $3, $4, $5, 'queued', 'queued', $6, $7, $8,
                            $9, $10, $11, $12::text,
                            $13::text,
                            CASE WHEN $13::text = '' THEN '[]'::jsonb ELSE jsonb_build_array(jsonb_build_object(
                              'ts', NOW()::text,
                              'event', 'file_conflict_auto_dependency',
                              'depends_on', $12::text
                            )) END,
                            NOW(), NOW(), $14::uuid)
                    """,
                    job_id, req.project, req.instruction, instruction_hash,
                    session_id, req.max_cycles, model, size,
                    worker_model or None, worker_model_reason or None,
                    req.parallel_group or None, effective_depends_on,
                    auto_dependency_reason,
                    tenant_id,
                )
                # P2-2: LISTEN/NOTIFY — 이벤트 드리븐 (asyncpg 소비자용)
                await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", job_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pipeline_runner.submit_fail", error=str(e))
        raise HTTPException(status_code=500, detail="작업 저장 실패")

    if locked:
        logger.info("pipeline_runner.job_queued_locked", job_id=job_id, project=req.project)
        msg = "프로젝트에 실행 중인 작업이 있어 대기열에 추가되었습니다. 현재 작업 완료 후 자동 실행됩니다."
    else:
        logger.info("pipeline_runner.job_submitted", job_id=job_id, project=req.project)
        msg = "작업이 대기열에 추가되었습니다. Runner가 곧 실행합니다."
    if auto_depends_on:
        msg += f" 동일 파일 충돌을 감지해 {auto_depends_on} 완료 후 실행되도록 자동 의존성을 부여했습니다."
    if req.worker_model and not req.worker_model_reason:
        msg += " 직접 모델 지정은 사유가 없어 저장하지 않았고, 어드민 러너 모델 설정값을 사용합니다."

    return JobSubmitResponse(job_id=job_id, status="queued", message=msg)


@router.get("/pipeline/jobs", tags=["pipeline-runner"])
async def list_jobs(
    status: Optional[str] = Query(None, max_length=30),
    project: Optional[str] = Query(None, max_length=10),
    session_id: Optional[str] = Query(None, max_length=36),
    limit: int = Query(20, ge=1, le=100),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """작업 목록 조회."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    conditions = ["tenant_id = $1::uuid"]
    params = [_tenant_id(context)]
    idx = 2

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if project:
        if project not in _VALID_PROJECTS:
            raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")
        conditions.append(f"project = ${idx}")
        params.append(project)
        idx += 1
    if session_id:
        conditions.append(f"chat_session_id = ${idx}")
        params.append(session_id)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT job_id, project, instruction, status, phase, cycle,
                   error_detail, created_at, updated_at,
                   started_at, depends_on, chat_session_id, model, worker_model,
                   actual_model, size, runner_pid
            FROM pipeline_jobs
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx}
            """,
            *params, limit,
        )

    results = []
    async with pool.acquire() as conn:
        for r in rows:
            item = {
                "job_id": r["job_id"],
                "project": r["project"],
                "instruction": r["instruction"][:200],
                "status": r["status"],
                "phase": r["phase"],
                "cycle": r["cycle"],
                "error_detail": _record_get(r, "error_detail"),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "started_at": r["started_at"].isoformat() if _record_get(r, "started_at") else None,
                "depends_on": _record_get(r, "depends_on"),
                "model": _record_get(r, "model") or "",
                "worker_model": _record_get(r, "worker_model") or "",
                "actual_model": _record_get(r, "actual_model") or "",
                "size": _record_get(r, "size") or "M",
                **_runner_display_status(r["status"], r["phase"], _record_get(r, "error_detail")),
            }
            health_probe = await _runner_health_probe(conn, r)
            if health_probe:
                item["health_probe"] = health_probe
            results.append(item)
    return results


@router.get("/pipeline/jobs/{job_id}", tags=["pipeline-runner"])
async def get_job(
    job_id: str,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """작업 상세 조회."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
            job_id,
            _tenant_id(context),
        )

    if not row:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    result = {
        "job_id": row["job_id"],
        "project": row["project"],
        "instruction": row["instruction"],
        "status": row["status"],
        "phase": row["phase"],
        "cycle": row["cycle"],
        "max_cycles": row["max_cycles"],
        "result_output": row["result_output"],
        "git_diff": (row["git_diff"] or "")[:5000],
        "review_feedback": row["review_feedback"],
        "error_detail": _record_get(row, "error_detail"),
        "model": _record_get(row, "model") or "",
        "worker_model": _record_get(row, "worker_model") or "",
        "actual_model": _record_get(row, "actual_model") or "",
        "size": _record_get(row, "size") or "M",
        **_runner_display_status(row["status"], row["phase"], _record_get(row, "error_detail")),
        "started_at": row["started_at"].isoformat() if _record_get(row, "started_at") else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
    async with pool.acquire() as conn:
        health_probe = await _runner_health_probe(conn, row)
    if health_probe:
        result["health_probe"] = health_probe
    return result


@router.post("/pipeline/jobs/{job_id}/notify", tags=["pipeline-runner"])
async def notify_completion(job_id: str):
    """Runner가 작업 완료 시 호출 — 채팅AI에 자동 반응 트리거."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id")

    # FIX-3: 터미널 상태 체크 — 이미 완료된 작업은 중복 처리 방지
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        terminal_row = await conn.fetchrow(
            "SELECT status FROM pipeline_jobs WHERE job_id = $1", job_id
        )
    if not terminal_row or terminal_row["status"] in ("done", "rejected_done", "error", "cancelled"):
        return {
            "status": "skipped",
            "reason": terminal_row["status"] if terminal_row else "not_found",
        }

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT job_id, project, status, phase, chat_session_id, error_detail, "
            "substring(result_output from 1 for 500) as output_preview, "
            "substring(instruction from 1 for 200) as instruction_preview "
            "FROM pipeline_jobs WHERE job_id = $1", job_id
        )

    if not row:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    status = row["status"]
    project = row["project"]

    # 작업 완료/에러 시 같은 프로젝트의 다음 queued 작업을 자동 승격
    promoted_job_id = None
    if status in ("done", "error", "rejected", "rejected_done"):
        try:
            async with pool.acquire() as conn:
                # P1-A: 실패 시 재귀 고아 정리 후 승격
                if status in ("error", "rejected", "rejected_done"):
                    await cascade_cleanup_orphans(conn, job_id)
                promoted_job_id = await promote_next_queued(conn, project)
        except Exception as e:
            logger.warning("pipeline_runner.promote_fail", project=project, error=str(e))

    session_id = row["chat_session_id"]
    if not session_id or not _UUID_RE.match(session_id):
        return {"status": "skipped", "reason": "session_id 없음", "promoted_job_id": promoted_job_id}
    instruction = row["instruction_preview"] or ""
    output = row["output_preview"] or ""

    if status == "awaiting_approval":
        async with pool.acquire() as conn:
            notify_claimed = await conn.fetchrow(
                """
                UPDATE pipeline_jobs
                SET logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(
                    jsonb_build_object(
                        'ts', NOW()::text,
                        'event', 'notify_ai',
                        'status', 'awaiting_approval',
                        'source', 'pipeline_notify'
                    )
                )
                WHERE job_id = $1
                  AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(COALESCE(logs, '[]'::jsonb)) AS log
                    WHERE log->>'event' = 'notify_ai'
                      AND log->>'status' = 'awaiting_approval'
                  )
                RETURNING job_id
                """,
                job_id,
            )
        if not notify_claimed:
            return {
                "status": "skipped",
                "reason": "awaiting_approval already notified",
                "session_id": session_id,
                "promoted_job_id": promoted_job_id,
            }
        msg = (f"[시스템] Pipeline Runner 작업 AI 검수 요청\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**작업**: {instruction}\n**결과 미리보기**:\n{output[:300]}\n\n"
               f"**검수 지시 (반드시 도구 호출로 완료):**\n"
               f"1. read_remote_file로 수정된 파일을 직접 확인하세요.\n"
               f"2. 검수 완료 후 반드시 아래 중 하나를 실행하세요:\n"
               f"   - 이상 없음: pipeline_runner_approve(job_id='{job_id}', action='approve') 호출\n"
               f"   - 문제 있음: pipeline_runner_approve(job_id='{job_id}', action='reject', feedback='구체적 사유') 호출\n"
               f"   - 수정 재지시: reject 후 pipeline_runner_submit으로 수정 지시 제출\n"
               f"도구 호출 없이 보고 금지. 반드시 승인 또는 거부 도구를 실행하세요.")
    elif status == "done":
        msg = (f"[시스템] Pipeline Runner 작업 배포 완료\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**결과**:\n{output[:300]}\n\n"
               f"**배포 검증 5단계 필수 수행:**\n"
               f"1. 컨테이너 상태 확인 (docker ps로 healthy 확인)\n"
               f"2. 변경 파일 반영 확인 (read_remote_file로 핵심 수정 라인 확인)\n"
               f"3. API 헬스체크 (health_check 또는 curl)\n"
               f"4. DB 데이터 정합성 (query_database로 관련 수치 실측 확인)\n"
               f"5. 프론트엔드 변경 시 UI 확인 (browser_snapshot 또는 capture_screenshot)\n"
               f"각 단계를 도구로 실제 확인한 후 결과를 CEO에게 보고하세요. 도구 호출 없이 '정상 완료' 보고 금지.")
    elif status == "error":
        error_detail = _record_get(row, "error_detail") or "unknown"
        msg = (f"[시스템] Pipeline Runner 작업 실패\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**에러 분류**: {error_detail}\n"
               f"**에러**:\n{output[:300]}\n\n"
               f"원인을 진단하고 조치하세요.")
    else:
        msg = f"[시스템] Pipeline Runner 작업 상태 변경: {job_id} → {status}"

    try:
        from app.services.chat_service import trigger_ai_reaction
        import asyncio
        logger.info("pipeline_runner.trigger_sent", job_id=job_id, session_id=session_id, status=status)
        asyncio.create_task(trigger_ai_reaction(session_id, msg))
        return {"status": "triggered", "session_id": session_id, "promoted_job_id": promoted_job_id}
    except Exception as e:
        logger.warning(f"notify_trigger_failed: {e}")
        return {"status": "error", "detail": str(e), "promoted_job_id": promoted_job_id}


@router.post("/pipeline/jobs/{job_id}/approve", tags=["pipeline-runner"])
async def approve_or_reject(
    job_id: str,
    req: JobApproveRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """작업 승인/거부 — Runner가 감지하여 배포 또는 롤백."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE pipeline_jobs
            SET status = $2,
                review_feedback = COALESCE(review_feedback, '') || E'\n[CEO] ' || $3,
                updated_at = NOW()
            WHERE job_id = $1 AND tenant_id = $4::uuid AND status = 'awaiting_approval'
            """,
            job_id,
            "approved" if req.action == "approve" else "rejected",
            req.feedback or req.action,
            _tenant_id(context),
        )

    affected = int(result.split()[-1]) if result else 0
    if affected == 0:
        raise HTTPException(status_code=400, detail="승인 대기 상태가 아닙니다")

    action_kr = "승인됨" if req.action == "approve" else "거부됨"
    logger.info("pipeline_runner.job_action", job_id=job_id, action=req.action)

    # autonomy_stats 기록 (자율성 데이터 축적)
    try:
        from app.services.autonomy_gate import record_task_result
        async with pool.acquire() as conn:
            job_row = await conn.fetchrow(
                "SELECT project FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
                job_id,
                _tenant_id(context),
            )
            if job_row:
                if req.action == "approve":
                    await record_task_result(
                        conn,
                        task_type="pipeline_runner",
                        task_id=job_id,
                        judge_verdict="pass",
                        user_modified=False,
                        project_id=job_row["project"],
                    )
                else:
                    await record_task_result(
                        conn,
                        task_type="pipeline_runner",
                        task_id=job_id,
                        judge_verdict="fail",
                        user_modified=True,
                        project_id=job_row["project"],
                    )
    except Exception as e:
        if req.action == "approve":
            logger.warning(f"autonomy_record_on_approve_failed: {e}")
        else:
            logger.warning(f"autonomy_record_on_reject_failed: {e}")

    return {"job_id": job_id, "action": req.action, "message": f"작업이 {action_kr}"}


# ─── AADS-211: 배치 제출 — 복수 작업을 의존성 그래프로 한번에 제출 ────────────

class BatchJobItem(BaseModel):
    """배치 내 개별 작업 정의."""
    key: str = Field(..., description="배치 내 작업 식별자 (예: 'A', 'B', 'C')")
    instruction: str = Field(..., max_length=50000)
    size: str = Field("M")
    worker_model: str = Field("")
    worker_model_reason: str = Field("", max_length=500)
    depends_on_key: str = Field("", description="이 배치 내 다른 작업의 key (자동으로 job_id 매핑)")


class BatchSubmitRequest(BaseModel):
    project: str = Field(...)
    session_id: str = Field(...)
    jobs: list[BatchJobItem] = Field(..., min_length=1, max_length=20)
    parallel_group: str = Field("", description="전체 배치에 적용할 병렬 그룹")
    max_cycles: int = Field(3, ge=1, le=10)

    @field_validator('project')
    @classmethod
    def validate_project(cls, v):
        if v not in _VALID_PROJECTS:
            raise ValueError(f"허용 프로젝트: {', '.join(sorted(_VALID_PROJECTS))}")
        return v

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v):
        if not v or not _UUID_RE.match(v):
            raise ValueError("session_id는 필수이며 UUID 형식이어야 합니다")
        return v


@router.post("/pipeline/jobs/batch", tags=["pipeline-runner"])
async def submit_batch(
    req: BatchSubmitRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """복수 작업을 의존성 그래프로 한번에 제출.
    AADS-211: 채팅 AI(오케스트레이터)가 작업을 쪼갠 뒤 호출."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    # 자동 parallel_group 생성 (미지정 시)
    pg = req.parallel_group or f"batch-{uuid.uuid4().hex[:8]}"

    # key → job_id 매핑 테이블
    key_to_job_id: dict[str, str] = {}
    for item in req.jobs:
        key_to_job_id[item.key] = f"runner-{uuid.uuid4().hex[:8]}"

    results = []
    batch_file_owner: dict[str, str] = {}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                tenant_id = _tenant_id(context)
                session_tenant = await conn.fetchval(
                    "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    req.session_id,
                    tenant_id,
                )
                if not session_tenant:
                    raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
                for item in req.jobs:
                    job_id = key_to_job_id[item.key]
                    depends_on = key_to_job_id.get(item.depends_on_key) if item.depends_on_key else None
                    item_target_files = _extract_target_files(item.instruction)
                    auto_dependency_reason = ""

                    worker_model, worker_model_reason = _normalize_worker_model_override(
                        item.worker_model,
                        item.worker_model_reason,
                    )
                    if worker_model:
                        model = worker_model
                        size = item.size
                    else:
                        size = item.size
                        if size == "M":
                            parsed = _parse_size_from_instruction(item.instruction)
                            size = parsed or _estimate_size(item.instruction)
                        model = await _get_model_for_size(conn, size)

                    instruction_hash = _compute_instruction_hash(req.project, item.instruction)
                    await _lock_instruction_hash(conn, instruction_hash)

                    # AADS-239: 멱등성 체크 (submit_job과 동일 로직)
                    # Step 1: 동일 hash + 동일 parallel_group 활성 상태 → blocked 기록
                    existing = await _find_active_duplicate(conn, req.project, instruction_hash, pg, tenant_id=tenant_id)
                    if existing:
                        detail = (
                            f"dedup_blocked: existing job {existing['job_id']} "
                            f"is {existing['status']}/{existing['phase']}"
                        )
                        await conn.execute(
                            """
                            INSERT INTO pipeline_jobs
                              (job_id, project, instruction, instruction_hash, chat_session_id,
                               status, phase, max_cycles, model, size, worker_model,
                               model_override_reason, parallel_group, depends_on,
                               error_detail, review_feedback, logs, created_at, updated_at, tenant_id)
                            VALUES ($1, $2, $3, $4, $5,
                                    'cancelled', 'dedup_blocked', $6, $7, $8, $9,
                                    $10, $11, $12,
                                    $13, $14,
                                    jsonb_build_array(jsonb_build_object(
                                      'ts', NOW()::text,
                                      'event', 'dedup_blocked',
                                      'existing_job_id', $15,
                                      'parallel_scope', $16,
                                      'auto_retryable', false
                                    )),
                                    NOW(), NOW(), $17::uuid)
                            """,
                            job_id,
                            req.project,
                            item.instruction,
                            instruction_hash,
                            req.session_id,
                            req.max_cycles,
                            model,
                            size,
                            worker_model or None,
                            worker_model_reason or None,
                            pg,
                            depends_on,
                            detail,
                            f"[Runner Guard] {detail}; auto_retryable=false",
                            existing["job_id"],
                            _parallel_scope(pg),
                            tenant_id,
                        )
                        results.append({
                            "key": item.key,
                            "job_id": job_id,
                            "model": model,
                            "depends_on": depends_on,
                            "skipped": True,
                            "status": "dedup_blocked",
                            "reason": detail,
                        })
                        continue

                    # Step 2: 동일 hash + error + 2시간 내 → queued 리셋 후 재시도
                    failed = await conn.fetchrow(
                        """
                        SELECT job_id FROM pipeline_jobs
                        WHERE instruction_hash = $1
                          AND status = 'error'
                          AND tenant_id = $2::uuid
                          AND created_at > NOW() - INTERVAL '2 hours'
                        ORDER BY created_at DESC LIMIT 1
                        FOR UPDATE
                        """,
                        instruction_hash,
                        tenant_id,
                    )
                    if failed:
                        await conn.execute(
                            "UPDATE pipeline_jobs SET status = 'queued', phase = 'queued', "
                            "error_detail = NULL, runner_pid = NULL, updated_at = NOW() "
                            "WHERE job_id = $1 AND tenant_id = $2::uuid",
                            failed["job_id"],
                            tenant_id,
                        )
                        key_to_job_id[item.key] = failed["job_id"]
                        await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", failed["job_id"])
                        results.append({
                            "key": item.key,
                            "job_id": failed["job_id"],
                            "model": model,
                            "depends_on": depends_on,
                            "retrying": True,
                        })
                        continue

                    if not depends_on:
                        internal_conflicts = sorted(
                            path for path in item_target_files if path in batch_file_owner
                        )
                        if internal_conflicts:
                            depends_on = batch_file_owner[internal_conflicts[0]]
                            auto_dependency_reason = (
                                "[Runner Guard] 배치 내 동일 파일 충돌 감지: "
                                f"{', '.join(internal_conflicts)}; {depends_on} 완료 후 자동 실행"
                            )
                        else:
                            conflict = await _find_active_file_conflict(
                                conn,
                                project=req.project,
                                target_files=item_target_files,
                                tenant_id=tenant_id,
                                ignore_job_ids=set(key_to_job_id.values()),
                            )
                            if conflict:
                                depends_on = conflict["job_id"]
                                auto_dependency_reason = (
                                    "[Runner Guard] 활성 작업과 동일 파일 충돌 감지: "
                                    f"{', '.join(conflict['overlap'])}; {depends_on} 완료 후 자동 실행"
                                )

                    await conn.execute(
                        """
                        INSERT INTO pipeline_jobs
                          (job_id, project, instruction, instruction_hash, chat_session_id,
                           status, phase, max_cycles, model, size,
                           worker_model, model_override_reason, parallel_group, depends_on,
                           review_feedback, logs, created_at, updated_at, tenant_id)
                        VALUES ($1, $2, $3, $4, $5, 'queued', 'queued', $6, $7, $8,
                                $9, $10, $11, $12::text,
                                $13::text,
                                CASE WHEN $13::text = '' THEN '[]'::jsonb ELSE jsonb_build_array(jsonb_build_object(
                                  'ts', NOW()::text,
                                  'event', 'file_conflict_auto_dependency',
                                  'depends_on', $12::text
                                )) END,
                                NOW(), NOW(), $14::uuid)
                        """,
                        job_id, req.project, item.instruction, instruction_hash,
                        req.session_id, req.max_cycles, model, size,
                        worker_model or None, worker_model_reason or None, pg, depends_on,
                        auto_dependency_reason,
                        tenant_id,
                    )
                    # P2-2: LISTEN/NOTIFY
                    await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", job_id)

                    for path in item_target_files:
                        batch_file_owner.setdefault(path, job_id)

                    results.append({
                        "key": item.key,
                        "job_id": job_id,
                        "model": model,
                        "depends_on": depends_on,
                        "auto_dependency": bool(auto_dependency_reason),
                        "target_files": sorted(item_target_files),
                    })

    except HTTPException:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pipeline_runner.batch_submit_fail", error=str(e))
        raise HTTPException(status_code=500, detail="배치 저장 실패")

    logger.info("pipeline_runner.batch_submitted",
                 project=req.project, count=len(results), parallel_group=pg)

    return {
        "parallel_group": pg,
        "jobs": results,
        "message": f"{len(results)}개 작업이 제출되었습니다. 의존성에 따라 순차/병렬 실행됩니다.",
    }


@router.get("/pipeline/lock-status", tags=["pipeline-runner"])
async def lock_status(
    project: str = Query(..., max_length=10),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """프로젝트별 동시실행 Lock 상태 조회. Shell runner가 claim 전 호출."""
    if project not in _VALID_PROJECTS:
        raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        running_row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed')",
            project,
            _tenant_id(context),
        )
        locked = await check_project_lock(conn, project, tenant_id=_tenant_id(context))
        queued_row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status = 'queued'",
            project,
            _tenant_id(context),
        )

    return {
        "project": project,
        "locked": locked,
        "running_count": running_row["cnt"] if running_row else 0,
        "max_concurrent_per_project": _max_concurrent_per_project(),
        "queued_count": queued_row["cnt"] if queued_row else 0,
    }


# ── Runner Model Config (AADS-241) ──────────────────────────────────

class _RunnerModelItem(BaseModel):
    """size별 모델 우선순위."""
    size: str = Field(..., pattern=r"^(XS|S|M|L|XL|AI_REVIEW)$")
    models: list[str] = Field(..., min_length=1)


class _RunnerModelConfigUpdate(BaseModel):
    """CEO 대시보드에서 러너 모델 설정 업데이트."""
    configs: list[_RunnerModelItem]


@router.get("/settings/runner-models")
async def get_runner_model_config():
    """size별 러너 모델 우선순위 조회."""
    import json as _json_get
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT size, models, updated_at, updated_by "
            "FROM runner_model_config ORDER BY size"
        )
    configs = []
    for r in rows:
        # asyncpg JSONB → str일 수 있으므로 안전하게 파싱
        raw = r["models"]
        if isinstance(raw, str):
            models = _json_get.loads(raw)
        elif isinstance(raw, list):
            models = raw
        else:
            models = list(raw) if raw else []
        configs.append({
            "size": r["size"],
            "models": models,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "updated_by": r["updated_by"],
        })
    return {"configs": configs}


@router.put("/settings/runner-models")
async def update_runner_model_config(req: _RunnerModelConfigUpdate):
    """size별 러너 모델 우선순위 업데이트. CEO 대시보드에서 호출."""
    import json as _json
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in req.configs:
                await conn.execute(
                    "INSERT INTO runner_model_config (size, models, updated_at, updated_by) "
                    "VALUES ($1, $2::jsonb, NOW(), 'CEO') "
                    "ON CONFLICT (size) DO UPDATE "
                    "SET models = EXCLUDED.models, updated_at = NOW(), updated_by = 'CEO'",
                    item.size.upper(),
                    _json.dumps(item.models),
                )
    logger.info("runner_model_config_updated", count=len(req.configs))
    return {"status": "ok", "message": f"{len(req.configs)}개 size 모델 설정 업데이트 완료"}
