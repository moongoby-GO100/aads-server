"""
AADS-113: 운영 통합 DB API 엔드포인트
- /api/v1/ops/directive-lifecycle  (POST/GET)
- /api/v1/ops/cost                 (POST/GET summary)
- /api/v1/ops/commit               (POST/GET)
- /api/v1/ops/bridge-log           (GET)
- /api/v1/ops/env-history/{server} (GET)
- /api/v1/ops/health-check         (GET)
- /api/v1/ops/stalled              (GET)
- /api/v1/ops/auto-recover         (POST)
AADS-116: 유지보수 모드 API
- /api/v1/ops/maintenance/start    (POST)
- /api/v1/ops/maintenance/end      (POST)
- /api/v1/ops/maintenance/status   (GET)
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict
import structlog

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
import asyncpg

logger = structlog.get_logger()
router = APIRouter()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads_dev_local@aads-postgres:5432/aads"
)

KST = timezone(timedelta(hours=9))


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL, timeout=10)


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

    status_col_map = {
        "queued": "queued_at",
        "running": "started_at",
        "completed": "completed_at",
        "requeued": "queued_at",
        "failed": "completed_at",
    }
    ts_col = status_col_map.get(req.status)

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
            """, req.task_id, req.project, req.title, req.server,
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
        conditions.append(f"project = ${idx}")
        params.append(project); idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status); idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, task_id, project, title, server, priority, status,
               queued_at, started_at, completed_at, duration_seconds,
               wait_seconds, error_detail
        FROM directive_lifecycle
        {where}
        ORDER BY COALESCE(completed_at, started_at, queued_at, created_at) DESC
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
    try:
        conn = await _get_conn()
        try:
            await conn.execute("""
                INSERT INTO cost_tracking (task_id, project, model, input_tokens,
                    output_tokens, cost_usd, llm_calls)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
            """, req.task_id, req.project, req.model, req.input_tokens,
                req.output_tokens, req.cost_usd, req.llm_calls)
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
        params.append(project); idx += 1
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
        params.append(task_id); idx += 1
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
        params.append(classification); idx += 1
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
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                "SELECT * FROM server_env_history WHERE server=$1 ORDER BY snapshot_at DESC LIMIT $2",
                server, limit
            )
        finally:
            await conn.close()
        return {"server": server, "items": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Health Check ─────────────────────────────────────────────────────────────

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
        return {
            "pipeline_healthy": pipeline_healthy,
            "stalled_count": stalled_count,
            "stalled_queue": int(stalled_queue or 0),
            "stalled_running": int(stalled_running or 0),
            "active_count": int(active_count or 0),
            "recent_completed_30m": int(recent_completed or 0),
            "pipeline_blocked": pipeline_blocked,
            "bridge_activity_1h": int(bridge_recent or 0),
            "blocked_tasks_count": int(blocked_tasks or 0),
            "undetected_tasks_count": int(undetected_tasks or 0),
            "last_seen_tasks_check": last_seen_check_kst,
            "maintenance_active": maintenance_active,
            "maintenance_server": maintenance_row["server"] if maintenance_active else None,
            "maintenance_reason": maintenance_row["reason"] if maintenance_active else None,
            "issues": _build_issues(stalled_queue, stalled_running, pipeline_blocked),
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
