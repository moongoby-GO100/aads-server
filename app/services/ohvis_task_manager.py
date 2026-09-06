"""
OHVIS 3-Tier Task Manager — 작업 생명주기 관리.

create → update_step → complete → judge → artifact card 저장.
러너/에이전트 시작 시 자동 생성, 완료 시 오비스 판단 + 아티팩트 카드 저장.
trigger_ai_reaction 결과를 chat_messages가 아닌 task_card 아티팩트로 격리.
"""
import json
import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_COMPLEX_INTENTS = frozenset({
    "code_modify", "deploy", "execute", "pipeline_runner",
    "report", "audit", "cto_strategy", "url_analyze",
})

_MAX_CONCURRENT_TASKS = 3


def _finalize_steps(steps: Any, final_status: str) -> List[Dict]:
    """Keep task row steps consistent when the parent task is terminal."""
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:
            steps = []
    if not isinstance(steps, list):
        return []
    target = "done" if final_status == "done" else "error"
    finalized = []
    for step in steps:
        if not isinstance(step, dict):
            finalized.append(step)
            continue
        item = dict(step)
        if item.get("status") in (None, "", "pending", "running"):
            item["status"] = target
        finalized.append(item)
    return finalized


async def create_task(
    session_id: str,
    title: str,
    task_type: str = "runner",
    parent_turn_id: Optional[str] = None,
    runner_job_id: Optional[str] = None,
    agent_ids: Optional[List[str]] = None,
    steps: Optional[List[Dict]] = None,
) -> Optional[str]:
    """ohvis_tasks에 작업 생성 + task_card 아티팩트 저장. 반환: task_id."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ohvis_tasks
                    (session_id, title, task_type, parent_turn_id,
                     runner_job_id, agent_ids, steps, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, 'running')
                RETURNING id
                """,
                _uuid.UUID(session_id), title[:200], task_type,
                _uuid.UUID(parent_turn_id) if parent_turn_id else None,
                runner_job_id, agent_ids, json.dumps(steps or []),
            )
            task_id = str(row["id"])
            logger.info("ohvis_task_created id=%s session=%s type=%s",
                        task_id[:8], session_id[:8], task_type)
            await _save_task_card(conn, session_id, task_id, {
                "title": title, "status": "running",
                "task_type": task_type, "steps": steps or [],
                "runner_job_id": runner_job_id,
            })
            return task_id
    except Exception as e:
        logger.warning("ohvis_task_create_failed: %s", e)
        return None


async def update_task(
    task_id: str,
    status: Optional[str] = None,
    steps: Optional[List[Dict]] = None,
    result: Optional[Dict] = None,
    ohvis_judgement: Optional[str] = None,
    cost_usd: Optional[float] = None,
) -> bool:
    """ohvis_tasks 업데이트 + task_card 아티팩트 갱신."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        sets, params = [], []
        idx = 1

        if status:
            sets.append(f"status=${idx}")
            params.append(status)
            idx += 1
            if status in ("done", "error"):
                sets.append(f"completed_at=${idx}")
                params.append(datetime.now(timezone.utc))
                idx += 1

        if steps is not None:
            sets.append(f"steps=${idx}::jsonb")
            params.append(json.dumps(steps))
            idx += 1

        if result is not None:
            sets.append(f"result=${idx}::jsonb")
            params.append(json.dumps(result, ensure_ascii=False, default=str))
            idx += 1

        if ohvis_judgement is not None:
            sets.append(f"ohvis_judgement=${idx}")
            params.append(ohvis_judgement)
            idx += 1

        if cost_usd is not None:
            sets.append(f"cost_usd=${idx}")
            params.append(cost_usd)
            idx += 1

        if not sets:
            return True

        sets.append(f"updated_at=${idx}")
        params.append(datetime.now(timezone.utc))
        idx += 1
        params.append(_uuid.UUID(task_id))

        query = (
            f"UPDATE ohvis_tasks SET {', '.join(sets)} WHERE id=${idx} "
            "RETURNING session_id, title, status, steps, task_type, "
            "runner_job_id, result, ohvis_judgement"
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            if not row:
                return False
            _steps = row["steps"]
            if isinstance(_steps, str):
                _steps = json.loads(_steps)
            if row["status"] in ("done", "error"):
                _final_steps = _finalize_steps(_steps, row["status"])
                if _final_steps != (_steps or []):
                    await conn.execute(
                        "UPDATE ohvis_tasks SET steps=$1::jsonb, updated_at=NOW() WHERE id=$2",
                        json.dumps(_final_steps, ensure_ascii=False, default=str),
                        _uuid.UUID(task_id),
                    )
                    _steps = _final_steps
            _result = row["result"]
            if isinstance(_result, str):
                _result = json.loads(_result)
            await _save_task_card(conn, str(row["session_id"]), task_id, {
                "title": row["title"], "status": row["status"],
                "task_type": row["task_type"], "steps": _steps or [],
                "runner_job_id": row["runner_job_id"],
                "result": _result, "ohvis_judgement": row["ohvis_judgement"],
            })
            logger.info("ohvis_task_updated id=%s status=%s", task_id[:8], status or "-")
            return True
    except Exception as e:
        logger.warning("ohvis_task_update_failed id=%s: %s", task_id[:8], e)
        return False


async def complete_task(
    task_id: str,
    status: str = "done",
    result: Optional[Dict] = None,
    ohvis_judgement: Optional[str] = None,
) -> bool:
    """작업 완료 + 보고 시각 기록."""
    ok = await update_task(task_id, status=status, result=result,
                           ohvis_judgement=ohvis_judgement)
    if ok:
        try:
            from app.core.db_pool import get_pool
            async with get_pool().acquire() as conn:
                await conn.execute(
                    "UPDATE ohvis_tasks SET reported_at = NOW() WHERE id = $1",
                    _uuid.UUID(task_id),
                )
        except Exception:
            pass
    return ok


async def mark_stale_running_tasks(stale_hours: int = 24) -> int:
    """status='running'인데 updated_at이 stale_hours 이상 지난 좀비 작업을 'stale'로 마킹.

    러너/에이전트 프로세스가 죽거나 서버 재시작으로 완료 보고 없이 끊긴 작업이
    'running'에 영구 고정되어 활성 작업 슬롯(_MAX_CONCURRENT_TASKS)을 막는 것을 방지한다.
    """
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE ohvis_tasks
                SET status = 'stale', updated_at = NOW()
                WHERE status = 'running'
                  AND updated_at < NOW() - ($1 || ' hours')::interval
                RETURNING id, session_id, title
                """,
                str(stale_hours),
            )
            for row in rows:
                if not row["session_id"]:
                    continue
                try:
                    await _save_task_card(conn, str(row["session_id"]), str(row["id"]), {
                        "title": row["title"], "status": "stale",
                    })
                except Exception:
                    pass
            if rows:
                logger.warning("ohvis_tasks_marked_stale: count=%d threshold_hours=%d", len(rows), stale_hours)
            return len(rows)
    except Exception as e:
        logger.warning("ohvis_tasks_mark_stale_failed: %s", e)
        return 0


async def find_task_by_runner(runner_job_id: str) -> Optional[str]:
    """runner_job_id로 기존 task_id 조회."""
    try:
        from app.core.db_pool import get_pool
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM ohvis_tasks WHERE runner_job_id = $1 "
                "ORDER BY created_at DESC LIMIT 1",
                runner_job_id,
            )
            return str(row["id"]) if row else None
    except Exception:
        return None


async def get_active_tasks(session_id: str) -> List[Dict]:
    """세션의 활성 작업 목록 (running/pending)."""
    try:
        from app.core.db_pool import get_pool
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, title, status, task_type, steps, runner_job_id, "
                "created_at, updated_at "
                "FROM ohvis_tasks WHERE session_id = $1 "
                "AND status IN ('pending', 'running') "
                "ORDER BY created_at DESC LIMIT 10",
                _uuid.UUID(session_id),
            )
            result = []
            for r in rows:
                d = dict(r)
                for k in ("id", "created_at", "updated_at"):
                    if d.get(k):
                        d[k] = str(d[k])
                if isinstance(d.get("steps"), str):
                    d["steps"] = json.loads(d["steps"])
                result.append(d)
            return result
    except Exception:
        return []


async def emit_task_sse(session_id: str, event_type: str, task_data: Dict) -> None:
    """Redis pub/sub로 task 이벤트 전파 (대시보드 SSE 구독용)."""
    try:
        from app.services.redis_stream import _get_redis
        redis = await _get_redis()
        if redis:
            channel = f"ohvis:task:{session_id}"
            payload = json.dumps({
                "type": event_type,
                "task": task_data,
            }, ensure_ascii=False, default=str)
            await redis.publish(channel, payload)
    except Exception as e:
        logger.debug("task_sse_emit_failed: %s", e)


def generate_instant_plan(intent: str, content: str) -> str:
    """복잡한 인텐트에 대한 즉시 계획 메시지 생성 (Tier 1)."""
    plans = {
        "code_modify": "코드 분석 및 수정",
        "deploy": "배포 절차 준비",
        "execute": "명령 실행 준비",
        "pipeline_runner": "Pipeline Runner 작업 위임",
        "report": "분석 보고서 작성",
        "audit": "감사/검증 작업",
        "cto_strategy": "전략 분석",
        "url_analyze": "URL 분석",
    }
    action = plans.get(intent, "작업 처리")
    short = content[:80].strip()
    if len(content) > 80:
        short += "..."
    return (
        f"📋 **{action}**을 시작합니다.\n"
        f"> {short}\n\n"
        f"_도구 호출 및 분석이 진행됩니다. 진행상황은 아티팩트 패널에서 확인하세요._"
    )


def is_complex_intent(intent: str) -> bool:
    return intent in _COMPLEX_INTENTS


async def _save_task_card(
    conn, session_id: str, task_id: str, task_data: Dict[str, Any],
) -> None:
    """task_card 아티팩트를 chat_artifacts에 UPSERT."""
    try:
        artifact_id = _uuid.uuid5(_uuid.NAMESPACE_URL, f"task_card:{task_id}")
        content = json.dumps(task_data, ensure_ascii=False, default=str)
        status = task_data.get("status", "")
        emoji = {"running": "🔄", "done": "✅", "error": "❌",
                 "pending": "⏳", "awaiting_approval": "🔍"}.get(status, "📋")
        title = f"{emoji} {task_data.get('title', '')[:100]}"

        await conn.execute(
            """
            INSERT INTO chat_artifacts
                (id, session_id, type, title, content, metadata, created_at, updated_at)
            VALUES ($1, $2, 'task_card', $3, $4, $5::jsonb, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title, content = EXCLUDED.content,
                metadata = EXCLUDED.metadata, updated_at = NOW()
            """,
            artifact_id, _uuid.UUID(session_id), title, content,
            json.dumps({"task_id": task_id, "status": status}, default=str),
        )
        await emit_task_sse(session_id, f"task_{status}", {
            "task_id": task_id, "title": task_data.get("title"),
            "status": status, "steps": task_data.get("steps", []),
        })
    except Exception as e:
        logger.debug("task_card_save_failed: %s", e)


async def get_multi_task_status(session_id: str) -> Dict[str, Any]:
    """P2-2: 멀티태스크 동시 진행 상태 조회"""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, status, task_type, steps, runner_job_id, cost_usd,
                       created_at, updated_at
                FROM ohvis_tasks
                WHERE session_id = $1 AND status IN ('pending', 'running')
                ORDER BY created_at ASC
                """,
                _uuid.UUID(session_id),
            )
            tasks = []
            for r in rows:
                steps = r["steps"]
                if isinstance(steps, str):
                    steps = json.loads(steps)
                tasks.append({
                    "id": str(r["id"]),
                    "title": r["title"],
                    "status": r["status"],
                    "task_type": r["task_type"],
                    "steps": steps or [],
                    "runner_job_id": r["runner_job_id"],
                    "cost_usd": float(r["cost_usd"]) if r["cost_usd"] is not None else None,
                    "elapsed_sec": int((datetime.now(timezone.utc) - r["created_at"]).total_seconds()),
                })
            return {
                "active": len(tasks),
                "slots_max": _MAX_CONCURRENT_TASKS,
                "tasks": tasks,
            }
    except Exception as e:
        logger.error("get_multi_task_status_failed session=%s: %s", session_id[:8], e)
        return {"active": 0, "slots_max": _MAX_CONCURRENT_TASKS, "tasks": []}
