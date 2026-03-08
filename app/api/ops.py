"""
AADS-113: 운영 통합 DB API 엔드포인트
AADS-116: 유지보수 모드 API
AADS-166: 파이프라인 전체 헬스체크 + SSE 스트리밍
"""
import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any, Dict
import structlog

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
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
        }
        return {
            "pipeline_healthy": pipeline_healthy,
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


# ─── Recovery Logs (AADS-132) ─────────────────────────────────────────────────

@router.get("/ops/recovery-logs")
async def list_recovery_logs(
    issue_type: Optional[str] = None,
    result: Optional[str] = None,
    server: Optional[str] = None,
    limit: int = Query(50, le=500),
):
    """복구 이력 조회."""
    conditions = []
    params: list = []
    idx = 1
    if issue_type:
        conditions.append(f"issue_type = ${idx}")
        params.append(issue_type); idx += 1
    if result:
        conditions.append(f"result = ${idx}")
        params.append(result); idx += 1
    if server:
        conditions.append(f"affected_server = ${idx}")
        params.append(server); idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                f"SELECT * FROM recovery_logs {where} ORDER BY created_at DESC LIMIT ${idx}",
                *params
            )
        finally:
            await conn.close()
        return {"items": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error("ops_recovery_logs_list_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


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
                FROM recovery_logs
                GROUP BY issue_type
                ORDER BY total DESC
            """)
            by_tier = await conn.fetch("""
                SELECT tier, COUNT(*) AS total,
                    SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS success_count
                FROM recovery_logs
                GROUP BY tier ORDER BY tier
            """)
            totals = await conn.fetchrow("""
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS total_success
                FROM recovery_logs
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


# ─── Circuit Breaker (AADS-132) ───────────────────────────────────────────────

@router.get("/ops/circuit-breaker")
async def circuit_breaker_status():
    """서킷브레이커 상태 조회 (3서버)."""
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                "SELECT * FROM circuit_breaker_state ORDER BY server"
            )
        finally:
            await conn.close()
        return {"servers": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error("ops_circuit_breaker_status_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ops/circuit-breaker/{server}/reset")
async def circuit_breaker_reset(server: str):
    """서킷브레이커 수동 리셋 — closed 상태로 강제 전환."""
    allowed = {"68", "211", "114"}
    if server not in allowed:
        raise HTTPException(status_code=400, detail=f"server must be one of {allowed}")
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                "UPDATE circuit_breaker_state "
                "SET state='closed', failure_count=0, cooldown_until=NULL, "
                "    opened_at=NULL, updated_at=NOW() "
                "WHERE server=$1",
                server
            )
        finally:
            await conn.close()
        logger.info("circuit_breaker_manual_reset", server=server)
        return {"ok": True, "server": server, "state": "closed"}
    except Exception as e:
        logger.error("ops_circuit_breaker_reset_error", server=server, error=str(e))
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
                conditions.append(f"issue_type = ${idx}"); params.append(issue_type); idx += 1
            if result:
                conditions.append(f"result = ${idx}"); params.append(result); idx += 1
            if server:
                conditions.append(f"affected_server = ${idx}"); params.append(server); idx += 1
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.extend([limit, offset])
            rows = await conn.fetch(
                f"""
                SELECT id, issue_type, affected_task_id, affected_server,
                       tier, action_taken, result, duration_seconds,
                       recovery_route, error_message, recovered_by, created_at::text
                FROM recovery_logs {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx+1}
                """,
                *params,
            )
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM recovery_logs {where}", *params[:-2]
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


@router.get("/ops/recovery-logs/stats")
async def recovery_logs_stats():
    """복구 통계: 이슈 유형별 발생 횟수, 성공률, 평균 복구 시간."""
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(
                """
                SELECT issue_type,
                       COUNT(*) AS total,
                       SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) AS success_count,
                       AVG(duration_seconds) AS avg_duration_seconds
                FROM recovery_logs
                GROUP BY issue_type
                ORDER BY total DESC
                """
            )
        finally:
            await conn.close()
        return {
            "stats": [
                {
                    "issue_type": r["issue_type"],
                    "total": r["total"],
                    "success_count": r["success_count"],
                    "success_rate": round(r["success_count"] / r["total"] * 100, 1) if r["total"] else 0,
                    "avg_duration_seconds": round(float(r["avg_duration_seconds"] or 0), 2),
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.error("recovery_logs_stats_error", error=str(e))
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
async def consistency_check():
    """정합성 검증 (STATUS↔DB, pending↔큐, commit SHA)."""
    from app.services.health_checker import check_consistency
    try:
        return await check_consistency()
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

import glob as _glob
import subprocess as _subprocess

_WATCHDOG_LOG_DIR = "/root/aads/logs/watchdog_reports"
_WATCHDOG_SCRIPT = "/root/aads/scripts/claude_watchdog.py"
_SERVER_211_HOST_OPS = "211.188.51.113"
_SSH_KEY_OPS = "/root/.ssh/id_ed25519_newtalk"


class ClaudeCleanupRequest(BaseModel):
    server: Optional[str] = None  # "68"|"211"|"114"|None(전체)
    reason: Optional[str] = "manual_ceo_trigger"


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
    """수동 claude_watchdog.py 정리 트리거 (CEO 확인용)."""
    try:
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
    """bridge.py 원격 재시작 (서버 211 SSH)."""
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
