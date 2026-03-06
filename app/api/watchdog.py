"""
T-038: Watchdog API — 에러 자동기록·학습·자동복구
"""
import hashlib
import re
import subprocess
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from app.api.context import verify_monitor_key, check_rate_limit
from app.memory.store import memory_store

import structlog

logger = structlog.get_logger()
router = APIRouter()


# --- Models ---
class ErrorReport(BaseModel):
    error_type: str
    source: str
    server: str
    message: str
    stack_trace: Optional[str] = None
    context: Optional[Dict[str, Any]] = {}


class ResolutionUpdate(BaseModel):
    resolution: str
    auto_recoverable: bool = False
    recovery_command: Optional[str] = None


# --- 유틸 ---
def _error_hash(error_type: str, source: str, message: str) -> str:
    """동일 에러 그룹화를 위한 해시. 메시지에서 가변 부분 제거."""
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', 'TIMESTAMP', message)
    normalized = re.sub(r'/root/[^\s]+', 'PATH', normalized)
    normalized = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', 'IP', normalized)
    raw = f"{error_type}:{source}:{normalized[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# --- 에러 기록 (POST) ---
@router.post("/watchdog/errors")
async def report_error(
    req: ErrorReport,
    request: Request,
    auth: bool = Depends(verify_monitor_key),
    _rate: None = Depends(check_rate_limit),
):
    """에러 자동 기록. 동일 패턴이면 occurrence_count 증가."""
    eh = _error_hash(req.error_type, req.source, req.message)

    async with memory_store.pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, occurrence_count, auto_recoverable, recovery_command FROM error_log WHERE error_hash=$1",
            eh
        )

        if existing:
            await conn.execute("""
                UPDATE error_log
                SET occurrence_count = occurrence_count + 1,
                    last_seen = NOW(),
                    message = $2,
                    stack_trace = COALESCE($3, stack_trace),
                    context = $4::jsonb
                WHERE error_hash = $1
            """, eh, req.message, req.stack_trace, str(req.context or {}))

            error_id = existing["id"]
            count = existing["occurrence_count"] + 1

            if existing["auto_recoverable"] and existing["recovery_command"]:
                recovery_result = await _attempt_recovery(
                    error_id, existing["recovery_command"]
                )
                return {
                    "status": "auto_recovered" if recovery_result else "recovery_failed",
                    "error_hash": eh,
                    "error_id": error_id,
                    "occurrence_count": count,
                    "recovery_attempted": True,
                    "recovery_success": recovery_result,
                }

            return {
                "status": "recorded_recurring",
                "error_hash": eh,
                "error_id": error_id,
                "occurrence_count": count,
                "has_resolution": bool(existing.get("auto_recoverable")),
            }
        else:
            row = await conn.fetchrow("""
                INSERT INTO error_log (error_hash, error_type, source, server, message, stack_trace, context, resolution_type)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'pending')
                RETURNING id
            """, eh, req.error_type, req.source, req.server,
                req.message, req.stack_trace, str(req.context or {}))

            return {
                "status": "recorded_new",
                "error_hash": eh,
                "error_id": row["id"],
                "occurrence_count": 1,
            }


# --- 해결법 등록 (PUT) ---
@router.put("/watchdog/errors/{error_hash}/resolution")
async def update_resolution(
    error_hash: str,
    req: ResolutionUpdate,
    auth: bool = Depends(verify_monitor_key),
):
    """에러 해결법 등록. 다음 동일 에러 발생 시 자동 적용."""
    async with memory_store.pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE error_log
            SET resolution = $2,
                resolution_type = CASE WHEN $3 THEN 'auto' ELSE 'manual' END,
                auto_recoverable = $3,
                recovery_command = $4,
                resolved_at = NOW()
            WHERE error_hash = $1
        """, error_hash, req.resolution, req.auto_recoverable, req.recovery_command)

        if result == "UPDATE 0":
            raise HTTPException(404, f"Error hash '{error_hash}' not found")

        # Experience Memory L3에도 저장 (에러 해결법 학습)
        try:
            await memory_store.store_experience(
                experience_type="error_resolution",
                domain="watchdog",
                tags=[error_hash, "auto" if req.auto_recoverable else "manual"],
                content={
                    "title": f"error_resolution:{error_hash}",
                    "error_hash": error_hash,
                    "resolution": req.resolution,
                    "auto_recoverable": req.auto_recoverable,
                    "recovery_command": req.recovery_command,
                }
            )
        except Exception as e:
            logger.warning("experience_store_failed", error=str(e))

        return {"status": "ok", "error_hash": error_hash, "auto_recoverable": req.auto_recoverable}


# --- 에러 목록 조회 (GET) ---
@router.get("/watchdog/errors")
async def list_errors(
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    limit: int = 50,
    auth: bool = Depends(verify_monitor_key),
):
    """에러 목록 조회. status=pending으로 미해결 건만 필터."""
    async with memory_store.pool.acquire() as conn:
        query = """
            SELECT id, error_hash, error_type, source, server, message,
                   resolution, resolution_type, auto_recoverable,
                   occurrence_count, first_seen, last_seen
            FROM error_log WHERE 1=1
        """
        params = []
        idx = 1

        if status:
            query += f" AND resolution_type=${idx}"
            params.append(status)
            idx += 1
        if error_type:
            query += f" AND error_type=${idx}"
            params.append(error_type)
            idx += 1

        query += f" ORDER BY last_seen DESC LIMIT ${idx}"
        params.append(limit)

        rows = await conn.fetch(query, *params)
        return {
            "status": "ok",
            "count": len(rows),
            "errors": [dict(r) for r in rows],
        }


# --- 대시보드 요약 (GET, 인증 불필요) ---
@router.get("/watchdog/summary")
async def watchdog_summary():
    """워치독 상태 요약 — public 엔드포인트."""
    async with memory_store.pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_errors,
                COUNT(*) FILTER (WHERE resolution_type = 'pending') as pending,
                COUNT(*) FILTER (WHERE resolution_type = 'auto') as auto_resolved,
                COUNT(*) FILTER (WHERE resolution_type = 'manual') as manual_resolved,
                COUNT(*) FILTER (WHERE auto_recoverable = TRUE) as auto_recoverable,
                COUNT(*) FILTER (WHERE last_seen > NOW() - INTERVAL '24 hours') as last_24h,
                COALESCE(SUM(occurrence_count), 0) as total_occurrences
            FROM error_log
        """)

        recent = await conn.fetch("""
            SELECT error_hash, error_type, source, server, message,
                   occurrence_count, last_seen
            FROM error_log ORDER BY last_seen DESC LIMIT 5
        """)

        recovery_seeds = await conn.fetchval(
            "SELECT COUNT(*) FROM recovery_log WHERE auto_executable = true"
        )

        stats_dict = dict(stats)
        stats_dict["recovery_seeds"] = int(recovery_seeds or 0)

        return {
            "status": "ok",
            "stats": stats_dict,
            "recent_errors": [dict(r) for r in recent],
        }


# --- 자동 복구 실행 ---
async def _attempt_recovery(error_id: int, command: str) -> bool:
    """안전한 명령만 실행. 화이트리스트 기반."""
    SAFE_PREFIXES = [
        "docker restart",
        "docker compose",
        "systemctl reload nginx",
        "systemctl restart",
        "supervisorctl restart",
        "curl",
    ]

    if not any(command.strip().startswith(p) for p in SAFE_PREFIXES):
        logger.warning("recovery_blocked_unsafe_command", command=command[:100])
        return False

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
        success = result.returncode == 0

        async with memory_store.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO recovery_log (error_log_id, recovery_command, success, output)
                VALUES ($1, $2, $3, $4)
            """, error_id, command, success,
                (result.stdout[:500] + result.stderr[:500]))

        logger.info("recovery_attempted", error_id=error_id, success=success)
        return success
    except Exception as e:
        logger.error("recovery_execution_failed", error_id=error_id, error=str(e))
        return False
