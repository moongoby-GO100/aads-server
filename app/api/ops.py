"""
AADS-113: 운영 통합 DB API 엔드포인트
AADS-116: 유지보수 모드 API
AADS-166: 파이프라인 전체 헬스체크 + SSE 스트리밍
"""
import os
import json
import asyncio
import hashlib
import re
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Any, Dict
import structlog
import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
import asyncpg
from app.auth import require_internal_admin
from app.core.claude_md_merger import build_merged_claude_md, get_merged_claude_md_sha256
from app.core.project_config import normalize_project_label
from app.services.server_registry import get_server_config, get_server_host, resolve_server_id

logger = structlog.get_logger()
router = APIRouter()

# 서버 시작 시 고유 빌드 해시 생성 (컨테이너 재시작 = 새 해시)
_BUILD_HASH = hashlib.md5(f"{os.getpid()}-{_time.time()}".encode()).hexdigest()[:12]

_CLAUDE_RELAY_URL = os.getenv("CLAUDE_RELAY_URL", "http://host.docker.internal:8199").rstrip("/")
_RELAY_SECRET_PATHS = (
    Path(os.getenv("CLAUDE_RELAY_SHARED_SECRET_FILE", "/app/scripts/claude_relay_secret.txt")),
    Path("/root/aads/aads-server/scripts/claude_relay_secret.txt"),
)


def _load_relay_secret() -> str:
    secret = (os.getenv("CLAUDE_RELAY_SHARED_SECRET") or "").strip()
    if secret:
        return secret
    for path in _RELAY_SECRET_PATHS:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if value:
            return value
    return ""


def _normalize_codex_limit_window(window: Dict[str, Any]) -> Dict[str, Any]:
    resets_at_epoch = window.get("resets_at_epoch")
    resets_in_sec = window.get("resets_in_sec")
    resets_at_iso = window.get("resets_at_iso")
    if resets_in_sec is None and resets_at_epoch:
        try:
            resets_at = datetime.fromtimestamp(float(resets_at_epoch), tz=timezone.utc)
            resets_in_sec = max(0, int((resets_at - datetime.now(timezone.utc)).total_seconds()))
            resets_at_iso = resets_at.isoformat()
        except Exception:
            resets_in_sec = None
    return {
        "used_percent": window.get("used_percent"),
        "window_minutes": window.get("window_minutes"),
        "resets_in_sec": resets_in_sec,
        "resets_at_iso": resets_at_iso,
    }


# 화면에 띄울 Codex 한도 버킷. 주 한도만 본다.
#
# Codex 는 limit_id 를 여러 개 내려준다. codex_bengalfox 는 같은 계정의 별도
# 집계일 뿐 대체 용량이 아니다 — 주 한도가 차면 CLI 가 통째로 거부한다
# ("You've hit your usage limit"). 2026-09-12 에 bengalfox=0%, codex=100%
# 상태로 직접 호출해 확인했고 model_selector.py 도 같은 이유로 주 한도만 본다.
#
# 그런데 채팅 상단 사용량 표시는 두 버킷을 나란히 그려, 주 한도가 98%인데
# 옆에 0% 가 보였다. 쓸 수 있는 여유가 있는 것처럼 읽혀 오판을 부른다.
# 판정에 쓰지 않는 값은 화면에도 올리지 않는다.
_CODEX_PRIMARY_LIMIT_IDS = ("codex", "codex_cli", "primary")


def _normalize_codex_usage_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_limits = [i for i in (payload.get("limits") or []) if isinstance(i, dict)]
    primary_only = [
        i for i in raw_limits
        if str(i.get("limit_id") or "").strip().lower() in _CODEX_PRIMARY_LIMIT_IDS
    ]
    # 주 한도를 못 찾으면 첫 버킷을 남긴다 — 표시가 통째로 비면 더 나쁘다.
    source_limits = primary_only or raw_limits[:1]

    limits = []
    for item in source_limits:
        limits.append({
            "limit_id": item.get("limit_id"),
            "plan_type": item.get("plan_type") or payload.get("plan_type") or payload.get("raw_plan_type"),
            "primary": _normalize_codex_limit_window(item.get("primary") or {}),
            "secondary": _normalize_codex_limit_window(item.get("secondary") or {}),
            "credits": item.get("credits") or {},
        })
    return {
        "ok": bool(payload.get("ok")) and not payload.get("error"),
        "cached": payload.get("cached"),
        "ttl_sec": payload.get("ttl_sec"),
        "fetched_at": payload.get("fetched_at"),
        "plan_type": payload.get("plan_type") or payload.get("raw_plan_type"),
        "limits": limits,
        "error": payload.get("error"),
        "detail": payload.get("detail"),
        "parse_warning": payload.get("parse_warning"),
    }


def _codex_usage_fallback(reason: str, detail: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ok": True,
        "fallback": True,
        "fallback_reason": reason,
        "detail": detail,
        "plan_type": "codex_cli",
        "limits": [
            {
                "limit_id": "codex",
                "plan_type": "codex_cli",
                "primary": {
                    "used_percent": 0,
                    "window_minutes": 300,
                    "resets_in_sec": 5 * 60 * 60,
                    "resets_at_iso": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
                },
                "secondary": {
                    "used_percent": 0,
                    "window_minutes": 7 * 24 * 60,
                    "resets_in_sec": 7 * 24 * 60 * 60,
                    "resets_at_iso": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                },
                "credits": {},
            }
        ],
    }


def _short_session_id(session_id: Any) -> str:
    return str(session_id)[:8]


async def _get_stream_activity_snapshot(recent_minutes: int = 5) -> Dict[str, Any]:
    from app.services.chat_service import get_active_bg_tasks

    local_owner = (
        os.getenv("AADS_CONTAINER_NAME")
        or os.getenv("HOSTNAME")
        or ""
    ).strip()
    active_map = get_active_bg_tasks()
    raw_executing_session_ids = [str(sid) for sid, active in active_map.items() if active]
    raw_executing_set = set(raw_executing_session_ids)
    db_running_session_ids: List[str] = []
    db_local_session_ids: List[str] = []
    placeholder_session_ids: List[str] = []
    placeholder_sessions: List[str] = []

    try:
        from app.core.db_pool import get_pool as _gp

        async with _gp().acquire() as conn:
            running_rows = await conn.fetch(
                """
                SELECT DISTINCT session_id::text AS session_id
                FROM chat_turn_executions
                WHERE status IN ('running', 'retrying')
                  AND completed_at IS NULL
                """
            )
            local_rows = await conn.fetch(
                """
                SELECT DISTINCT session_id::text AS session_id
                FROM chat_turn_executions
                WHERE status IN ('running', 'retrying')
                  AND completed_at IS NULL
                  AND owner_instance = $1
                  AND (
                      lease_expires_at IS NULL
                      OR lease_expires_at > NOW()
                      OR heartbeat_at > NOW() - INTERVAL '60 seconds'
                  )
                  AND NOT (
                      COALESCE(error_message, '') = 'recovery_auto_retry_scheduled'
                      AND EXISTS (
                          SELECT 1
                          FROM chat_messages ph
                          WHERE ph.execution_id = chat_turn_executions.id
                            AND ph.intent = 'streaming_placeholder'
                            AND COALESCE(ph.is_hidden, FALSE) = TRUE
                      )
                  )
                """,
                local_owner,
            )
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (session_id)
                       session_id::text AS session_id
                FROM chat_messages
                WHERE intent = 'streaming_placeholder'
                  AND created_at > NOW() - make_interval(mins => $1)
                ORDER BY session_id, created_at DESC
                """,
                recent_minutes,
            )
        db_running_session_ids = [row["session_id"] for row in running_rows]
        db_local_session_ids = [row["session_id"] for row in local_rows]
        placeholder_session_ids = [row["session_id"] for row in rows]
        placeholder_sessions = [_short_session_id(row["session_id"]) for row in rows]
    except Exception as e:
        logger.warning("stream_activity_snapshot_failed", error=str(e))

    db_running_set = set(db_running_session_ids)
    db_local_set = set(db_local_session_ids)
    placeholder_set = set(placeholder_session_ids)
    # Active task maps can retain a live asyncio task after DB finalization.
    # Count it for deploy drain only when the DB still has an active turn or
    # the UI still has a recent placeholder tied to the session.  In blue/green
    # mode the drain endpoint must be slot-local: DB rows owned by the peer slot
    # must not make this slot look busy and block same-digest standby sync.
    executing_set = raw_executing_set & db_local_set
    if not local_owner:
        executing_set = raw_executing_set & (db_running_set | placeholder_set)
    executing_session_ids = sorted(executing_set)
    executing_sessions = [_short_session_id(sid) for sid in executing_session_ids]
    recovery_pending_ids = [sid for sid in placeholder_session_ids if sid not in executing_set]
    visible_ids = list(executing_set | placeholder_set)

    return {
        "owner_instance": local_owner,
        "executing_count": len(executing_session_ids),
        "executing_sessions": executing_sessions,
        "raw_executing_count": len(raw_executing_session_ids),
        "raw_executing_sessions": [_short_session_id(sid) for sid in raw_executing_session_ids],
        "db_running_count": len(db_running_session_ids),
        "db_running_sessions": [_short_session_id(sid) for sid in db_running_session_ids],
        "db_local_running_count": len(db_local_session_ids),
        "db_local_running_sessions": [_short_session_id(sid) for sid in db_local_session_ids],
        "placeholder_recent_count": len(placeholder_session_ids),
        "placeholder_recent_sessions": placeholder_sessions,
        "recovery_pending_count": len(recovery_pending_ids),
        "recovery_pending_sessions": [_short_session_id(sid) for sid in recovery_pending_ids],
        "visible_count": len(visible_ids),
        "visible_sessions": [_short_session_id(sid) for sid in visible_ids],
        "window_minutes": recent_minutes,
    }


@router.get("/ops/streaming-metrics")
async def get_streaming_metrics():
    """스트리밍 운영 메트릭 조회."""
    try:
        from app.core.db_pool import get_pool as _gp

        async with _gp().acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH active AS (
                    SELECT COUNT(*)::int AS active_streaming_count,
                           COALESCE(
                               AVG(EXTRACT(EPOCH FROM (NOW() - started_at))),
                               0
                           ) AS avg_streaming_duration_sec
                    FROM chat_turn_executions
                    WHERE status IN ('running', 'retrying')
                      AND completed_at IS NULL
                ),
                recent AS (
                    SELECT COUNT(*) FILTER (WHERE status = 'completed')::int AS completed_count,
                           COUNT(*) FILTER (WHERE status = 'interrupted')::int AS interrupted_count
                    FROM chat_turn_executions
                    WHERE status IN ('completed', 'interrupted')
                      AND COALESCE(completed_at, updated_at, started_at) >= NOW() - INTERVAL '1 hour'
                )
                SELECT active.active_streaming_count,
                       ROUND(active.avg_streaming_duration_sec::numeric, 1) AS avg_streaming_duration_sec,
                       recent.completed_count,
                       recent.interrupted_count,
                       CASE
                           WHEN (recent.completed_count + recent.interrupted_count) > 0
                           THEN ROUND(
                               recent.completed_count::numeric * 100.0
                               / (recent.completed_count + recent.interrupted_count),
                               1
                           )
                           ELSE 0
                       END AS completed_ratio_pct,
                       CASE
                           WHEN (recent.completed_count + recent.interrupted_count) > 0
                           THEN ROUND(
                               recent.interrupted_count::numeric * 100.0
                               / (recent.completed_count + recent.interrupted_count),
                               1
                           )
                           ELSE 0
                       END AS interrupted_ratio_pct
                FROM active
                CROSS JOIN recent
                """
            )
    except Exception as e:
        logger.warning("streaming_metrics_failed", error=str(e))
        raise HTTPException(status_code=500, detail="failed_to_load_streaming_metrics")

    return {
        "active_streaming_count": int(row["active_streaming_count"] or 0),
        "average_streaming_duration_sec": float(row["avg_streaming_duration_sec"] or 0),
        "recent_one_hour": {
            "completed_count": int(row["completed_count"] or 0),
            "interrupted_count": int(row["interrupted_count"] or 0),
            "completed_ratio_pct": float(row["completed_ratio_pct"] or 0),
            "interrupted_ratio_pct": float(row["interrupted_ratio_pct"] or 0),
        },
        "window_minutes": 60,
        "generated_at": datetime.now(KST).isoformat(),
    }


@router.get("/version")
@router.get("/ops/version")
async def get_version():
    """배포 버전 해시 반환 — 프론트엔드 자동 새로고침용"""
    import subprocess as _sp_ver
    try:
        _git_sha = _sp_ver.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="/app", stderr=_sp_ver.DEVNULL
        ).decode().strip()
    except Exception:
        _git_sha = "unknown"
    return {"build_hash": _BUILD_HASH, "git_sha": _git_sha, "timestamp": _time.time()}


def _normalize_etag(value: str) -> str:
    normalized = (value or "").strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    return normalized.strip('"')


def _etag_matches(if_none_match: Optional[str], etag: str) -> bool:
    if not if_none_match:
        return False
    current = _normalize_etag(etag)
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if _normalize_etag(candidate) == current:
            return True
    return False


@router.get("/ops/claude-md")
async def get_merged_claude_md(request: Request, project: str = Query("AADS")):
    """분산 규칙 문서를 합쳐 text/markdown으로 반환."""
    content = await build_merged_claude_md(project=project)
    sha256 = get_merged_claude_md_sha256(project=project) or hashlib.sha256(content.encode("utf-8")).hexdigest()
    etag = f'"{sha256[:16]}"'

    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag})

    return Response(
        content=content,
        media_type="text/markdown",
        headers={"ETag": etag},
    )


# ─── 3단계 잠금 시스템 API ──────────────────────────────────────

@router.get("/ops/locks")
async def get_lock_status():
    """전체 프로젝트 잠금 상태 조회 — CEO 대시보드용."""
    from app.services.deploy_lock import get_all_lock_status
    return get_all_lock_status()


@router.post("/ops/locks/work/acquire")
async def api_acquire_work_lock(project: str, session_id: str, scope: str = ""):
    """프로젝트 작업 잠금 획득."""
    from app.services.deploy_lock import acquire_work_lock
    return acquire_work_lock(project, session_id, scope=scope)


@router.post("/ops/locks/work/release")
async def api_release_work_lock(project: str, session_id: str, scope: str = ""):
    """프로젝트 작업 잠금 해제."""
    from app.services.deploy_lock import release_work_lock
    return {"released": release_work_lock(project, session_id, scope=scope)}


@router.post("/ops/locks/file/acquire")
async def api_acquire_file_lock(project: str, file_path: str, session_id: str):
    """파일 잠금 획득."""
    from app.services.deploy_lock import acquire_file_lock
    return acquire_file_lock(project, file_path, session_id)


@router.post("/ops/locks/file/release")
async def api_release_file_lock(project: str, file_path: str, session_id: str):
    """파일 잠금 해제."""
    from app.services.deploy_lock import release_file_lock
    return {"released": release_file_lock(project, file_path, session_id)}


@router.post("/ops/locks/deploy/acquire")
async def api_acquire_deploy_lock(project: str, session_id: str):
    """배포 잠금 획득."""
    from app.services.deploy_lock import acquire_deploy_lock
    return acquire_deploy_lock(project, session_id)


@router.post("/ops/locks/deploy/release")
async def api_release_deploy_lock(project: str, session_id: str):
    """배포 잠금 해제."""
    from app.services.deploy_lock import release_deploy_lock
    return {"released": release_deploy_lock(project, session_id)}


@router.get("/ops/active-work/{project}")
async def get_active_work(project: str):
    """프로젝트 활성 작업(Runner + Chat-Direct + Lock) 통합 조회."""
    from app.services.deploy_lock import get_project_active_work
    return await get_project_active_work(project)


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads2026secure@aads-postgres:5432/aads"
)

KST = timezone(timedelta(hours=9))


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL, timeout=10)


class DeployQueueRequest(BaseModel):
    project: str = "AADS"
    release_sha: Optional[str] = None
    component: str = "api"
    deploy_type: Optional[str] = None
    target_env: str = "production"
    runner_job_id: Optional[str] = None
    requested_by: Optional[str] = None
    request_source: str = "ops_api"
    commit_status: str = "committed"
    push_status: str = "pushed"
    auto_start: bool = True
    rollback_plan: Optional[str] = None
    approval_policy: str = "auto_if_green"
    metadata: Dict[str, Any] = Field(default_factory=dict)


async def _start_aads_deploy_queue_worker(trigger: str) -> Dict[str, Any]:
    """Best-effort queue worker kick that returns quickly to the chat/API caller."""
    launcher_candidates = (
        Path("/root/aads/aads-server/scripts/start_aads_deploy_queue_worker.sh"),
        Path("/app/scripts/start_aads_deploy_queue_worker.sh"),
    )
    launcher = next((path for path in launcher_candidates if path.exists()), None)
    if launcher is None:
        return {
            "started": False,
            "status": "launcher_unavailable",
            "detail": "start_aads_deploy_queue_worker.sh not found",
        }

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            str(launcher),
            "bluegreen",
            trigger,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
    except asyncio.TimeoutError:
        return {
            "started": True,
            "status": "start_timeout_assumed_background",
            "detail": "worker launcher did not return within 8s; check /ops/deploy/status",
        }
    except Exception as exc:
        logger.warning("ops_deploy_worker_start_failed", error=str(exc))
        return {
            "started": False,
            "status": "start_failed",
            "detail": str(exc)[:300],
        }

    output = (stdout or b"").decode("utf-8", errors="replace").strip()
    error = (stderr or b"").decode("utf-8", errors="replace").strip()
    started = proc.returncode == 0 and (
        "started" in output.lower()
        or "already running" in output.lower()
        or "deploy queue empty" in output.lower()
    )
    return {
        "started": started,
        "status": "started" if started else "start_failed",
        "returncode": proc.returncode,
        "detail": (output or error)[-500:],
    }


@router.get(
    "/ops/deploy/status",
    dependencies=[Depends(require_internal_admin)],
    summary="공통 배포 관제 상태",
)
async def get_common_deploy_status():
    """배포 queue/phase/시간/BG 동기화 및 stale runner 신호를 조회한다."""
    from app.services.deploy_observability import get_deploy_status

    conn = None
    try:
        conn = await _get_conn()
        return await get_deploy_status(conn)
    except Exception as exc:
        logger.error("ops_deploy_status_failed", error=str(exc))
        return {
            "generated_at": datetime.now(timezone.utc),
            "schema_version": "deploy-observability-v1",
            "degraded": True,
            "degraded_reasons": ["database_query_failed"],
            "error_summary": "deployment telemetry is temporarily unavailable",
            "active_deployments": [],
            "queued_deployments": [],
            "recent_completed_deployments": [],
            "recent_durations_per_project": [],
            "phase_timeline": [],
            "stale_zombie_signals": [],
            "legacy_stale_candidates": [],
            "bg_digest_sync": [],
            "project_deployments": [],
            "component_deployments": [],
            "next_deploy_readiness": {
                "ready": False,
                "blockers": ["observability_unavailable"],
                "next_queued_runner_job_id": None,
            },
        }
    finally:
        if conn is not None:
            await conn.close()


@router.post(
    "/ops/deploy/requests",
    dependencies=[Depends(require_internal_admin)],
    summary="배포 요청을 ops DB 큐에 등록",
)
async def create_common_deploy_request(req: DeployQueueRequest):
    """커밋/푸시가 끝난 release SHA를 배포 큐에 등록하고 즉시 반환한다."""
    from app.services.deploy_observability import enqueue_deploy_request

    conn = None
    try:
        release_sha = (req.release_sha or os.getenv("AADS_RELEASE_SHA") or "").strip()
        if not release_sha:
            raise HTTPException(status_code=400, detail="release_sha is required")
        conn = await _get_conn()
        row = await enqueue_deploy_request(
            conn,
            project=normalize_project_label(req.project),
            release_sha=release_sha,
            component=req.component,
            deploy_type=req.deploy_type,
            target_env=req.target_env,
            runner_job_id=req.runner_job_id,
            requested_by=req.requested_by or "ops_api",
            request_source=req.request_source,
            commit_status=req.commit_status,
            push_status=req.push_status,
            auto_start=req.auto_start,
            rollback_plan=req.rollback_plan,
            approval_policy=req.approval_policy,
            metadata=req.metadata,
        )
        from app.services.deploy_adapters import DeployRequest, resolve_adapter

        project_key = str(row.get("project") or req.project or "").upper()
        component_key = str(row.get("component") or req.component or "api")
        adapter = resolve_adapter(project_key, component_key)
        adapter_request = DeployRequest(
            project=project_key,
            component=component_key,
            deploy_type=str(row.get("deploy_type") or req.deploy_type or ""),
            release_sha=str(row.get("release_sha") or release_sha),
            target_env=str(row.get("target_env") or req.target_env or "production"),
            deploy_run_id=row.get("id"),
            requested_by=req.requested_by or "ops_api",
            request_source=req.request_source,
            metadata=req.metadata or {},
        )
        preflight = await adapter.preflight(adapter_request)

        worker_start: Dict[str, Any] = {
            "started": False,
            "status": "not_requested",
            "detail": "auto_start=false",
            "ownership": adapter.ownership,
        }
        if req.auto_start:
            if preflight.ok:
                worker_start = (await adapter.start(adapter_request)).as_dict()
                if (
                    worker_start.get("status") == "launcher_unavailable"
                    and project_key == "AADS"
                    and component_key == "api"
                ):
                    # legacy fallback path kept for host installs without the registry launcher
                    worker_start = await _start_aads_deploy_queue_worker("ops_api_request")
            else:
                worker_start = {
                    "started": False,
                    "status": "preflight_blocked",
                    "detail": "; ".join(preflight.blockers)[:500],
                    "ownership": adapter.ownership,
                }
        return {
            "status": "queued",
            "deploy_run_id": row.get("id"),
            "adapter": adapter.describe(),
            "preflight": preflight.as_dict(),
            "project": row.get("project"),
            "component": row.get("component"),
            "deploy_type": row.get("deploy_type"),
            "target_env": row.get("target_env"),
            "release_sha": row.get("release_sha"),
            "phase": row.get("phase"),
            "queue_position": row.get("queue_position"),
            "deduplicated": row.get("deduplicated", False),
            "worker_start": worker_start,
            "next_check": "/api/v1/ops/deploy/status",
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("ops_deploy_request_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="deployment request queue failed") from exc
    finally:
        if conn is not None:
            await conn.close()


class DeployControlRequest(BaseModel):
    actor: str = "ops"
    reason: str = ""
    auto_start: bool = True


class DeployReconcileRequest(BaseModel):
    dry_run: bool = True
    actor: str = "ops"


@router.get(
    "/ops/deploy/adapters",
    dependencies=[Depends(require_internal_admin)],
    summary="등록된 배포 어댑터 목록",
)
async def list_deploy_adapters():
    """프로젝트/컴포넌트별 배포 어댑터와 실행 소유권을 반환한다."""
    from app.services.deploy_adapters import list_adapters, registry_coverage

    return {
        "generated_at": datetime.now(timezone.utc),
        "coverage": registry_coverage(),
        "adapters": list_adapters(),
    }


@router.post(
    "/ops/deploy/reconcile",
    dependencies=[Depends(require_internal_admin)],
    summary="stale 배포 run/lock 정리 (기본 dry-run)",
)
async def reconcile_deploy_runs(req: DeployReconcileRequest):
    """heartbeat 끊긴 run, 오래된 queue, 만료 lock을 탐지하고 선택적으로 정리한다."""
    from app.services.deploy_control import reconcile_deploy_state

    conn = None
    try:
        conn = await _get_conn()
        return await reconcile_deploy_state(conn, dry_run=req.dry_run, actor=req.actor)
    except Exception as exc:
        logger.error("ops_deploy_reconcile_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="deploy reconcile failed") from exc
    finally:
        if conn is not None:
            await conn.close()


@router.post(
    "/ops/deploy/{run_id}/cancel",
    dependencies=[Depends(require_internal_admin)],
    summary="대기 중인 배포 취소",
)
async def cancel_deploy_run_api(run_id: int, req: DeployControlRequest):
    from app.services.deploy_control import cancel_deploy_run

    conn = None
    try:
        conn = await _get_conn()
        result = await cancel_deploy_run(conn, run_id, actor=req.actor, reason=req.reason)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ops_deploy_cancel_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail="deploy cancel failed") from exc
    finally:
        if conn is not None:
            await conn.close()


@router.post(
    "/ops/deploy/{run_id}/approve",
    dependencies=[Depends(require_internal_admin)],
    summary="승인 대기 배포 승인",
)
async def approve_deploy_run_api(run_id: int, req: DeployControlRequest):
    from app.services.deploy_control import approve_deploy_run

    conn = None
    try:
        conn = await _get_conn()
        result = await approve_deploy_run(conn, run_id, actor=req.actor)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ops_deploy_approve_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail="deploy approve failed") from exc
    finally:
        if conn is not None:
            await conn.close()


@router.post(
    "/ops/deploy/{run_id}/retry",
    dependencies=[Depends(require_internal_admin)],
    summary="실패/차단 배포 재시도",
)
async def retry_deploy_run_api(run_id: int, req: DeployControlRequest):
    from app.services.deploy_control import retry_deploy_run

    conn = None
    try:
        conn = await _get_conn()
        result = await retry_deploy_run(conn, run_id, actor=req.actor, auto_start=req.auto_start)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ops_deploy_retry_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail="deploy retry failed") from exc
    finally:
        if conn is not None:
            await conn.close()


@router.get(
    "/ops/deploy/{run_id}/logs",
    dependencies=[Depends(require_internal_admin)],
    summary="배포 run phase timeline + 로그",
)
async def get_deploy_run_logs_api(run_id: int, max_lines: int = 200):
    from app.services.deploy_control import get_deploy_run_logs

    conn = None
    try:
        conn = await _get_conn()
        result = await get_deploy_run_logs(conn, run_id, max_lines=max(20, min(1000, max_lines)))
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ops_deploy_logs_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail="deploy logs failed") from exc
    finally:
        if conn is not None:
            await conn.close()


# ─── Models ─────────────────────────────────────────────────────────────────

class LifecycleUpdate(BaseModel):
    task_id: str
    project: str = "AADS"
    status: str
    timestamp: Optional[str] = None
    title: Optional[str] = None
    server: Optional[str] = None
    priority: Optional[str] = None
    executor: Optional[str] = None
    file_path: Optional[str] = None
    error_detail: Optional[str] = None


class CostRecord(BaseModel):
    task_id: str
    project: str = "AADS"
    model: Optional[str] = None
    tenant_id: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0


class CommitRecord(BaseModel):
    task_id: str
    repo: Optional[str] = None
    commit_sha: Optional[str] = None
    message: Optional[str] = None
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    http_verified: bool = False


class MaintenanceStartRequest(BaseModel):
    server: str
    reason: str
    estimated_minutes: int = 15
    services: List[str] = []
    started_by: str = "ceo"


class MaintenanceEndRequest(BaseModel):
    server: str

class BridgeLogRecord(BaseModel):
    message_id: Optional[str] = None
    source_channel: Optional[str] = None
    classification: Optional[str] = None
    action_taken: Optional[str] = None
    directive_task_id: Optional[str] = None
    blocked_reason: Optional[str] = None
    raw_length: int = 0


class WorkspaceChangeFinalizeRequest(BaseModel):
    session_id: str
    project: Optional[str] = None
    repo: Optional[str] = None
    reason: str = "manual"


# ─── Directive Lifecycle ──────────────────────────────────────────────────────

@router.post("/ops/directive-lifecycle")
async def upsert_lifecycle(req: LifecycleUpdate):
    """지시서 라이프사이클 상태 기록 (UPSERT)."""
    ts = datetime.now(tz=KST)
    if req.timestamp:
        try:
            ts = datetime.fromisoformat(req.timestamp)
        except Exception:
            pass

    project = normalize_project_label(req.project)
    try:
        conn = await _get_conn()
        try:
            # UPSERT — 타임스탬프를 status에 따라 미리 계산하여 전달
            q_at = ts if req.status in ("queued", "requeued") else None
            s_at = ts if req.status == "running" else None
            c_at = ts if req.status in ("completed", "failed") else None
            await conn.execute("""
                INSERT INTO directive_lifecycle
                    (task_id, project, title, server, priority, executor, file_path, status,
                     created_at, queued_at, started_at, completed_at, error_detail)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),$9,$10,$11,$12)
                ON CONFLICT (task_id, project) DO UPDATE SET
                    status       = EXCLUDED.status,
                    queued_at    = COALESCE(EXCLUDED.queued_at, directive_lifecycle.queued_at),
                    started_at   = COALESCE(EXCLUDED.started_at, directive_lifecycle.started_at),
                    completed_at = COALESCE(EXCLUDED.completed_at, directive_lifecycle.completed_at),
                    title        = COALESCE(EXCLUDED.title, directive_lifecycle.title),
                    server       = COALESCE(EXCLUDED.server, directive_lifecycle.server),
                    priority     = COALESCE(EXCLUDED.priority, directive_lifecycle.priority),
                    executor     = COALESCE(EXCLUDED.executor, directive_lifecycle.executor),
                    file_path    = COALESCE(EXCLUDED.file_path, directive_lifecycle.file_path),
                    error_detail = COALESCE(EXCLUDED.error_detail, directive_lifecycle.error_detail)
            """, req.task_id, project, req.title, req.server,
                req.priority, req.executor, req.file_path, req.status,
                q_at, s_at, c_at, req.error_detail)
        finally:
            await conn.close()
        return {"ok": True, "task_id": req.task_id, "status": req.status}
    except Exception as e:
        logger.error("ops_lifecycle_upsert_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ops/directive-lifecycle")
async def list_lifecycle(
    project: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(50, le=500),
):
    """지시서 라이프사이클 목록 조회."""
    conditions = []
    params: list = []
    idx = 1
    if project:
        conditions.append(f"dl.project = ${idx}")
        params.append(normalize_project_label(project))
        idx += 1
    if status:
        conditions.append(f"dl.status = ${idx}")
        params.append(status)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT dl.id, dl.task_id, dl.project, dl.title, dl.server, dl.priority, dl.status,
               dl.queued_at, dl.started_at, dl.completed_at, dl.duration_seconds,
               dl.wait_seconds, dl.error_detail, pj.actual_model
        FROM directive_lifecycle dl
        LEFT JOIN pipeline_jobs pj ON pj.job_id = dl.task_id
        {where}
        ORDER BY COALESCE(dl.completed_at, dl.started_at, dl.queued_at, dl.created_at) DESC
        LIMIT ${idx}
    """
    params.append(limit)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(sql, *params)
        finally:
            await conn.close()
        return {"items": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error("ops_lifecycle_list_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ops/directive-lifecycle/{task_id}")
async def get_lifecycle(task_id: str):
    """지시서 라이프사이클 상세 조회."""
    try:
        conn = await _get_conn()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM directive_lifecycle WHERE task_id=$1 ORDER BY id DESC LIMIT 1",
                task_id
            )
        finally:
            await conn.close()
        if not row:
            raise HTTPException(status_code=404, detail=f"task_id {task_id} not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Cost Tracking ────────────────────────────────────────────────────────────

@router.post("/ops/cost")
async def record_cost(req: CostRecord):
    """비용 기록."""
    project = normalize_project_label(req.project)
    try:
        conn = await _get_conn()
        try:
            await conn.execute("""
                INSERT INTO cost_tracking (task_id, project, model, input_tokens,
                    output_tokens, cost_usd, llm_calls, tenant_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7, COALESCE($8::uuid, public.aads_internal_tenant_id()))
            """, req.task_id, project, req.model, req.input_tokens,
                req.output_tokens, req.cost_usd, req.llm_calls, req.tenant_id)
        finally:
            await conn.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ops/cost/summary")
async def cost_summary(
    project: Optional[str] = None,
    days: int = Query(7, le=90),
):
    """일별/프로젝트별/모델별 비용 집계."""
    conditions = ["recorded_at >= NOW() - INTERVAL '1 day' * $1"]
    params: list = [days]
    idx = 2
    if project:
        conditions.append(f"project = ${idx}")
        params.append(normalize_project_label(project))
        idx += 1
    where = "WHERE " + " AND ".join(conditions)
    try:
        conn = await _get_conn()
        try:
            by_project = await conn.fetch(f"""
                SELECT project, SUM(cost_usd) as total_cost,
                       SUM(input_tokens) as total_input, SUM(output_tokens) as total_output,
                       COUNT(*) as records
                FROM cost_tracking {where}
                GROUP BY project ORDER BY total_cost DESC
            """, *params)
            by_model = await conn.fetch(f"""
                SELECT model, SUM(cost_usd) as total_cost, COUNT(*) as calls
                FROM cost_tracking {where}
                GROUP BY model ORDER BY total_cost DESC
            """, *params)
            by_day = await conn.fetch(f"""
                SELECT DATE(recorded_at AT TIME ZONE 'Asia/Seoul') as day,
                       SUM(cost_usd) as total_cost, COUNT(*) as records
                FROM cost_tracking {where}
                GROUP BY day ORDER BY day DESC
            """, *params)
            total = await conn.fetchrow(f"""
                SELECT SUM(cost_usd) as grand_total, COUNT(*) as total_records
                FROM cost_tracking {where}
            """, *params)
        finally:
            await conn.close()
        return {
            "days": days,
            "grand_total_usd": float(total["grand_total"] or 0),
            "total_records": total["total_records"],
            "by_project": [dict(r) for r in by_project],
            "by_model": [dict(r) for r in by_model],
            "by_day": [dict(r) for r in by_day],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Commit Log ──────────────────────────────────────────────────────────────

@router.post("/ops/commit")
async def record_commit(req: CommitRecord):
    """커밋 기록."""
    try:
        conn = await _get_conn()
        try:
            await conn.execute("""
                INSERT INTO commit_log (task_id, repo, commit_sha, message,
                    files_changed, lines_added, lines_deleted, http_verified)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """, req.task_id, req.repo, req.commit_sha, req.message,
                req.files_changed, req.lines_added, req.lines_deleted, req.http_verified)
        finally:
            await conn.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ops/commits")
async def list_commits(
    task_id: Optional[str] = None,
    limit: int = Query(50, le=200),
):
    """커밋 로그 조회."""
    conditions = []
    params: list = []
    idx = 1
    if task_id:
        conditions.append(f"task_id = ${idx}")
        params.append(task_id)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                f"SELECT * FROM commit_log {where} ORDER BY pushed_at DESC LIMIT ${idx}",
                *params
            )
        finally:
            await conn.close()
        return {"items": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ops/workspace-changes")
async def list_workspace_changes(
    session_id: str,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    status: Optional[str] = None,
):
    """세션별 workspace 변경 ledger 조회."""
    try:
        from app.services.workspace_change_tracker import list_changes

        statuses = [status] if status else None
        items = await list_changes(
            session_id=session_id,
            project=project,
            repo=repo,
            statuses=statuses,
        )
        return {"items": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ops/workspace-changes/finalize")
async def finalize_workspace_changes(req: WorkspaceChangeFinalizeRequest):
    """세션의 pending 변경을 finalize(commit/push)."""
    try:
        from app.services.workspace_change_tracker import finalize_session_changes

        return await finalize_session_changes(
            session_id=req.session_id,
            project=req.project,
            repo=req.repo,
            reason=req.reason,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Bridge Log ──────────────────────────────────────────────────────────────


@router.post("/ops/bridge-log")
async def record_bridge_log(req: BridgeLogRecord):
    """브릿지 활동 기록."""
    try:
        conn = await _get_conn()
        try:
            await conn.execute("""
                INSERT INTO bridge_activity_log
                    (message_id, source_channel, classification, action_taken,
                     directive_task_id, blocked_reason, raw_length)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
            """, req.message_id, req.source_channel, req.classification,
                req.action_taken, req.directive_task_id, req.blocked_reason,
                req.raw_length)
        finally:
            await conn.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/ops/bridge-log")
async def bridge_log(
    classification: Optional[str] = None,
    limit: int = Query(50, le=200),
):
    """브릿지 활동 로그 조회."""
    conditions = []
    params: list = []
    idx = 1
    if classification:
        conditions.append(f"classification = ${idx}")
        params.append(classification)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                f"SELECT * FROM bridge_activity_log {where} ORDER BY detected_at DESC LIMIT ${idx}",
                *params
            )
        finally:
            await conn.close()
        return {"items": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Environment History ─────────────────────────────────────────────────────

@router.get("/ops/env-history/{server}")
async def env_history(server: str, limit: int = Query(20, le=100)):
    """서버별 환경 이력 조회 (최근 N건)."""
    canonical_server = resolve_server_id(server)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                "SELECT * FROM server_env_history WHERE server=$1 ORDER BY snapshot_at DESC LIMIT $2",
                canonical_server, limit
            )
        finally:
            await conn.close()
        return {
            "server": canonical_server,
            "requested_server": server,
            "items": [dict(r) for r in rows],
            "count": len(rows),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Health Check ─────────────────────────────────────────────────────────────

@router.get("/ops/active-streams")
async def active_streams():
    """현재 활성 SSE 스트리밍 세션 수 반환."""
    snapshot = await _get_stream_activity_snapshot()
    return {
        "count": snapshot["executing_count"],
        "sessions": snapshot["executing_sessions"],
        **snapshot,
    }


_CLI_SESSION_STORE = os.getenv("AADS_CLI_SESSION_STORE", "/tmp/.claude-sdk/.claude/projects")
_CLI_SESSION_STALE_SEC = int(os.getenv("AADS_CLI_SESSION_STALE_SEC", "1800"))


def _check_cli_session_persistence(turns_recent: int) -> dict:
    """CLI 대화가 영속 볼륨에 실제로 쌓이고 있는지 본다.

    2026-09-13: 인증 격리 수정이 대화 영속 수정을 조용히 깼는데 아무도 몰랐다.
    볼륨은 마운트돼 있었고 코드도 남아 있어 겉보기에는 정상이었다. 실제로는
    CLI 의 HOME 이 호출마다 바뀌면서 대화가 임시 디렉터리로 가 매번 지워졌고,
    resume 이 전부 실패해 턴마다 전체 프롬프트(실측 5만 자)를 다시 보냈다.

    "마운트돼 있는가"로는 못 잡는다. "최근에 쓰이고 있는가"로 봐야 한다.
    채팅 턴이 돌았는데 볼륨에 그만큼의 쓰기가 없으면 저장 경로가 끊긴 것이다.
    """
    label = "CLI 대화 영속"
    store = Path(_CLI_SESSION_STORE)
    try:
        if not store.is_dir():
            return {"ok": False, "count": 1, "label": label,
                    "detail": f"대화 저장소 없음: {store}"}
        newest = 0.0
        files = 0
        writes_recent = 0
        window_start = _time.time() - _CLI_SESSION_STALE_SEC
        for root, _dirs, names in os.walk(store):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                files += 1
                try:
                    mtime = os.stat(os.path.join(root, name)).st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
                if mtime >= window_start:
                    writes_recent += 1
        if not files:
            return {"ok": False, "count": 1, "label": label,
                    "detail": "대화 파일 0건 — 저장 경로가 볼륨에 연결되지 않았다"}
        age = int(_time.time() - newest) if newest else None
        # 최근 턴이 없으면 판정하지 않는다. 유휴 시간에 오래된 것은 정상이다.
        if turns_recent <= 0:
            return {"ok": True, "count": 0, "label": label,
                    "detail": f"최근 턴 없음 — 판정 보류 (파일 {files}건, 최신 {age}초 전)"}
        # 완전 단절: 턴은 돌았는데 볼륨에 쓰기가 하나도 없다.
        if writes_recent == 0:
            return {"ok": False, "count": 1, "label": label,
                    "detail": (f"턴 {turns_recent}건이 돌았는데 볼륨 쓰기 0건(최신 {age}초 전) — "
                               "대화가 볼륨 밖에 쌓이고 있다. resume 불가, 매 턴 전체 프롬프트 재전송")}
        # 부분 단절은 실패로 단정하지 않는다. 턴과 대화파일은 1:1 이 아니라
        # 비율만으로 판정하면 오탐이 난다. 수치를 남겨 사람이 보게 한다.
        return {"ok": True, "count": 0, "label": label,
                "detail": (f"턴 {turns_recent}건 / 볼륨 쓰기 {writes_recent}건, "
                           f"파일 {files}건, 최신 {age}초 전")}
    except Exception as exc:  # 헬스체크가 예외로 죽으면 안 된다
        return {"ok": True, "count": 0, "label": label,
                "detail": f"점검 실패(무시): {str(exc)[:80]}"}


@router.get("/ops/health-check")
async def health_check():
    """전체 파이프라인 건전성 확인."""
    try:
        conn = await _get_conn()
        try:
            stalled_queue = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle "
                "WHERE status='queued' AND queued_at < NOW() - INTERVAL '10 min'"
            )
            stalled_running = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle "
                "WHERE status='running' AND started_at < NOW() - INTERVAL '60 min'"
            )
            recent_completed = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle "
                "WHERE status='completed' AND completed_at > NOW() - INTERVAL '30 min'"
            )
            active_count = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status IN ('queued','running')"
            )
            completed_today = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status = 'completed' AND completed_at >= CURRENT_DATE"
            )
            running_count = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status = 'running'"
            )
            error_count = await conn.fetchval(
                "SELECT COUNT(*) FROM directive_lifecycle WHERE status = 'failed'"
            )
            # 최근 bridge 활동 (1시간)
            bridge_recent = await conn.fetchval(
                "SELECT COUNT(*) FROM bridge_activity_log WHERE detected_at > NOW() - INTERVAL '1 hour'"
            )
            # 체크 8/9: 최신 blocked/undetected 카운트 및 마지막 체크 시각
            blocked_tasks = await conn.fetchval(
                "SELECT metric_value FROM system_metrics "
                "WHERE server='68' AND metric_name='blocked_tasks_count' "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            undetected_tasks = await conn.fetchval(
                "SELECT metric_value FROM system_metrics "
                "WHERE server='68' AND metric_name='undetected_tasks_count' "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            last_seen_check_ts = await conn.fetchval(
                "SELECT recorded_at FROM system_metrics "
                "WHERE server='68' AND metric_name='blocked_tasks_count' "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            # CLI 대화 영속 점검에 쓸 최근 채팅 활동량.
            # 턴이 돌았는데 대화 볼륨에 쓰기가 없으면 저장 경로가 끊긴 것이다.
            cli_turns_recent = await conn.fetchval(
                "SELECT COUNT(*) FROM chat_turn_executions "
                "WHERE created_at > NOW() - INTERVAL '30 minutes'"
            )
            # AADS-116: 유지보수 모드 상태
            maintenance_row = await conn.fetchrow(
                "SELECT server, reason FROM maintenance_schedule "
                "WHERE status='active' ORDER BY started_at DESC LIMIT 1"
            )
        finally:
            await conn.close()

        stalled_count = int(stalled_queue or 0) + int(stalled_running or 0)
        pipeline_blocked = (int(recent_completed or 0) == 0 and int(active_count or 0) > 0)
        pipeline_healthy = (stalled_count == 0 and not pipeline_blocked)

        last_seen_check_kst = (
            last_seen_check_ts.astimezone(KST).isoformat()
            if last_seen_check_ts else None
        )

        maintenance_active = maintenance_row is not None
        issues_list = _build_issues(stalled_queue, stalled_running, pipeline_blocked)
        checks = {
            "queue_stall": { "ok": int(stalled_queue or 0) == 0, "count": int(stalled_queue or 0), "label": "큐 정체" },
            "execution_stall": { "ok": int(stalled_running or 0) == 0, "count": int(stalled_running or 0), "label": "실행 정체" },
            "pipeline_flow": { "ok": not pipeline_blocked, "count": 1 if pipeline_blocked else 0, "label": "파이프라인 흐름" },
            "bridge_integrity": { "ok": True, "count": 0, "label": "브릿지 정합성" },
            "commit_integrity": { "ok": True, "count": 0, "label": "커밋 정합성" },
            "cost_tracking": { "ok": True, "count": 0, "label": "비용 추적" },
            "env_trend": { "ok": True, "count": 0, "label": "환경 트렌드" },
            "manager_response": { "ok": True, "count": 0, "label": "매니저 응답" },
            "cli_session_persistence": _check_cli_session_persistence(int(cli_turns_recent or 0)),
        }
        # ── 인프라 상태 (컨테이너 + DB풀 + 디스크 + 메모리) ──
        import shutil as _shutil
        import subprocess as _sp

        infra = {}
        # 컨테이너 상태 (Docker API via socket proxy)
        # OHVIS Blue/Green: 각 서비스는 -blue/-green 슬롯 중 하나만 활성이면 healthy 처리
        _docker_host = os.environ.get("DOCKER_HOST", "")
        _container_names = [
            "aads-server", "aads-server-blue", "aads-server-green",
            "aads-dashboard", "aads-dashboard-blue", "aads-dashboard-green",
            "aads-postgres", "aads-redis", "aads-socket-proxy", "aads-litellm",
        ]
        for cname in _container_names:
            try:
                if _docker_host.startswith("tcp://"):
                    _base = _docker_host.replace("tcp://", "http://")
                    _url = f"{_base}/v1.41/containers/{cname}/json"
                    _r = _sp.run(["curl", "-sf", _url, "--max-time", "3"], capture_output=True, text=True, timeout=5)
                else:
                    _url = f"http://localhost/v1.41/containers/{cname}/json"
                    _r = _sp.run(["curl", "-sf", "--unix-socket", "/var/run/docker.sock", _url, "--max-time", "3"],
                                 capture_output=True, text=True, timeout=5)
                if _r.returncode == 0 and _r.stdout:
                    import json as _j2
                    _cdata = _j2.loads(_r.stdout)
                    infra[cname] = _cdata.get("State", {}).get("Status", "unknown")
                else:
                    infra[cname] = "unknown"
            except Exception:
                infra[cname] = "unknown"

        # DB 커넥션 풀
        try:
            from app.core.db_pool import get_pool_stats
            infra["db_pool"] = get_pool_stats()
        except Exception:
            infra["db_pool"] = {"available": False}

        # 디스크
        try:
            _du = _shutil.disk_usage("/")
            infra["disk_pct"] = round(_du.used / _du.total * 100, 1)
            infra["disk_free_gb"] = round(_du.free / (1024**3), 1)
        except Exception:
            infra["disk_pct"] = None

        # 메모리
        try:
            with open("/proc/meminfo") as _f:
                _lines = _f.readlines()
            _mt = _ma = 0
            for _l in _lines:
                if _l.startswith("MemTotal:"): _mt = int(_l.split()[1])  # noqa: E701
                elif _l.startswith("MemAvailable:"): _ma = int(_l.split()[1]) # noqa: E701
            if _mt > 0:
                infra["memory_pct"] = round((1 - _ma / _mt) * 100, 1)
        except Exception:
            infra["memory_pct"] = None

        # 로드
        try:
            _load = os.getloadavg()
            infra["load_1m"] = round(_load[0], 2)
        except Exception:
            infra["load_1m"] = None

        # placeholder 잔존
        try:
            from app.core.db_pool import get_pool as _gp
            async with _gp().acquire() as _hc:
                infra["stale_placeholders"] = await _hc.fetchval(
                    "SELECT count(*) FROM chat_messages WHERE intent = 'streaming_placeholder'"
                )
        except Exception:
            infra["stale_placeholders"] = None

        try:
            _stream_snapshot = await _get_stream_activity_snapshot()
            infra["active_streams_executing"] = _stream_snapshot["executing_count"]
            infra["active_streams_visible"] = _stream_snapshot["visible_count"]
            infra["recovery_pending_streams"] = _stream_snapshot["recovery_pending_count"]
            infra["recent_placeholders"] = _stream_snapshot["placeholder_recent_count"]
        except Exception:
            infra["active_streams_executing"] = None
            infra["active_streams_visible"] = None
            infra["recovery_pending_streams"] = None
            infra["recent_placeholders"] = None

        # ── completion_rate_24h: 24시간 실행 완료율 (50% 미만 시 경보 대상) ──
        try:
            _cr_row = await _hc.fetchrow(
                "SELECT count(*) FILTER (WHERE status='completed') as ok, count(*) as total "
                "FROM chat_turn_executions WHERE started_at > now() - interval '24 hours'"
            )
            _cr_total = int(_cr_row["total"]) if _cr_row else 0
            infra["completion_rate_24h"] = round(100.0 * int(_cr_row["ok"]) / _cr_total, 1) if _cr_total > 0 else None
            infra["completions_24h"] = int(_cr_row["ok"]) if _cr_row else 0
            infra["executions_24h"] = _cr_total
        except Exception:
            infra["completion_rate_24h"] = None

        # OHVIS Blue/Green: aads-server / aads-dashboard 는 슬롯 페어 중 하나만 running 이면 OK
        _pair_ok_server = any(
            infra.get(_n) == "running"
            for _n in ("aads-server", "aads-server-blue", "aads-server-green")
        )
        _pair_ok_dashboard = any(
            infra.get(_n) == "running"
            for _n in ("aads-dashboard", "aads-dashboard-blue", "aads-dashboard-green")
        )
        _singleton_ok = all(
            infra.get(_n) == "running"
            for _n in ("aads-postgres", "aads-redis", "aads-socket-proxy", "aads-litellm")
        )
        all_containers_ok = _pair_ok_server and _pair_ok_dashboard and _singleton_ok

        return {
            "pipeline_healthy": pipeline_healthy and all_containers_ok,
            "stalled_count": stalled_count,
            "stalled_queue": int(stalled_queue or 0),
            "stalled_running": int(stalled_running or 0),
            "active_count": int(active_count or 0),
            "completed_today": int(completed_today or 0),
            "running_count": int(running_count or 0),
            "error_count": int(error_count or 0),
            "recent_completed_30m": int(recent_completed or 0),
            "checks": checks,
            "pipeline_blocked": pipeline_blocked,
            "bridge_activity_1h": int(bridge_recent or 0),
            "blocked_tasks_count": int(blocked_tasks or 0),
            "undetected_tasks_count": int(undetected_tasks or 0),
            "last_seen_tasks_check": last_seen_check_kst,
            "maintenance_active": maintenance_active,
            "maintenance_server": maintenance_row["server"] if maintenance_active else None,
            "maintenance_reason": maintenance_row["reason"] if maintenance_active else None,
            "issues": issues_list,
            "infra": infra,
            "checked_at": datetime.now(KST).isoformat(),
        }
    except Exception as e:
        logger.error("ops_health_check_error", error=str(e))
        return {
            "pipeline_healthy": False,
            "error": str(e),
            "stalled_count": -1,
            "maintenance_active": False,
            "maintenance_server": None,
            "maintenance_reason": None,
            "issues": [{"type": "db_error", "detail": str(e)}],
        }


def _remote_health_config(server_id: str) -> dict[str, Any] | None:
    sid = resolve_server_id(server_id)
    cfg = get_server_config(sid)
    if not cfg:
        return None
    return {
        "id": sid,
        "host": cfg["host"],
        "port": 9090,
        "ssh_port": int(cfg.get("ssh_port", 22)),
        "type": cfg.get("type", "ssh"),
    }


def _run_server_probe(cfg: dict[str, Any], command: str, timeout: int = 8):
    import subprocess as _sp

    if cfg.get("type") == "local":
        return _sp.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    return _sp.run(
        [
            "ssh",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-p", str(cfg.get("ssh_port", 22)),
            f"root@{cfg['host']}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_free_m(output: str) -> dict[str, Any]:
    for line in output.splitlines():
        if line.strip().startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                total = int(parts[1])
                used = int(parts[2])
                available = int(parts[6])
                return {
                    "memory_pct": round((used / total) * 100, 1) if total else None,
                    "memory_total_mb": total,
                    "memory_used_mb": used,
                    "memory_available_mb": available,
                }
    return {}


def _parse_meminfo(output: str) -> dict[str, Any]:
    values: dict[str, int] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0])
        except ValueError:
            continue
    total_kb = values.get("MemTotal")
    available_kb = values.get("MemAvailable")
    if not total_kb or available_kb is None:
        return {}
    used_kb = max(0, total_kb - available_kb)
    return {
        "memory_pct": round((used_kb / total_kb) * 100, 1),
        "memory_total_mb": round(total_kb / 1024),
        "memory_used_mb": round(used_kb / 1024),
        "memory_available_mb": round(available_kb / 1024),
    }


def _parse_df_h(output: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    parts = lines[1].split()
    if len(parts) < 5:
        return {}
    pct_match = re.search(r"(\d+)%", parts[4])
    return {
        "disk_pct": int(pct_match.group(1)) if pct_match else None,
        "disk_total": parts[1],
        "disk_used": parts[2],
        "disk_available": parts[3],
    }


def _parse_uptime(output: str) -> dict[str, Any]:
    match = re.search(r"load average:\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)", output)
    if not match:
        return {"uptime": output.strip()[:160]} if output.strip() else {}
    return {
        "load_1m": float(match.group(1)),
        "load_5m": float(match.group(2)),
        "load_15m": float(match.group(3)),
        "uptime": output.strip()[:160],
    }


def _parse_loadavg(output: str) -> dict[str, Any]:
    parts = output.split()
    if len(parts) < 3:
        return {}
    try:
        return {
            "load_1m": float(parts[0]),
            "load_5m": float(parts[1]),
            "load_15m": float(parts[2]),
        }
    except ValueError:
        return {}


def _parse_lscpu(output: str) -> dict[str, Any]:
    """lscpu 출력에서 CPU 사양 추출 (코어 수·모델명·아키텍처)."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()

    result: dict[str, Any] = {}
    for key in ("cpu(s)",):
        try:
            result["cpu_cores"] = int(fields[key])
            break
        except (KeyError, ValueError):
            continue
    if fields.get("model name"):
        result["cpu_model"] = fields["model name"][:80]
    if fields.get("architecture"):
        result["cpu_arch"] = fields["architecture"]
    if fields.get("hypervisor vendor"):
        result["cpu_virtualization"] = fields["hypervisor vendor"]
    for key in ("thread(s) per core",):
        try:
            result["cpu_threads_per_core"] = int(fields[key])
        except (KeyError, ValueError):
            pass
    return result


def _parse_nproc(output: str) -> dict[str, Any]:
    """lscpu 미설치 환경 폴백 — 코어 수만 확보."""
    try:
        return {"cpu_cores": int(output.strip().splitlines()[0])}
    except (ValueError, IndexError):
        return {}


def _derive_cpu_load_pct(metrics: dict[str, Any]) -> Optional[float]:
    """load_1m / 코어 수 → CPU 부하율(%). 코어 수 없으면 계산 불가."""
    load_1m = metrics.get("load_1m")
    cores = metrics.get("cpu_cores")
    if not isinstance(load_1m, (int, float)) or not isinstance(cores, int) or cores <= 0:
        return None
    return round(load_1m / cores * 100, 1)


def _status_from_metrics(metrics: dict[str, Any]) -> str:
    disk_pct = metrics.get("disk_pct")
    memory_pct = metrics.get("memory_pct")
    cpu_load_pct = metrics.get("cpu_load_pct")
    if (
        (isinstance(disk_pct, (int, float)) and disk_pct >= 90)
        or (isinstance(memory_pct, (int, float)) and memory_pct >= 90)
        or (isinstance(cpu_load_pct, (int, float)) and cpu_load_pct >= 200)
    ):
        return "critical"
    if (
        (isinstance(disk_pct, (int, float)) and disk_pct >= 80)
        or (isinstance(memory_pct, (int, float)) and memory_pct >= 80)
        or (isinstance(cpu_load_pct, (int, float)) and cpu_load_pct >= 100)
    ):
        return "warning"
    return "ok"


def _collect_server_health_fallback(cfg: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    errors: list[str] = []
    probes: list[tuple[str, Any, str | None, Any | None]] = [
        ("free -m", _parse_free_m, "cat /proc/meminfo", _parse_meminfo),
        ("df -h /", _parse_df_h, None, None),
        ("uptime", _parse_uptime, "cat /proc/loadavg", _parse_loadavg),
        ("lscpu", _parse_lscpu, "nproc", _parse_nproc),
    ]
    for command, parser, fallback_command, fallback_parser in probes:
        try:
            result = _run_server_probe(cfg, command, timeout=8)
            if result.returncode == 0:
                metrics.update(parser(result.stdout))
                continue
            if fallback_command and fallback_parser:
                fallback_result = _run_server_probe(cfg, fallback_command, timeout=8)
                if fallback_result.returncode == 0:
                    metrics.update(fallback_parser(fallback_result.stdout))
                    continue
            errors.append(f"{command}: {result.stderr.strip()[:120]}")
        except Exception as exc:
            errors.append(f"{command}: {str(exc)[:120]}")

    cpu_load_pct = _derive_cpu_load_pct(metrics)
    if cpu_load_pct is not None:
        metrics["cpu_load_pct"] = cpu_load_pct

    status = _status_from_metrics(metrics) if metrics else "fail"
    return {
        "server_id": cfg.get("id"),
        "host": cfg.get("host"),
        "status": status,
        "healthy": status in {"ok", "warning"},
        "source": "ssh_system_probe_fallback" if cfg.get("type") != "local" else "local_system_probe_fallback",
        "checked_at": datetime.now(KST).isoformat(),
        "services": {},
        "errors": errors[:3],
        **metrics,
    }

@router.get("/ops/server-health/{server_id}")
async def remote_server_health(server_id: str):
    """원격 서버 헬스체크 프록시 — 브라우저 CORS/방화벽 우회."""
    cfg = _remote_health_config(server_id)
    if not cfg:
        raise HTTPException(404, f"Unknown server: {server_id}")
    try:
        r = _run_server_probe(cfg, f"curl -sf --max-time 3 http://localhost:{cfg['port']}/health", timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            import json as _json
            payload = _json.loads(r.stdout.strip())
            payload.setdefault("server_id", cfg.get("id"))
            payload.setdefault("host", cfg.get("host"))
            payload.setdefault("source", "health_server")
            return payload
        fallback = _collect_server_health_fallback(cfg)
        if r.stderr.strip():
            fallback["health_server_error"] = r.stderr.strip()[:200]
        return fallback
    except Exception as e:
        fallback = _collect_server_health_fallback(cfg)
        fallback["health_server_error"] = str(e)[:200]
        return fallback


def _build_issues(stalled_queue, stalled_running, pipeline_blocked):
    issues = []
    if int(stalled_queue or 0) > 0:
        issues.append({"type": "queue_stalled", "count": int(stalled_queue), "severity": "critical"})
    if int(stalled_running or 0) > 0:
        issues.append({"type": "execution_stalled", "count": int(stalled_running), "severity": "critical"})
    if pipeline_blocked:
        issues.append({"type": "pipeline_blocked", "severity": "critical"})
    return issues


# ─── Stalled ─────────────────────────────────────────────────────────────────

@router.get("/ops/stalled")
async def list_stalled():
    """정체된 지시서 목록."""
    try:
        conn = await _get_conn()
        try:
            stalled = await conn.fetch("""
                SELECT task_id, project, status, title,
                       queued_at, started_at,
                       EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, queued_at)))::INTEGER AS stalled_seconds
                FROM directive_lifecycle
                WHERE (status='queued' AND queued_at < NOW() - INTERVAL '10 min')
                   OR (status='running' AND started_at < NOW() - INTERVAL '60 min')
                ORDER BY stalled_seconds DESC
            """)
        finally:
            await conn.close()
        return {"stalled": [dict(r) for r in stalled], "count": len(stalled)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Auto Recover ─────────────────────────────────────────────────────────────

@router.post("/ops/auto-recover")
async def auto_recover(request: Request):
    """수동 복구 트리거 — CrossValidator.run_all_checks() 즉시 실행."""
    try:
        from app.services.cross_validator import CrossValidator
        import asyncpg as _asyncpg
        pool = await _asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        try:
            validator = CrossValidator(pool)
            results = await validator.run_all_checks()
        finally:
            await pool.close()
        return {"ok": True, "issues_found": len(results), "results": results}
    except Exception as e:
        logger.error("ops_auto_recover_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ─── Maintenance Mode (AADS-116) ──────────────────────────────────────────────

@router.post("/ops/maintenance/start")
async def maintenance_start(req: MaintenanceStartRequest):
    """유지보수 모드 시작 — 해당 서비스 감시 일시 정지."""
    try:
        conn = await _get_conn()
        try:
            # 기존 active 유지보수 종료
            await conn.execute(
                "UPDATE maintenance_schedule SET status='ended', actual_end=NOW() "
                "WHERE server=$1 AND status='active'",
                req.server
            )
            estimated_end = datetime.now(tz=KST) + timedelta(minutes=req.estimated_minutes)
            row = await conn.fetchrow(
                """
                INSERT INTO maintenance_schedule
                    (server, reason, services_paused, started_at, estimated_end, started_by, status)
                VALUES ($1, $2, $3, NOW(), $4, $5, 'active')
                RETURNING id, started_at, estimated_end
                """,
                req.server, req.reason, req.services, estimated_end, req.started_by
            )
        finally:
            await conn.close()
        logger.info("maintenance_start", server=req.server, reason=req.reason,
                    services=req.services, estimated_minutes=req.estimated_minutes)
        return {
            "ok": True,
            "id": row["id"],
            "server": req.server,
            "reason": req.reason,
            "services_paused": req.services,
            "started_at": row["started_at"].isoformat(),
            "estimated_end": row["estimated_end"].isoformat(),
        }
    except Exception as e:
        logger.error("maintenance_start_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ops/maintenance/end")
async def maintenance_end(req: MaintenanceEndRequest):
    """유지보수 모드 종료 — 감시 재개."""
    try:
        conn = await _get_conn()
        try:
            result = await conn.execute(
                "UPDATE maintenance_schedule SET status='ended', actual_end=NOW() "
                "WHERE server=$1 AND status='active'",
                req.server
            )
        finally:
            await conn.close()
        updated = int(result.split()[-1]) if result else 0
        logger.info("maintenance_end", server=req.server, updated=updated)
        return {"ok": True, "server": req.server, "ended_count": updated}
    except Exception as e:
        logger.error("maintenance_end_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ─── Recovery Logs (AADS-132) ─────────────────────────────────────────────────

@router.get("/ops/recovery-logs/stats")
async def recovery_logs_stats():
    """복구 통계 — 이슈 유형별 발생 횟수, 성공률, 평균 복구 시간."""
    try:
        conn = await _get_conn()
        try:
            by_type = await conn.fetch("""
                SELECT
                    issue_type,
                    COUNT(*) AS total,
                    SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS success_count,
                    ROUND(
                        100.0 * SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0),
                        1
                    ) AS success_rate_pct,
                    ROUND(AVG(duration_seconds)::numeric, 1) AS avg_duration_seconds
                FROM escalation_recovery
                GROUP BY issue_type
                ORDER BY total DESC
            """)
            by_tier = await conn.fetch("""
                SELECT tier, COUNT(*) AS total,
                    SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS success_count
                FROM escalation_recovery
                GROUP BY tier ORDER BY tier
            """)
            totals = await conn.fetchrow("""
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS total_success
                FROM escalation_recovery
            """)
        finally:
            await conn.close()
        return {
            "by_issue_type": [dict(r) for r in by_type],
            "by_tier": [dict(r) for r in by_tier],
            "total": int(totals["total"] or 0),
            "total_success": int(totals["total_success"] or 0),
        }
    except Exception as e:
        logger.error("ops_recovery_stats_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ops/maintenance/status")
async def maintenance_status(server: Optional[str] = None):
    """현재 유지보수 상태 조회."""
    try:
        conn = await _get_conn()
        try:
            if server:
                row = await conn.fetchrow(
                    "SELECT * FROM maintenance_schedule "
                    "WHERE server=$1 AND status='active' ORDER BY started_at DESC LIMIT 1",
                    server
                )
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM maintenance_schedule "
                    "WHERE status='active' ORDER BY started_at DESC LIMIT 1"
                )
        finally:
            await conn.close()

        if not row:
            return {"active": False, "server": server}

        return {
            "active": True,
            "server": row["server"],
            "reason": row["reason"],
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "estimated_end": row["estimated_end"].isoformat() if row["estimated_end"] else None,
            "services_paused": list(row["services_paused"]) if row["services_paused"] else [],
            "started_by": row["started_by"],
        }
    except Exception as e:
        logger.error("maintenance_status_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ─── AADS-132: 복구 로그 + 서킷브레이커 API ────────────────────────────────


@router.get("/ops/recovery-logs")
async def list_recovery_logs(
    issue_type: Optional[str] = None,
    result: Optional[str] = None,
    server: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """복구 이력 조회 (issue_type, result, server 필터)."""
    try:
        conn = await _get_conn()
        try:
            conditions, params, idx = [], [], 1
            if issue_type:
                conditions.append(f"issue_type = ${idx}"); params.append(issue_type); idx += 1  # noqa: E702
            if result:
                conditions.append(f"result = ${idx}"); params.append(result); idx += 1  # noqa: E702
            if server:
                conditions.append(f"affected_server = ${idx}"); params.append(server); idx += 1  # noqa: E702
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.extend([limit, offset])
            rows = await conn.fetch(
                f"""
                SELECT id, issue_type, affected_task_id, affected_server,
                       tier, action_taken, result, duration_seconds,
                       recovery_route, error_message, recovered_by, created_at::text
                FROM escalation_recovery {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx+1}
                """,
                *params,
            )
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM escalation_recovery {where}", *params[:-2]
            )
        finally:
            await conn.close()
        return {
            "items": [dict(r) for r in rows],
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error("recovery_logs_list_error", error=str(e))
        raise HTTPException(500, str(e))


@router.get("/ops/circuit-breaker")
async def circuit_breaker_status():
    """서킷브레이커 상태 조회 (3서버)."""
    from app.services.circuit_breaker import get_all_states
    states = await get_all_states()
    return {"circuit_breakers": states}


@router.post("/ops/sync-project-docs")
async def sync_project_docs(request: Request):
    """프로젝트별 중요 문서 링크를 DB에 저장하고 aads-docs 레포에 자동 push."""
    import subprocess
    body = await request.json()
    project_docs = body.get("project_docs")
    if not project_docs or not isinstance(project_docs, dict):
        raise HTTPException(400, "project_docs (object) is required")

    conn = None
    try:
        conn = await _get_conn()
        now = datetime.now(KST).isoformat()

        # 1) DB 저장 (system_memory, category=project_docs)
        for project, docs in project_docs.items():
            project = normalize_project_label(project)
            await conn.execute("""
                INSERT INTO system_memory (category, key, value, updated_by, created_at, updated_at)
                VALUES ('project_docs', $1, $2::jsonb, 'dashboard', NOW(), NOW())
                ON CONFLICT (category, key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW(), updated_by = 'dashboard'
            """, project, json.dumps(docs))

        # 2) aads-docs 레포에 JSON 파일 쓰기 + push
        docs_repo = "/root/aads/aads-docs"
        json_path = f"{docs_repo}/shared/project-docs.json"
        os.makedirs(f"{docs_repo}/shared", exist_ok=True)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "updated_at": now,
                "updated_by": "dashboard",
                "project_docs": project_docs
            }, f, ensure_ascii=False, indent=2)

        # git add + commit + push
        subprocess.run(
            ["git", "-C", docs_repo, "add", "shared/project-docs.json"],
            capture_output=True, text=True, timeout=30
        )
        diff_result = subprocess.run(
            ["git", "-C", docs_repo, "diff", "--cached", "--quiet"],
            capture_output=True, text=True, timeout=10
        )
        git_pushed = False
        commit_sha = ""
        if diff_result.returncode != 0:
            commit_result = subprocess.run(
                ["git", "-C", docs_repo, "commit", "-m",
                 f"[AADS] docs: project-docs.json 자동 업데이트 ({now})"],
                capture_output=True, text=True, timeout=30
            )
            if commit_result.returncode == 0:
                push_result = subprocess.run(
                    ["git", "-C", docs_repo, "push", "origin", "main"],
                    capture_output=True, text=True, timeout=60
                )
                git_pushed = push_result.returncode == 0
                sha_result = subprocess.run(
                    ["git", "-C", docs_repo, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=10
                )
                commit_sha = sha_result.stdout.strip()

        return {
            "ok": True,
            "saved_projects": list(project_docs.keys()),
            "git_pushed": git_pushed,
            "commit_sha": commit_sha,
            "json_path": "shared/project-docs.json",
            "updated_at": now
        }
    except Exception as e:
        logger.error("sync_project_docs_error", error=str(e))
        raise HTTPException(500, str(e))
    finally:
        if conn:
            await conn.close()


@router.post("/ops/sync-trigger-messages")
async def sync_trigger_messages(request: Request):
    """프로젝트별 트리거 메시지를 DB에 저장하고 aads-docs 레포에 자동 push."""
    import subprocess
    body = await request.json()
    trigger_messages = body.get("trigger_messages")
    if not trigger_messages or not isinstance(trigger_messages, dict):
        raise HTTPException(400, "trigger_messages (object) is required")

    conn = None
    try:
        conn = await _get_conn()
        now = datetime.now(KST).isoformat()

        for project, msg in trigger_messages.items():
            project = normalize_project_label(project)
            await conn.execute("""
                INSERT INTO system_memory (category, key, value, updated_by, created_at, updated_at)
                VALUES ('trigger_messages', $1, $2::jsonb, 'dashboard', NOW(), NOW())
                ON CONFLICT (category, key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW(), updated_by = 'dashboard'
            """, project, json.dumps(msg))

        docs_repo = "/root/aads/aads-docs"
        json_path = f"{docs_repo}/shared/trigger-messages.json"
        os.makedirs(f"{docs_repo}/shared", exist_ok=True)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "updated_at": now,
                "updated_by": "dashboard",
                "trigger_messages": trigger_messages
            }, f, ensure_ascii=False, indent=2)

        subprocess.run(
            ["git", "-C", docs_repo, "add", "shared/trigger-messages.json"],
            capture_output=True, text=True, timeout=30
        )
        diff_result = subprocess.run(
            ["git", "-C", docs_repo, "diff", "--cached", "--quiet"],
            capture_output=True, text=True, timeout=10
        )
        git_pushed = False
        commit_sha = ""
        if diff_result.returncode != 0:
            commit_result = subprocess.run(
                ["git", "-C", docs_repo, "commit", "-m",
                 f"[AADS] docs: trigger-messages.json 자동 업데이트 ({now})"],
                capture_output=True, text=True, timeout=30
            )
            if commit_result.returncode == 0:
                push_result = subprocess.run(
                    ["git", "-C", docs_repo, "push", "origin", "main"],
                    capture_output=True, text=True, timeout=60
                )
                git_pushed = push_result.returncode == 0
                sha_result = subprocess.run(
                    ["git", "-C", docs_repo, "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=10
                )
                commit_sha = sha_result.stdout.strip()

        return {
            "ok": True,
            "saved_projects": list(trigger_messages.keys()),
            "git_pushed": git_pushed,
            "commit_sha": commit_sha,
            "updated_at": now
        }
    except Exception as e:
        logger.error("sync_trigger_messages_error", error=str(e))
        raise HTTPException(500, str(e))
    finally:
        if conn:
            await conn.close()


@router.get("/ops/trigger-messages")
async def get_trigger_messages():
    """DB에서 프로젝트별 트리거 메시지 조회."""
    conn = None
    try:
        conn = await _get_conn()
        rows = await conn.fetch(
            "SELECT key, value FROM system_memory WHERE category = 'trigger_messages' ORDER BY key"
        )
        trigger_messages = {}
        for r in rows:
            val = r["value"]
            trigger_messages[r["key"]] = json.loads(val) if isinstance(val, str) else val
        return {"ok": True, "trigger_messages": trigger_messages}
    except Exception as e:
        logger.error("get_trigger_messages_error", error=str(e))
        raise HTTPException(500, str(e))
    finally:
        if conn:
            await conn.close()


@router.get("/ops/project-docs")
async def get_project_docs():
    """DB에서 프로젝트별 중요 문서 링크 조회."""
    conn = None
    try:
        conn = await _get_conn()
        rows = await conn.fetch(
            "SELECT key, value FROM system_memory WHERE category = 'project_docs' ORDER BY key"
        )
        project_docs = {}
        for r in rows:
            project_docs[r["key"]] = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
        return {"ok": True, "project_docs": project_docs}
    except Exception as e:
        logger.error("get_project_docs_error", error=str(e))
        raise HTTPException(500, str(e))
    finally:
        if conn:
            await conn.close()


@router.post("/ops/circuit-breaker/{server}/reset")
async def circuit_breaker_reset(server: str):
    """서킷브레이커 수동 리셋 → closed 상태로 전환."""
    from app.services.circuit_breaker import reset_circuit
    ok = await reset_circuit(server)
    if not ok:
        raise HTTPException(500, f"Circuit breaker reset failed for server {server}")
    return {"ok": True, "server": server, "state": "closed"}


# ─── AADS-181: 3서버 통합 요약 ───────────────────────────────────────────────

@router.get("/ops/server-summary")
async def get_server_summary():
    """
    3대 서버(68/211/114) 요약.
    - 각 서버: pending/running/done 건수 + active claude 세션 수
    - SSH 불가 시 HTTP fallback (cross_server_checker 내부 처리)
    """
    from app.services.cross_server_checker import get_server_summary as _get_server_summary
    try:
        return await _get_server_summary()
    except Exception as e:
        logger.error("server_summary_error", error=str(e))
        raise HTTPException(500, f"서버 요약 조회 실패: {e}")


# ─── AADS-166: 디렉티브 폴더 스캔 (Part 1) ─────────────────────────────────

@router.get("/directives/{status}")
async def get_directives_folder(status: str):
    """디렉티브 폴더 실시간 조회. status: pending|running|done|archived."""
    allowed = {"pending", "running", "done", "archived"}
    if status not in allowed:
        raise HTTPException(400, f"status must be one of {allowed}")
    from app.services.health_checker import scan_directive_folder
    return await scan_directive_folder(status)


# ─── AADS-166: 파이프라인 프로세스 liveness (Part 2) ─────────────────────────

@router.get("/ops/pipeline-status")
async def pipeline_status():
    """파이프라인 프로세스 liveness 체크."""
    from app.services.health_checker import check_pipeline_status
    try:
        return await check_pipeline_status()
    except Exception as e:
        logger.error("pipeline_status_error", error=str(e))
        raise HTTPException(500, str(e))


# ─── AADS-166: 인프라 점검 (Part 3) ─────────────────────────────────────────

@router.get("/ops/infra-check")
async def infra_check():
    """인프라 전체 점검 (DB/GitHub/SSH/디스크/메모리/CPU)."""
    from app.services.health_checker import check_infra
    try:
        return await check_infra()
    except Exception as e:
        logger.error("infra_check_error", error=str(e))
        raise HTTPException(500, str(e))


# ─── AADS-166: 정합성 검증 (Part 4) ─────────────────────────────────────────

@router.get("/ops/consistency-check")
async def consistency_check(auto_fix: bool = Query(False, description="불일치 자동 수정 여부")):
    """정합성 검증 (STATUS↔DB, pending↔큐, commit SHA). auto_fix=true 시 자동 복구."""
    from app.services.health_checker import check_consistency
    try:
        return await check_consistency(auto_fix=auto_fix)
    except Exception as e:
        logger.error("consistency_check_error", error=str(e))
        raise HTTPException(500, str(e))


# ─── AADS-166: 통합 헬스 (Part 5) ───────────────────────────────────────────

@router.get("/ops/full-health")
async def full_health():
    """통합 헬스체크 — Part 1~4 + 기존 health-check 병렬 실행."""
    from app.services.health_checker import full_health_check
    try:
        return await full_health_check()
    except Exception as e:
        logger.error("full_health_error", error=str(e))
        raise HTTPException(500, str(e))


# ─── AADS-166: SSE 실시간 스트리밍 (Part 7) ─────────────────────────────────

_sse_connections = 0
_MAX_SSE_CONNECTIONS = 5


@router.get("/ops/stream")
async def ops_stream():
    """SSE 실시간 스트리밍 — 5초 주기 health/directive/pipeline 이벤트."""
    global _sse_connections
    if _sse_connections >= _MAX_SSE_CONNECTIONS:
        raise HTTPException(429, "최대 SSE 연결 수 초과")

    from app.services.health_checker import quick_health, directive_changes_since, pipeline_quick_status

    async def event_generator():
        global _sse_connections
        _sse_connections += 1
        last_check = datetime.now(tz=timezone(timedelta(hours=9))) - timedelta(seconds=30)
        _cross_server_tick = 0  # 30초마다 cross_server_directives (6 * 5s)
        _cross_server_prev_counts: dict = {}
        try:
            while True:
                # 1) health 이벤트
                try:
                    health = await quick_health()
                    yield f"event: health\ndata: {json.dumps(health, default=str)}\n\n"
                except Exception as e:
                    yield f"event: health\ndata: {json.dumps({'error': str(e)})}\n\n"

                # 2) directive 이벤트
                try:
                    changes = await directive_changes_since(last_check)
                    if changes:
                        yield f"event: directive\ndata: {json.dumps(changes, default=str)}\n\n"
                    last_check = datetime.now(tz=timezone(timedelta(hours=9)))
                except Exception:
                    pass

                # 3) pipeline 이벤트
                try:
                    pipeline = await pipeline_quick_status()
                    yield f"event: pipeline\ndata: {json.dumps(pipeline, default=str)}\n\n"
                except Exception:
                    pass

                # 4) claude_watchdog 이벤트 (AADS-169): 최신 watchdog 보고서 요약
                try:
                    wd_dir = "/root/aads/logs/watchdog_reports"
                    if os.path.isdir(wd_dir):
                        import glob as _wd_glob
                        wd_files = sorted(_wd_glob.glob(os.path.join(wd_dir, "*.json")), reverse=True)
                        if wd_files:
                            with open(wd_files[0], encoding="utf-8") as _wf:
                                wd_data = json.load(_wf)
                            wd_summary = {
                                "generated_at": wd_data.get("generated_at"),
                                "summary": wd_data.get("summary"),
                                "servers": {
                                    sid: {
                                        "scan_ok": sdata.get("scan_ok"),
                                        "process_counts": sdata.get("process_counts"),
                                        "bridge_alive": sdata.get("bridge_alive"),
                                        "auto_trigger_alive": sdata.get("auto_trigger_alive"),
                                    }
                                    for sid, sdata in (wd_data.get("servers") or {}).items()
                                },
                                "issues": wd_data.get("issues", {}).get("all", [])[:5],
                                "cleanup_log": wd_data.get("cleanup_log", [])[:5],
                            }
                            yield f"event: claude_watchdog\ndata: {json.dumps(wd_summary, default=str)}\n\n"
                except Exception:
                    pass

                # 5) cross_server_directives 이벤트 (AADS-181): 30초마다 3서버 스캔 변경 감지
                _cross_server_tick += 1
                if _cross_server_tick >= 6:  # 6 * 5s = 30s
                    _cross_server_tick = 0
                    try:
                        from app.services.cross_server_checker import scan_all_servers
                        cs_data = await scan_all_servers(statuses=["pending", "running"])
                        cs_counts = cs_data.get("counts", {})
                        # 변경 감지: 이전 카운트와 다를 때만 이벤트 발송
                        if cs_counts != _cross_server_prev_counts:
                            _cross_server_prev_counts = cs_counts.copy()
                            cs_event = {
                                "total_count": cs_data.get("total_count", 0),
                                "counts": cs_counts,
                                "by_server": {
                                    sid: {
                                        "reachable": sdata.get("reachable", False),
                                        "pending": sdata.get("counts", {}).get("pending", 0),
                                        "running": sdata.get("counts", {}).get("running", 0),
                                    }
                                    for sid, sdata in cs_data.get("by_server", {}).items()
                                },
                                "scanned_at": cs_data.get("scanned_at"),
                            }
                            yield f"event: cross_server_directives\ndata: {json.dumps(cs_event, default=str)}\n\n"
                    except Exception:
                        pass

                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            _sse_connections -= 1

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── AADS-168: Claude 프로세스 감시 데몬 API ─────────────────────────────────

import glob as _glob # noqa: E402
import subprocess as _subprocess # noqa: E402

_WATCHDOG_LOG_DIR = "/root/aads/logs/watchdog_reports"
_WATCHDOG_SCRIPT = "/root/aads/scripts/claude_watchdog.py"
_SERVER_211_HOST_OPS = get_server_host("contabo14")
_SSH_KEY_OPS = "/root/.ssh/id_ed25519_newtalk"


class ClaudeCleanupRequest(BaseModel):
    server: Optional[str] = None  # "68"|"211"|"114"|None(전체)
    reason: Optional[str] = "manual_ceo_trigger"
    dry_run: bool = False  # True시 스크립트 실행 없이 최신 보고서만 반환 (AADS-169)


class BridgeRestartRequest(BaseModel):
    reason: Optional[str] = "manual_ceo_trigger"


@router.get("/ops/claude-processes")
async def get_claude_processes(limit: int = Query(5, le=20)):
    """최근 watchdog 보고서 조회 (3서버 프로세스 현황, 이슈, 자동정리 이력)."""
    try:
        log_dir = _WATCHDOG_LOG_DIR
        if not os.path.isdir(log_dir):
            return {"ok": True, "reports": [], "message": "watchdog_reports 디렉토리 없음"}

        pattern = os.path.join(log_dir, "*.json")
        files = sorted(_glob.glob(pattern), reverse=True)[:limit]

        reports = []
        for fpath in files:
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
                reports.append({
                    "file": os.path.basename(fpath),
                    "generated_at": data.get("generated_at"),
                    "summary": data.get("summary"),
                    "issues": data.get("issues", {}).get("all", []),
                    "cleanup_log": data.get("cleanup_log", []),
                    "servers": {
                        sid: {
                            "scan_ok": sdata.get("scan_ok"),
                            "process_counts": sdata.get("process_counts"),
                            "running_slots_db": sdata.get("running_slots_db"),
                            "bridge_alive": sdata.get("bridge_alive"),
                            "auto_trigger_alive": sdata.get("auto_trigger_alive"),
                        }
                        for sid, sdata in (data.get("servers") or {}).items()
                    },
                })
            except Exception:
                continue

        return {
            "ok": True,
            "count": len(reports),
            "reports": reports,
        }
    except Exception as e:
        logger.error("get_claude_processes_error", error=str(e))
        raise HTTPException(500, str(e))


@router.post("/ops/claude-cleanup")
async def claude_cleanup(req: ClaudeCleanupRequest):
    """수동 claude_watchdog.py 정리 트리거 (CEO 확인용). dry_run=True시 최신 보고서만 반환."""
    try:
        # AADS-169: dry_run=True → 스크립트 실행 없이 최신 보고서 반환
        if req.dry_run:
            log_dir = _WATCHDOG_LOG_DIR
            latest_report = {}
            if os.path.isdir(log_dir):
                pattern = os.path.join(log_dir, "*.json")
                files = sorted(_glob.glob(pattern), reverse=True)
                if files:
                    try:
                        with open(files[0], encoding="utf-8") as f:
                            latest_report = json.load(f)
                    except Exception:
                        pass
            logger.info("claude_cleanup_dry_run", server=req.server, reason=req.reason)
            return {
                "ok": True,
                "dry_run": True,
                "summary": latest_report.get("summary", {}),
                "issues": latest_report.get("issues", {}).get("all", []),
                "servers": {
                    sid: {
                        "scan_ok": sdata.get("scan_ok"),
                        "process_counts": sdata.get("process_counts"),
                        "bridge_alive": sdata.get("bridge_alive"),
                        "auto_trigger_alive": sdata.get("auto_trigger_alive"),
                    }
                    for sid, sdata in (latest_report.get("servers") or {}).items()
                },
                "generated_at": latest_report.get("generated_at"),
                "reason": req.reason,
                "server_filter": req.server,
            }

        env = os.environ.copy()
        # watchdog에 필요한 env 주입 (DB, Telegram)
        env_file = "/root/aads/aads-server/.env"
        if os.path.isfile(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))

        cmd = ["python3", _WATCHDOG_SCRIPT]
        proc = _subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=90, env=env,
            cwd="/root/aads"
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr[-500:].strip() if proc.stderr else ""

        summary = {}
        if stdout:
            try:
                summary = json.loads(stdout)
            except Exception:
                summary = {"raw_output": stdout[:500]}

        logger.info(
            "claude_cleanup_manual",
            server=req.server, reason=req.reason,
            returncode=proc.returncode, summary=summary
        )
        return {
            "ok": proc.returncode == 0,
            "dry_run": False,
            "returncode": proc.returncode,
            "summary": summary,
            "stderr": stderr,
            "reason": req.reason,
            "server_filter": req.server,
        }
    except _subprocess.TimeoutExpired:
        logger.error("claude_cleanup_timeout")
        raise HTTPException(504, "watchdog 실행 타임아웃 (90초)")
    except Exception as e:
        logger.error("claude_cleanup_error", error=str(e))
        raise HTTPException(500, str(e))


@router.post("/ops/bridge-restart")
async def bridge_restart(req: BridgeRestartRequest):
    """bridge.py 원격 재시작 (contabo14 SSH)."""
    try:
        ssh_cmd = [
            "ssh",
            "-i", _SSH_KEY_OPS,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            f"root@{_SERVER_211_HOST_OPS}",
            "nohup python3 /root/aads/scripts/bridge.py >> /root/aads/logs/bridge.log 2>&1 &",
        ]
        result = _subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=20
        )
        ok = result.returncode == 0

        # 재시작 후 bridge.py 실제 실행 확인
        import time
        time.sleep(2)
        chk_cmd = [
            "ssh",
            "-i", _SSH_KEY_OPS,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"root@{_SERVER_211_HOST_OPS}",
            "pgrep -f bridge.py",
        ]
        chk = _subprocess.run(chk_cmd, capture_output=True, text=True, timeout=10)
        bridge_alive = chk.returncode == 0 and chk.stdout.strip() != ""

        logger.info(
            "bridge_restart",
            reason=req.reason,
            ssh_ok=ok,
            bridge_alive=bridge_alive
        )
        return {
            "ok": ok,
            "bridge_alive": bridge_alive,
            "returncode": result.returncode,
            "stderr": result.stderr[:300] if result.stderr else "",
            "reason": req.reason,
            "server": "211",
        }
    except _subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH 타임아웃")
    except Exception as e:
        logger.error("bridge_restart_error", error=str(e))
        raise HTTPException(500, str(e))


# ─── OAuth 사용량 추적 API (AADS-192) ────────────────────────────────────
@router.get("/ops/usage-stats")
async def get_oauth_usage_stats():
    """OAuth 사용량 통계 — 5시간/1주일 롤링 윈도우 + rate-limit 상태."""
    from app.services.oauth_usage_tracker import get_usage_stats
    return await get_usage_stats()


@router.get("/ops/codex-usage")
async def get_codex_cli_usage():
    """Codex CLI 5시간/주간 한도 — Claude Relay의 codex app-server 조회를 프록시."""
    headers = {}
    secret = _load_relay_secret()
    if secret:
        headers["X-Claude-Relay-Secret"] = secret

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=2.0)) as client:
            resp = await client.get(f"{_CLAUDE_RELAY_URL}/codex-usage", headers=headers)
    except httpx.TimeoutException:
        return _codex_usage_fallback(
            "codex_usage_timeout",
            "Codex CLI 한도 조회가 시간 초과되었습니다.",
        )
    except httpx.HTTPError as exc:
        return _codex_usage_fallback("codex_usage_relay_unreachable", str(exc)[:200])

    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": "invalid_relay_response", "detail": resp.text[:200]}

    normalized = _normalize_codex_usage_payload(payload if isinstance(payload, dict) else {})
    if resp.status_code >= 400:
        return _codex_usage_fallback(
            normalized.get("error") or f"relay_http_{resp.status_code}",
            normalized.get("detail"),
        )
    if not normalized.get("ok") or not normalized.get("limits"):
        return _codex_usage_fallback(
            normalized.get("error") or "relay_empty_limits",
            normalized.get("detail"),
        )
    return normalized


@router.get("/ops/claude-accounts")
async def get_claude_accounts():
    """클로드 슬롯 목록과 현재 주계정.

    대시보드의 계정 전환 UI 가 이 경로를 부르는데 **엔드포인트가 없어 404 였다**
    (2026-09-16 확인). 프론트가 try/catch 로 삼켜 빈 카드로 보였다.
    슬롯 자격증명은 호스트 파일이라 릴레이가 안다 — 여기서는 프록시만 한다.
    설계: aads-docs/docs/PRD-SETTINGS-UNIFIED-ACCOUNT-CARD-v1.0.md
    """
    headers = {}
    secret = _load_relay_secret()
    if secret:
        headers["X-Claude-Relay-Secret"] = secret
    accounts = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0)) as client:
            resp = await client.get(f"{_CLAUDE_RELAY_URL}/account-bindings", headers=headers)
            health = await client.get(f"{_CLAUDE_RELAY_URL}/health", headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": "relay_unreachable", "detail": str(exc)[:200], "accounts": []}

    for b in (resp.json().get("bindings", []) if resp.status_code < 400 else []):
        target = str(b.get("target", ""))
        if not target.startswith("claude:"):
            continue
        accounts.append({
            "id": int(target.split(":", 1)[1]),
            "label": b.get("account", ""),
            "subscription": b.get("subscription"),
            "has_token": bool(b.get("bound")),
            "needs_login": bool(b.get("needs_login")),
        })

    current = 1
    try:
        current = int((health.json() or {}).get("current_oauth") or 1)
    except Exception:
        pass
    return {"ok": True, "accounts": accounts, "current_account": current}


class ClaudeAccountSwitch(BaseModel):
    account: int


@router.post("/ops/claude-account/switch")
async def switch_claude_account(body: ClaudeAccountSwitch):
    """주계정 전환 — 채팅이 실제로 이 계정을 먼저 쓰게 만든다.

    예전에는 릴레이의 CURRENT_OAUTH 만 바꿨는데, 그것은 호출자가 슬롯을
    지정하지 않았을 때 쓰는 릴레이의 기본값일 뿐이다. 채팅은 슬롯을 항상
    명시해서 부르고(model_selector), 그 순서는 DB priority 가 정한다. 그래서
    버튼을 눌러도 채팅이 쓰는 계정은 그대로였다 — 2026-09-17 대표님 지적.

    이제 DB priority 를 바꾸는 것이 본체이고, 릴레이 기본값 동기화는 곁가지다.
    자격증명이 없는 슬롯은 릴레이가 409 로 막으므로 그것만 하드 실패로 본다.
    """
    slot = str(body.account)
    headers = {"Content-Type": "application/json"}
    secret = _load_relay_secret()
    if secret:
        headers["X-Claude-Relay-Secret"] = secret

    # 1) 자격증명 확인 + 릴레이 기본값 동기화 (닿지 않아도 전환은 계속한다)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0)) as client:
            resp = await client.post(f"{_CLAUDE_RELAY_URL}/oauth/switch",
                                     headers=headers, json={"slot": slot})
        if resp.status_code == 409:
            detail = "자격증명 없음"
            try:
                detail = resp.json().get("error") or detail
            except ValueError:
                pass
            return {"ok": False, "detail": detail}
        if resp.status_code >= 400:
            logger.warning("claude_account_switch_relay_warn",
                           slot=slot, status=resp.status_code, body=resp.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("claude_account_switch_relay_unreachable", slot=slot, error=str(exc)[:160])

    # 2) 여기가 실제로 채팅을 바꾸는 곳이다.
    from app.core.auth_provider import get_oauth_key_records_async, set_token_order_async

    if not await set_token_order_async(f"slot{slot}"):
        return {"ok": False, "detail": f"슬롯 {slot} 계정을 찾지 못했다"}

    records = await get_oauth_key_records_async(include_rate_limited=True)
    order = [
        {"slot": r.get("slot", ""), "label": r.get("label", "")}
        for r in records if r.get("slot")
    ]
    label = next((r["label"] for r in order if r["slot"] == slot), f"slot{slot}")
    return {"ok": True, "current_account": body.account, "label": label, "order": order}


# ─── 회사별 계정(슬롯) 배정 ────────────────────────────────────────────
#
# 배정은 "우선" 이지 "전용" 이 아니다 — 배정 슬롯이 막히면 배정 없는 슬롯으로
# 내려간다(app/services/slot_projects.py). 화면도 그렇게 설명해야 한다.
class CompanySlotAssign(BaseModel):
    slot: Optional[str] = None


@router.get("/ops/oauth-slot-projects")
async def get_oauth_slot_projects():
    """회사별 계정 배정 현황 — 설정 화면이 이 한 벌로 표를 그린다."""
    from app.core.auth_provider import LAST_RESORT_SLOTS, get_oauth_key_records_async
    from app.core.db_pool import get_pool
    from app.services.slot_projects import slot_project_map

    mapping = await slot_project_map(force=True)
    company_slot: Dict[str, str] = {}
    for slot, keys in mapping.items():
        for key in keys:
            company_slot[str(key).upper()] = str(slot)

    companies: List[Dict[str, Any]] = []
    try:
        rows = await get_pool().fetch(
            "SELECT DISTINCT ON (project_key) project_key, name, created_at "
            "FROM chat_workspaces WHERE COALESCE(project_key, '') <> '' "
            "ORDER BY project_key, created_at"
        )
        companies = [
            {
                "project_key": str(r["project_key"]).upper(),
                "name": r["name"] or str(r["project_key"]),
                "slot": company_slot.get(str(r["project_key"]).upper(), ""),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("oauth_slot_projects_companies_failed", error=str(exc)[:160])

    accounts: List[Dict[str, Any]] = []
    try:
        for record in await get_oauth_key_records_async(include_rate_limited=True):
            slot = str(record.get("slot", "") or "")
            if not slot:
                continue
            accounts.append({
                "slot": slot,
                "label": record.get("label") or f"slot{slot}",
                "key_name": record.get("key_name", ""),
                "priority": record.get("priority", 0),
                "last_resort": slot in LAST_RESORT_SLOTS,
                "rate_limited": bool(record.get("rate_limited_until")),
            })
        accounts.sort(key=lambda a: (a["last_resort"], a["priority"], a["slot"]))
    except Exception as exc:
        logger.warning("oauth_slot_projects_accounts_failed", error=str(exc)[:160])

    return {"ok": True, "companies": companies, "accounts": accounts}


@router.put("/ops/oauth-slot-projects/{project_key}")
async def set_company_oauth_slot(project_key: str, body: CompanySlotAssign):
    """이 회사가 먼저 쓸 계정을 정한다. slot 이 비면 자동 순서로 되돌린다."""
    from app.services.slot_projects import set_company_slot

    slot = (body.slot or "").strip()
    if slot and not re.fullmatch(r"[0-9]{1,3}", slot):
        raise HTTPException(status_code=400, detail="slot 은 숫자여야 한다")
    result = await set_company_slot(project_key, slot or None, by="CEO")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "배정 실패"))
    return result


# ─── 주계정: 수동 선택 / 자동 규칙 ────────────────────────────────────
class AccountPrimaryRequest(BaseModel):
    provider: str
    mode: Optional[str] = None       # auto | manual
    key_name: Optional[str] = None   # manual 일 때 고른 계정


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


@router.get("/ops/account-primary")
async def get_account_primary():
    """provider 별 주계정 + 모드. 설정 화면의 자동/수동 토글이 이걸 본다."""
    from app.services import account_primary

    out: Dict[str, Any] = {"ok": True, "providers": {}}
    for provider in account_primary.PROVIDERS:
        try:
            mode = await account_primary.get_mode(provider)
            accounts = await account_primary.usage_view(provider)
        except Exception as exc:
            logger.warning("account_primary_view_failed", provider=provider, error=str(exc)[:160])
            out["providers"][provider] = {"mode": "manual", "primary": "", "accounts": []}
            continue
        out["providers"][provider] = {
            "mode": mode,
            "primary": accounts[0]["key_name"] if accounts else "",
            "accounts": [{
                "key_name": a["key_name"],
                "label": a["label"],
                "priority": a["priority"],
                "is_active": a["is_active"],
                "has_quota": a["has_quota"],
                "headroom_pct": a["headroom_pct"],
                "resets_at": _iso(a["resets_at"]),
                "rate_limited_until": _iso(a["rate_limited_until"]),
            } for a in accounts],
        }
    return out


@router.post("/ops/account-primary/reconcile")
async def reconcile_account_primary():
    """자동 모드인 provider 만 규칙대로 다시 세운다. 2분 크론이 부른다."""
    from app.services import account_primary

    results = []
    for provider in account_primary.PROVIDERS:
        try:
            results.append(await account_primary.reconcile(provider))
        except Exception as exc:
            logger.warning("account_primary_reconcile_failed",
                           provider=provider, error=str(exc)[:160])
            results.append({"provider": provider, "error": str(exc)[:160]})
    return {"ok": True, "results": results}


@router.post("/ops/account-primary")
async def set_account_primary(body: AccountPrimaryRequest):
    """수동이면 고른 계정을 1순위로, 자동이면 규칙대로 즉시 다시 세운다."""
    from app.services import account_primary

    provider = (body.provider or "").strip().lower()
    if provider not in account_primary.PROVIDERS:
        raise HTTPException(status_code=400, detail="provider 는 anthropic 또는 codex 다")

    mode = (body.mode or "").strip().lower()
    if mode == account_primary.AUTO:
        await account_primary.set_mode(provider, account_primary.AUTO)
        result = await account_primary.reconcile(provider)
        return {"ok": True, **result}

    key_name = (body.key_name or "").strip()
    if not key_name:
        raise HTTPException(status_code=400, detail="수동 전환에는 key_name 이 필요하다")
    result = await account_primary.set_manual(provider, key_name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "전환 실패"))

    # 클로드는 릴레이 기본값도 같이 맞춘다. 릴레이가 안 떠 있어도 DB 가 본체라
    # 전환 자체는 유효하므로 실패를 삼킨다.
    if provider == "anthropic":
        try:
            from app.core.auth_provider import _sync_relay_current_slot, get_oauth_key_records_async

            records = await get_oauth_key_records_async(include_rate_limited=True)
            slot = next((r.get("slot", "") for r in records
                         if r.get("key_name") == key_name), "")
            if slot:
                await _sync_relay_current_slot(str(slot))
        except Exception as exc:
            logger.warning("account_primary_relay_sync_failed", error=str(exc)[:160])
    return result


# ─── 계정별 LLM 사용량 현황 API (AADS-190C) ──────────────────────────────
@router.get("/ops/account-usage")
async def get_llm_account_usage():
    """계정별 LLM 키 상태 + exact/observed usage 집계."""
    from app.services.llm_account_usage import get_account_usage_snapshot

    return await get_account_usage_snapshot()


# ─── Claude Max 사용량 API ─────────────────────────────────────────────
_CLAUDE_MAX_CACHE = {"ts": 0, "payload": None}
_CLAUDE_MAX_TTL = int(os.getenv("CLAUDE_MAX_USAGE_TTL_SEC", "60"))


@router.get("/ops/claude-max-usage")
async def get_claude_max_usage():
    """Claude Max 5h/1w 사용량 — Codex /codex-usage 호환 포맷."""
    import time as _t
    now = _t.time()
    cached = _CLAUDE_MAX_CACHE.get("payload")
    cache_ts = _CLAUDE_MAX_CACHE.get("ts", 0)
    if cached and (now - cache_ts) < _CLAUDE_MAX_TTL:
        return {"cached": True, "age_sec": round(now - cache_ts, 1),
                "ttl_sec": _CLAUDE_MAX_TTL, **cached}
    from app.services.oauth_usage_tracker import get_claude_max_usage as _get
    payload = await _get()
    _CLAUDE_MAX_CACHE["payload"] = payload
    _CLAUDE_MAX_CACHE["ts"] = now
    return {"cached": False, "ttl_sec": _CLAUDE_MAX_TTL, **payload}


# ─── 도구 오류율 통계 API (AADS-206) ─────────────────────────────────────
@router.get("/ops/tool-stats")
async def get_tool_stats(hours: int = Query(24, ge=1, le=168)):
    """도구별 성공/실패 통계 — 최근 N시간 (기본 24h, 최대 168h)."""
    from app.services.tool_archive import get_tool_error_stats
    stats = await get_tool_error_stats(hours)
    total_calls = sum(s["total"] for s in stats)
    total_errors = sum(s["errors"] for s in stats)
    return {
        "period_hours": hours,
        "total_calls": total_calls,
        "total_errors": total_errors,
        "stats": stats,
    }


@router.get("/ops/llm-response-metrics")
async def get_llm_response_metrics_endpoint(
    hours: int = Query(24, ge=1, le=168),
    model: str = Query("", max_length=120),
):
    """채팅/백그라운드 LLM/Claude·Codex CLI 응답속도 통합 지표."""
    from app.services.llm_response_metrics import get_llm_response_metrics

    return await get_llm_response_metrics(hours=hours, model=model)


@router.get("/ops/prompt-profile")
async def get_prompt_profile():
    """시스템 프롬프트 섹션별 토큰 프로파일 조회."""
    from app.core.prompts.token_profiler import profile_sections, profile_all_workspaces
    return {
        "sections": profile_sections(),
        "workspaces": profile_all_workspaces(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# AADS-AAG-DEBT-001 — 대시보드가 부르는데 없던 /ops 조회 엔드포인트 6종
#
# 2026-09-16 AAG 스캔이 ROUTE_MISSING(P0) 으로 잡은 6건이다. 호출부가 전부
# `Promise.allSettled` + `res.ok` 검사라 404 가 예외로 올라오지 않고 화면만 조용히
# 비었다 — 콘솔에도 안 남는다. 인증 미들웨어가 라우팅보다 먼저 401 을 돌려주므로
# HTTP 로도 404 와 구분되지 않았다.
#
# 응답은 호출부의 `Array.isArray(d) ? d : d.items || []` 관례에 맞춰
# 전부 {"items": [...]} 로 통일한다 (ArtifactChart.tsx:144 실측).
#
# 아래 순수 함수(파라미터 클램프·행 직렬화)는 DB 없이 단위테스트한다 —
# tests/unit/test_ops_dashboard_endpoints.py.
# ══════════════════════════════════════════════════════════════════════════════

# pipeline_jobs 는 큐 테이블이고 끝난 작업은 pipeline_cleanup 이
# pipeline_jobs_archive 로 옮긴다(migrations/20260915_pipeline_jobs_archive.sql —
# "프로젝트별 성공률을 pipeline_jobs 로 산출할 방법이 없다"). 큐만 세면 completed 가
# 구조적으로 0 이 되므로 집계·이력은 두 테이블을 job_id 로 중복 제거해 합쳐 본다.
_PIPELINE_DONE_STATUSES = frozenset({"done", "completed", "success"})
_PIPELINE_FAILED_STATUSES = frozenset({"error", "failed", "rejected", "rejected_done"})
_PIPELINE_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})

# code_reviews.verdict 는 APPROVE / FLAG / REQUEST_CHANGES 세 값이다(실측 16,501행).
# /ops 화면은 verdict === "PASS" 만 통과로 그리므로 여기서 정규화해 넘긴다.
# 원본은 raw_verdict 로 같이 실어 보내 판정 근거를 잃지 않는다.
_QA_PASS_VERDICTS = frozenset({"APPROVE", "APPROVED", "PASS", "PASS_TIMEOUT"})

_CIRCUIT_TO_SERVER_STATUS = {"closed": "ok", "half_open": "warn", "open": "error"}
_CIRCUIT_SEVERITY = {"closed": 1, "half_open": 2, "open": 3}

# 지시서 머리말에서 제목을 뽑을 때 쓴다. `_pipeline_title` 참고.
_TITLE_TOKEN_RE = re.compile(r"TITLE:[ \t]*(.+)", re.IGNORECASE)
_INSTRUCTION_KEY_RE = re.compile(
    r"\s+(?:PRIORITY|SIZE|TASK_ID|PARENT|ASSIGNEE|DUE|OWNER|LABELS)\s*:", re.IGNORECASE
)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """범위를 벗어난 값은 422 로 막지 않고 잘라낸다.

    이 6개는 위젯이 고정 파라미터로 부르는 조회 전용 경로다. 상한을 넘겼다고
    422 를 돌려주면 호출부가 `res.ok` 만 보고 다시 조용히 빈 화면이 된다 —
    고치려던 증상 그대로다. 잘라내고 실제 적용값을 응답에 실어 보낸다.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


def _iso_or_none(value: Any) -> Optional[str]:
    """타임스탬프를 KST ISO 문자열로. 이미 문자열이면 그대로 둔다."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.astimezone(KST).isoformat()
    except Exception:
        return str(value)


def _coerce_json(value: Any) -> Any:
    """jsonb 컬럼. asyncpg 는 코덱 설정에 따라 str 로도 dict 로도 준다."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _truncate(text: str, max_len: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _cost_trend_items(rows: List[Dict[str, Any]], days: int, today) -> List[Dict[str, Any]]:
    """일자별 비용을 빈 날 0 으로 메워 days 개 연속 점으로 만든다.

    LineChart 는 점 배열을 그대로 그린다 — 비용이 0 인 날을 빼면 x 축이
    소리 없이 압축돼 추이가 왜곡된다.
    """
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("day"))[:10]
        by_day[key] = row
    items: List[Dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        hit = by_day.get(key)
        items.append({
            "date": key,
            "cost": round(float(hit.get("cost") or 0), 6) if hit else 0.0,
            "records": int(hit.get("records") or 0) if hit else 0,
        })
    return items


def _project_stat_items(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """(project, status, cnt) 행을 프로젝트별 건수/상태 분포로 접는다."""
    acc: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        project = (row.get("project") or "UNKNOWN").strip() or "UNKNOWN"
        status = (row.get("status") or "unknown").strip().lower() or "unknown"
        cnt = int(row.get("cnt") or 0)
        entry = acc.setdefault(project, {
            "project": project, "total": 0, "completed": 0,
            "failed": 0, "cancelled": 0, "active": 0, "by_status": {},
        })
        entry["total"] += cnt
        entry["by_status"][status] = entry["by_status"].get(status, 0) + cnt
        if status in _PIPELINE_DONE_STATUSES:
            entry["completed"] += cnt
        elif status in _PIPELINE_FAILED_STATUSES:
            entry["failed"] += cnt
        elif status in _PIPELINE_CANCELLED_STATUSES:
            entry["cancelled"] += cnt
        else:
            entry["active"] += cnt
    return sorted(acc.values(), key=lambda e: (-e["total"], e["project"]))


def _pipeline_title(instruction: Optional[str], max_len: int = 120) -> str:
    """지시서 본문에서 제목 한 줄을 뽑는다.

    러너 지시서 머리말은 두 형태로 들어온다(둘 다 실측):
      - 줄바꿈형  `TASK_ID: X\\nTITLE: Y\\nPRIORITY: ...`  (아카이브 행)
      - 한 줄형   `TASK_ID: X TITLE: Y PRIORITY: ...`      (현재 큐 행)
    그래서 줄머리 매칭만으로는 한 줄형에서 제목을 통째로 놓친다. TITLE 토큰을
    찾아 다음 키 앞에서 끊고, 없으면 첫 비어 있지 않은 줄로 떨어진다.

    정규식은 둘 다 중첩 반복이 없다 — 지시서 본문은 길고, 중첩 반복을 쓰면
    백트래킹이 폭발한다(R-BG 3).
    """
    text = instruction or ""
    fallback = ""
    for line in text.splitlines()[:10]:
        stripped = line.strip()
        if not stripped:
            continue
        match = _TITLE_TOKEN_RE.search(stripped)
        if match:
            title = match.group(1)
            cut = _INSTRUCTION_KEY_RE.search(title)
            if cut:
                title = title[: cut.start()]
            title = title.strip()
            if title:
                return _truncate(title, max_len)
        if not fallback:
            fallback = stripped.lstrip("#").strip()
    return _truncate(fallback, max_len)


def _pipeline_history_items(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "task_id": row.get("job_id"),
        "title": _pipeline_title(row.get("instruction")),
        "status": (row.get("status") or "unknown"),
        "project": (row.get("project") or "UNKNOWN"),
        "phase": row.get("phase"),
        "created_at": _iso_or_none(row.get("created_at")),
        "completed_at": _iso_or_none(row.get("completed_at")),
        "source": row.get("source") or "live",
    } for row in rows]


def _qa_detail(feedback: Any, flag_category: Optional[str], max_len: int = 300) -> str:
    """리뷰 피드백에서 사람이 읽을 한 줄. 없으면 flag_category 로 떨어진다."""
    obj = _coerce_json(feedback)
    text = ""
    if isinstance(obj, dict):
        issues = obj.get("issues")
        if isinstance(issues, list) and issues:
            text = str(issues[0])
        elif obj.get("summary"):
            text = str(obj["summary"])
        elif obj.get("reason"):
            text = str(obj["reason"])
    elif isinstance(obj, list) and obj:
        text = str(obj[0])
    if not text:
        text = flag_category or ""
    return _truncate(text, max_len)


def _qa_result_items(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in rows:
        raw_verdict = (row.get("verdict") or "").strip().upper()
        cycle = int(row.get("review_cycle") or 1)
        score = row.get("score")
        items.append({
            "task_id": row.get("job_id"),
            "project": (row.get("project") or "UNKNOWN"),
            "verdict": "PASS" if raw_verdict in _QA_PASS_VERDICTS else "FAIL",
            "raw_verdict": raw_verdict or None,
            "score": float(score) if score is not None else None,
            "retry_count": max(cycle - 1, 0),
            "needs_retry": bool(row.get("needs_retry")),
            "flag_category": row.get("flag_category"),
            "detail": _qa_detail(row.get("feedback"), row.get("flag_category")),
            "created_at": _iso_or_none(row.get("created_at")),
        })
    return items


def _screenshot_url(after_path: Optional[str]) -> Optional[str]:
    """브라우저가 실제로 읽을 수 있는 경로만 screenshot_url 로 내보낸다.

    design_reviews.after_path 는 서버 파일시스템 경로일 수 있고, 그대로 <img src>
    에 넣으면 깨진 이미지가 뜬다. 정적 경로/절대 URL 일 때만 준다.
    """
    path = (after_path or "").strip()
    if not path:
        return None
    if path.startswith(("http://", "https://", "/static/")):
        return path
    return None


def _design_review_items(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in rows:
        issues = _coerce_json(row.get("issues_json")) or []
        detail = ""
        if isinstance(issues, list) and issues:
            detail = _truncate(str(issues[0]), 300)
        elif isinstance(issues, dict):
            detail = _truncate(json.dumps(issues, ensure_ascii=False), 300)
        cost = row.get("cost_usd")
        item = {
            "id": row.get("id"),
            "task_id": row.get("task_id"),
            "project": (row.get("project") or "UNKNOWN"),
            "verdict": (row.get("verdict") or "PENDING").strip().upper(),
            "page_url": row.get("page_url"),
            "before_path": row.get("before_path"),
            "after_path": row.get("after_path"),
            "reviewer_model": row.get("reviewer_model"),
            "cost_usd": float(cost) if cost is not None else None,
            "issues": issues,
            "scores": _coerce_json(row.get("scores_json")) or {},
            "detail": detail,
            "created_at": _iso_or_none(row.get("created_at")),
        }
        url = _screenshot_url(row.get("after_path"))
        if url:
            item["screenshot_url"] = url
        items.append(item)
    return items


def _worst_circuit(circuit_states: Optional[List[Dict[str, Any]]], threshold: int) -> Dict[str, Any]:
    """위젯은 서킷브레이커를 한 덩어리로 그린다 — 가장 나쁜 서버를 대표로 올린다."""
    worst: Optional[Dict[str, Any]] = None
    worst_rank = -1
    for state in circuit_states or []:
        name = (state.get("state") or "closed").strip().lower()
        rank = _CIRCUIT_SEVERITY.get(name, 0)
        if rank > worst_rank:
            worst_rank = rank
            worst = {
                "server": state.get("server"),
                "state": name,
                "fail_count": int(state.get("failure_count") or 0),
            }
    if worst is None:
        worst = {"server": None, "state": "closed", "fail_count": 0}
    worst["threshold"] = threshold
    return worst


def _ops_status_servers(
    health: Optional[Dict[str, Any]],
    circuit_states: Optional[List[Dict[str, Any]]],
    servers_meta: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """서버 카드. 프로브를 새로 쏘지 않고 health-check + 서킷브레이커만 읽는다.

    위젯이 30초마다 폴링하므로 여기서 SSH 프로브를 돌리면 폴링 주기마다
    3대에 접속하게 된다. 값이 없는 칸은 키 자체를 빼야 한다 —
    호출부가 `srv.cpu !== undefined` 로 판정해서 null 을 넣으면 "null%" 가 뜬다.
    """
    by_server: Dict[str, Dict[str, Any]] = {}
    for state in circuit_states or []:
        key = str(state.get("server") or "").strip()
        if key:
            by_server[key] = state

    health = health or {}
    infra = health.get("infra") or {}
    out: List[Dict[str, Any]] = []
    for meta in servers_meta:
        entry: Dict[str, Any] = {
            "id": meta.get("id"),
            "name": meta.get("display_name") or meta.get("id"),
            "ip": meta.get("host"),
            "status": "unknown",
        }
        if meta.get("type") == "local":
            if health.get("error"):
                entry["status"] = "error"
            elif health.get("pipeline_healthy"):
                entry["status"] = "ok"
            else:
                entry["status"] = "warn"
            if infra.get("memory_pct") is not None:
                entry["mem"] = infra["memory_pct"]
            if infra.get("disk_pct") is not None:
                entry["disk"] = infra["disk_pct"]
            if infra.get("load_1m") is not None:
                entry["load_1m"] = infra["load_1m"]
        else:
            state = by_server.get(str(meta.get("id")))
            if state is None:
                for legacy in meta.get("legacy_ids") or []:
                    if str(legacy) in by_server:
                        state = by_server[str(legacy)]
                        break
            if state is not None:
                name = (state.get("state") or "").strip().lower()
                entry["status"] = _CIRCUIT_TO_SERVER_STATUS.get(name, "unknown")
                entry["fail_count"] = int(state.get("failure_count") or 0)
        out.append(entry)
    return out


def _ops_status_payload(
    health: Optional[Dict[str, Any]],
    circuit_states: Optional[List[Dict[str, Any]]],
    servers_meta: List[Dict[str, Any]],
    threshold: int,
) -> Dict[str, Any]:
    health = health or {}
    return {
        "servers": _ops_status_servers(health, circuit_states, servers_meta),
        "circuit_breaker": _worst_circuit(circuit_states, threshold),
        "circuit_breakers": list(circuit_states or []),
        "pipeline_healthy": bool(health.get("pipeline_healthy")),
        "maintenance_active": bool(health.get("maintenance_active")),
        "stalled_count": int(health.get("stalled_count") or 0),
        "active_count": int(health.get("active_count") or 0),
        "running_count": int(health.get("running_count") or 0),
        "completed_today": int(health.get("completed_today") or 0),
        "error_count": int(health.get("error_count") or 0),
        "issues": health.get("issues") or [],
        "checked_at": health.get("checked_at") or datetime.now(KST).isoformat(),
    }


def _server_meta_list() -> List[Dict[str, Any]]:
    """server_registry 정본에서 카드 3장을 만든다. 별칭 중복 집계를 피해
    CANONICAL_SERVER_IDS 만 순회한다(레지스트리 직접 순회 금지)."""
    from app.services.server_registry import CANONICAL_SERVER_IDS

    metas: List[Dict[str, Any]] = []
    for sid in CANONICAL_SERVER_IDS:
        cfg = get_server_config(sid) or {}
        metas.append({
            "id": sid,
            "host": cfg.get("host"),
            "display_name": cfg.get("display_name") or sid,
            "type": cfg.get("type", "ssh"),
            "legacy_ids": cfg.get("legacy_ids") or [],
        })
    return metas


@router.get("/ops/cost-trend")
async def ops_cost_trend(
    days: int = Query(7, description="집계 일수 (1~90, 벗어나면 잘라낸다)"),
    project: Optional[str] = None,
):
    """일자별 비용 추이 — 채팅 아티팩트 비용 차트(ArtifactChart)."""
    days = _clamp_int(days, default=7, minimum=1, maximum=90)
    project_label = normalize_project_label(project) if project else None

    # 어느 테이블을 읽느냐가 이 엔드포인트의 전부다.
    #
    # `cost_tracking` 은 **2026-03-11 이후 한 행도 늘지 않았다**(총 220행).
    # `task_cost_log` 도 2026-03-05 에서 멈췄고, `llmops_traces` 는 7일간
    # 50,986 트레이스가 쌓이지만 `cost_usd` 가 전부 0 이다.
    # 지금 실제 비용이 적히는 곳은 `oauth_usage_log` 하나뿐이다
    # (최근 2일 1,303행 · 합계 $1,732.50, 2026-09-16 실측).
    #
    # 여기서 `cost_tracking` 을 읽으면 엔드포인트는 200 을 주고 차트는
    # 7일 내내 0 을 그린다 — **고장인데 고장으로 안 보이는** 형태다.
    # 그래서 기본 경로는 살아 있는 원장을 읽는다.
    #
    # 다만 `oauth_usage_log` 에는 project 컬럼이 없다(계정 슬롯·모델 단위 기록).
    # 프로젝트별 비용을 물으면 그 축을 가진 유일한 테이블인 `cost_tracking`
    # 으로 간다. 그 값이 3월에 멈춰 있다는 사실은 응답에 그대로 적어 보낸다 —
    # 조용히 0 을 돌려주는 것보다 낫다.
    now_kst = datetime.now(KST)
    if project_label:
        source = "cost_tracking"
        sql = """
            SELECT DATE(recorded_at AT TIME ZONE 'Asia/Seoul') AS day,
                   COALESCE(SUM(cost_usd), 0) AS cost,
                   COUNT(*)::int AS records
            FROM cost_tracking
            WHERE recorded_at >= (date_trunc('day', NOW() AT TIME ZONE 'Asia/Seoul')
                  - (INTERVAL '1 day' * ($1::int - 1))) AT TIME ZONE 'Asia/Seoul'
              AND project = $2
            GROUP BY day ORDER BY day
        """
        params: list = [days, project_label]
    else:
        source = "oauth_usage_log"
        sql = """
            SELECT DATE(created_at AT TIME ZONE 'Asia/Seoul') AS day,
                   COALESCE(SUM(cost_usd), 0) AS cost,
                   COUNT(*)::int AS records
            FROM oauth_usage_log
            WHERE created_at >= (date_trunc('day', NOW() AT TIME ZONE 'Asia/Seoul')
                  - (INTERVAL '1 day' * ($1::int - 1))) AT TIME ZONE 'Asia/Seoul'
            GROUP BY day ORDER BY day
        """
        params = [days]

    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(sql, *params)
        finally:
            await conn.close()
    except Exception as e:
        logger.error("ops_cost_trend_error", error=str(e), source=source)
        raise HTTPException(status_code=500, detail=str(e))

    items = _cost_trend_items([dict(r) for r in rows], days, now_kst.date())
    payload = {
        "items": items,
        "days": days,
        "project": project_label,
        "source": source,
        "total_usd": round(sum(i["cost"] for i in items), 6),
        "generated_at": now_kst.isoformat(),
    }
    if project_label:
        payload["source_note"] = (
            "프로젝트별 비용은 cost_tracking 에만 축이 있는데 이 테이블은 "
            "2026-03-11 이후 기록이 멈췄다. 최근 값이 0 이면 비용이 0 이 아니라 "
            "기록이 없는 것이다."
        )
    return payload


@router.get("/ops/project-stats")
async def ops_project_stats(
    days: int = Query(30, description="집계 일수 (1~365, 벗어나면 잘라낸다)"),
):
    """프로젝트별 파이프라인 건수/상태 분포 — 채팅 아티팩트 완료율 차트."""
    days = _clamp_int(days, default=30, minimum=1, maximum=365)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch("""
                SELECT project, status, COUNT(*)::int AS cnt
                FROM (
                    SELECT DISTINCT ON (job_id) job_id, project, status
                    FROM (
                        SELECT job_id, project, status, 0 AS src
                        FROM pipeline_jobs
                        WHERE created_at >= NOW() - (INTERVAL '1 day' * $1::int)
                        UNION ALL
                        SELECT job_id, project, status, 1 AS src
                        FROM pipeline_jobs_archive
                        WHERE created_at >= NOW() - (INTERVAL '1 day' * $1::int)
                    ) u
                    ORDER BY job_id, src
                ) j
                GROUP BY project, status
            """, days)
        finally:
            await conn.close()
    except Exception as e:
        logger.error("ops_project_stats_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    items = _project_stat_items([dict(r) for r in rows])
    return {
        "items": items,
        "days": days,
        "total": sum(i["total"] for i in items),
        "generated_at": datetime.now(KST).isoformat(),
    }


@router.get("/ops/status")
async def ops_status():
    """운영 상태 요약 — 서버 카드 + 서킷브레이커 (ArtifactDashboard).

    상태를 새로 계산하지 않는다. 기존 /ops/health-check 와 서킷브레이커 상태를
    그대로 접어서 위젯이 읽는 모양으로만 바꾼다 (중복 구현 금지).
    """
    from app.services.circuit_breaker import FAILURE_THRESHOLD, get_all_states

    health: Dict[str, Any] = {}
    circuit_states: List[Dict[str, Any]] = []
    try:
        health = await health_check()
    except Exception as e:
        logger.error("ops_status_health_error", error=str(e))
        health = {"error": str(e), "pipeline_healthy": False}
    try:
        circuit_states = await get_all_states()
    except Exception as e:
        logger.warning("ops_status_circuit_error", error=str(e))

    return _ops_status_payload(health, circuit_states, _server_meta_list(), FAILURE_THRESHOLD)


@router.get("/ops/pipeline-history")
async def ops_pipeline_history(
    limit: int = Query(10, description="조회 건수 (1~100, 벗어나면 잘라낸다)"),
    project: Optional[str] = None,
):
    """최근 파이프라인 작업 이력 — 큐(pipeline_jobs) + 아카이브 합본."""
    limit = _clamp_int(limit, default=10, minimum=1, maximum=100)
    project_label = normalize_project_label(project) if project else None

    params: list = [limit]
    project_filter = ""
    if project_label:
        project_filter = "WHERE project = $2"
        params.append(project_label)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(f"""
                SELECT job_id, project, status, phase, instruction,
                       created_at, completed_at, source
                FROM (
                    SELECT DISTINCT ON (job_id)
                           job_id, project, status, phase, instruction,
                           created_at, completed_at, source
                    FROM (
                        SELECT job_id, project, status, phase, instruction,
                               created_at, completed_at, 'live' AS source, 0 AS src
                        FROM pipeline_jobs
                        UNION ALL
                        SELECT job_id, project, status,
                               row_data->>'phase' AS phase,
                               row_data->>'instruction' AS instruction,
                               created_at,
                               NULLIF(row_data->>'completed_at', '')::timestamptz AS completed_at,
                               'archive' AS source, 1 AS src
                        FROM pipeline_jobs_archive
                    ) u
                    ORDER BY job_id, src
                ) j
                {project_filter}
                ORDER BY created_at DESC NULLS LAST
                LIMIT $1::int
            """, *params)
        finally:
            await conn.close()
    except Exception as e:
        logger.error("ops_pipeline_history_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    items = _pipeline_history_items([dict(r) for r in rows])
    return {
        "items": items,
        "limit": limit,
        "project": project_label,
        "count": len(items),
        "generated_at": datetime.now(KST).isoformat(),
    }


@router.get("/ops/qa-results")
async def ops_qa_results(
    limit: int = Query(20, description="조회 건수 (1~200, 벗어나면 잘라낸다)"),
    project: Optional[str] = None,
):
    """최근 QA(코드리뷰) 판정 — /ops 패널 QA Results 섹션.

    출처는 code_reviews 다. design_qa_scores 는 디자인 수정요청(request_id)
    점수표라 task_id/project/판정 계약이 없고 행도 0건이다 — 2026-09-16 스키마
    직접 조회로 확인했다.
    """
    limit = _clamp_int(limit, default=20, minimum=1, maximum=200)
    project_label = normalize_project_label(project) if project else None

    params: list = [limit]
    where = ""
    if project_label:
        where = "WHERE project = $2"
        params.append(project_label)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(f"""
                SELECT job_id, project, verdict, score, review_cycle,
                       needs_retry, flag_category, feedback, created_at
                FROM code_reviews
                {where}
                ORDER BY created_at DESC
                LIMIT $1::int
            """, *params)
        finally:
            await conn.close()
    except Exception as e:
        logger.error("ops_qa_results_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    items = _qa_result_items([dict(r) for r in rows])
    return {
        "items": items,
        "limit": limit,
        "project": project_label,
        "count": len(items),
        "source_table": "code_reviews",
        "generated_at": datetime.now(KST).isoformat(),
    }


@router.get("/ops/design-reviews")
async def ops_design_reviews(
    limit: int = Query(10, description="조회 건수 (1~100, 벗어나면 잘라낸다)"),
):
    """최근 디자인 리뷰 판정 — /ops 패널 Design Reviews 섹션."""
    limit = _clamp_int(limit, default=10, minimum=1, maximum=100)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch("""
                SELECT d.id, d.task_id, d.page_url, d.before_path, d.after_path,
                       d.verdict, d.issues_json, d.scores_json, d.reviewer_model,
                       d.cost_usd, d.created_at,
                       COALESCE(j.project, a.project) AS project
                FROM design_reviews d
                LEFT JOIN pipeline_jobs j ON j.job_id = d.task_id
                LEFT JOIN pipeline_jobs_archive a ON a.job_id = d.task_id
                ORDER BY d.created_at DESC NULLS LAST
                LIMIT $1::int
            """, limit)
        finally:
            await conn.close()
    except Exception as e:
        logger.error("ops_design_reviews_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    items = _design_review_items([dict(r) for r in rows])
    return {
        "items": items,
        "limit": limit,
        "count": len(items),
        "generated_at": datetime.now(KST).isoformat(),
    }
