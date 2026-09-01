"""Global PC Agent collection queue for authenticated site automation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("YEOLJEONG_FINANCE_DATA_DIR", "app/data/yeoljeong_finance"))
QUEUE_PATH = Path(os.getenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(DATA_DIR / "pc_agent_collection_queue.json")))

ACTIVE_STATUSES = {"queued", "running", "action_required"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "superseded"}
ALLOWED_QUEUE_TYPES = {"delivery", "bank", "financial", "browser_recipe"}


def _now() -> datetime:
    return datetime.now(KST)


def _now_text() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clean_key(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def build_resource_key(item: dict[str, Any]) -> str:
    site_key = _clean_key(item.get("site_key"), _clean_key(item.get("service"), "site"))
    work_key = _clean_key(item.get("work_key"), site_key)
    runtime = _clean_key(item.get("runtime"), "pc_agent")
    return f"{runtime}|{site_key}|{work_key}"


def build_job_key(item: dict[str, Any]) -> str:
    raw = "|".join(
        [
            _clean_key(item.get("tenant_id"), "global"),
            _clean_key(item.get("queue_type"), "delivery"),
            _clean_key(item.get("service"), "service"),
            _clean_key(item.get("business_id"), "business"),
            _clean_key(item.get("branch"), "branch"),
            _clean_key(item.get("work_key"), _clean_key(item.get("site_key"), "work")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = _json_dict(item.get("payload"))
    queue_type = _clean_key(item.get("queue_type"), "delivery")
    if queue_type not in ALLOWED_QUEUE_TYPES:
        queue_type = "delivery"
    normalized = {
        "id": _clean_key(item.get("id"), str(uuid.uuid4())),
        "tenant_id": _clean_key(item.get("tenant_id"), ""),
        "job_key": _clean_key(item.get("job_key"), ""),
        "queue_type": queue_type,
        "site_key": _clean_key(item.get("site_key"), _clean_key(item.get("service"), "site")),
        "service": _clean_key(item.get("service"), ""),
        "business_id": _clean_key(item.get("business_id"), ""),
        "branch": _clean_key(item.get("branch"), ""),
        "work_key": _clean_key(item.get("work_key"), ""),
        "runtime": _clean_key(item.get("runtime"), "pc_agent"),
        "priority": _as_int(item.get("priority"), default=50, minimum=0, maximum=1000),
        "min_interval_seconds": _as_int(item.get("min_interval_seconds"), default=900, minimum=0, maximum=86400),
        "latest_only": bool(item.get("latest_only", True)),
        "status": _clean_key(item.get("status"), "queued"),
        "next_run_at": _clean_key(item.get("next_run_at"), _now_text()),
        "lease_agent_id": _clean_key(item.get("lease_agent_id"), ""),
        "attempt_count": _as_int(item.get("attempt_count"), default=0, minimum=0, maximum=100000),
        "max_attempts": _as_int(item.get("max_attempts"), default=3, minimum=1, maximum=1000),
        "payload": payload,
        "result": _json_dict(item.get("result")),
        "error_code": _clean_key(item.get("error_code"), ""),
        "message": _clean_key(item.get("message"), ""),
        "created_by": _clean_key(item.get("created_by"), ""),
        "created_at": _clean_key(item.get("created_at"), _now_text()),
        "updated_at": _clean_key(item.get("updated_at"), _now_text()),
        "started_at": _clean_key(item.get("started_at"), ""),
        "finished_at": _clean_key(item.get("finished_at"), ""),
    }
    normalized["resource_key"] = _clean_key(item.get("resource_key"), build_resource_key(normalized))
    normalized["job_key"] = _clean_key(item.get("job_key"), build_job_key(normalized))
    if normalized["status"] not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        normalized["status"] = "queued"
    return normalized


def _read_json_queue() -> list[dict[str, Any]]:
    if not QUEUE_PATH.exists():
        return []
    try:
        parsed = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [normalize_queue_item(item) for item in parsed if isinstance(item, dict)]


def _write_json_queue(rows: list[dict[str, Any]]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QUEUE_PATH)


def _db_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("YEOLJEONG_FINANCE_DATABASE_URL"))


async def _enqueue_db(item: dict[str, Any]) -> dict[str, Any]:
    pool = await _ensure_pool()

    tenant_uuid = uuid.UUID(item["tenant_id"]) if item.get("tenant_id") else None
    async with pool.acquire() as conn:
        async with conn.transaction():
            if item["latest_only"]:
                await conn.execute(
                    """
                    UPDATE pc_agent_collection_queue
                       SET status = 'superseded',
                           message = 'Superseded by newer latest_only request',
                           finished_at = COALESCE(finished_at, NOW()),
                           updated_at = NOW()
                     WHERE resource_key = $1
                       AND status = 'queued'
                       AND job_key <> $2
                    """,
                    item["resource_key"],
                    item["job_key"],
                )
            row = await conn.fetchrow(
                """
                INSERT INTO pc_agent_collection_queue (
                    tenant_id, job_key, queue_type, site_key, service, business_id, branch,
                    work_key, resource_key, runtime, priority, min_interval_seconds,
                    latest_only, status, next_run_at, payload, max_attempts, created_by,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12,
                    $13, 'queued', $14::timestamptz, $15::jsonb, $16, $17,
                    NOW()
                )
                ON CONFLICT (job_key) DO UPDATE
                   SET queue_type = EXCLUDED.queue_type,
                       site_key = EXCLUDED.site_key,
                       service = EXCLUDED.service,
                       business_id = EXCLUDED.business_id,
                       branch = EXCLUDED.branch,
                       work_key = EXCLUDED.work_key,
                       resource_key = EXCLUDED.resource_key,
                       runtime = EXCLUDED.runtime,
                       priority = EXCLUDED.priority,
                       min_interval_seconds = EXCLUDED.min_interval_seconds,
                       latest_only = EXCLUDED.latest_only,
                       status = CASE
                           WHEN pc_agent_collection_queue.status = 'running' THEN 'running'
                           WHEN pc_agent_collection_queue.status = 'action_required'
                                AND pc_agent_collection_queue.next_run_at > NOW()
                           THEN 'action_required'
                           ELSE 'queued'
                       END,
                       next_run_at = CASE
                           WHEN pc_agent_collection_queue.status = 'action_required'
                                AND pc_agent_collection_queue.next_run_at > NOW()
                           THEN pc_agent_collection_queue.next_run_at
                           ELSE EXCLUDED.next_run_at
                       END,
                       payload = EXCLUDED.payload,
                       max_attempts = EXCLUDED.max_attempts,
                       error_code = CASE
                           WHEN pc_agent_collection_queue.status = 'action_required'
                                AND pc_agent_collection_queue.next_run_at > NOW()
                           THEN pc_agent_collection_queue.error_code
                           ELSE ''
                       END,
                       message = CASE
                           WHEN pc_agent_collection_queue.status = 'action_required'
                                AND pc_agent_collection_queue.next_run_at > NOW()
                           THEN pc_agent_collection_queue.message
                           ELSE ''
                       END,
                       updated_at = NOW()
                RETURNING *
                """,
                tenant_uuid,
                item["job_key"],
                item["queue_type"],
                item["site_key"],
                item["service"],
                item["business_id"],
                item["branch"],
                item["work_key"],
                item["resource_key"],
                item["runtime"],
                item["priority"],
                item["min_interval_seconds"],
                item["latest_only"],
                item["next_run_at"],
                json.dumps(item["payload"], ensure_ascii=False),
                item["max_attempts"],
                item["created_by"],
            )
    return _row_to_item(row)


def _row_to_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("id", "tenant_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    for key in ("created_at", "updated_at", "next_run_at", "started_at", "finished_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    item["payload"] = _json_dict(item.get("payload"))
    item["result"] = _json_dict(item.get("result"))
    return item


def _run_db(coro: Any) -> Any | None:
    if not _db_enabled():
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None
    try:
        return asyncio.run(coro)
    except Exception:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None


def enqueue_collection_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_queue_item(item)
    db_item = _run_db(_enqueue_db(normalized))
    if isinstance(db_item, dict):
        return db_item
    rows = _read_json_queue()
    if normalized["latest_only"]:
        for row in rows:
            if row["resource_key"] == normalized["resource_key"] and row["status"] == "queued" and row["job_key"] != normalized["job_key"]:
                row["status"] = "superseded"
                row["message"] = "Superseded by newer latest_only request"
                row["finished_at"] = _now_text()
                row["updated_at"] = _now_text()
    existing = next((row for row in rows if row["job_key"] == normalized["job_key"]), None)
    if existing:
        existing_next_run_at = _parse_dt(existing.get("next_run_at"))
        keep_action_required = existing.get("status") == "action_required" and bool(
            existing_next_run_at and existing_next_run_at > _now()
        )
        keep_status = (
            "running"
            if existing.get("status") == "running"
            else ("action_required" if keep_action_required else "queued")
        )
        preserved = {
            "error_code": existing.get("error_code") or "",
            "message": existing.get("message") or "",
            "finished_at": existing.get("finished_at") or "",
            "next_run_at": existing.get("next_run_at") or normalized["next_run_at"],
        } if keep_action_required else {}
        existing.update(
            {
                **normalized,
                **preserved,
                "id": existing.get("id") or normalized["id"],
                "status": keep_status,
                "updated_at": _now_text(),
            }
        )
        item_out = existing
    else:
        rows.insert(0, normalized)
        item_out = normalized
    _write_json_queue(rows)
    return item_out


def enqueue_collection_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    queued = [enqueue_collection_item(item) for item in items]
    return {
        "queued": True,
        "count": len(queued),
        "items": queued,
        "job_ids": [item.get("id") for item in queued],
    }


def claim_next_collection_item(*, agent_id: str = "", now: datetime | None = None) -> dict[str, Any] | None:
    now_value = now or _now()
    db_item = _run_db(_claim_next_db(agent_id=agent_id, now_value=now_value))
    if isinstance(db_item, dict):
        return db_item
    rows = _read_json_queue()
    running_resources = {row["resource_key"] for row in rows if row["status"] == "running"}
    due: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] != "queued" or row["resource_key"] in running_resources:
            continue
        payload = _json_dict(row.get("payload"))
        required_agent_id = _clean_key(
            payload.get("required_browser_agent_id"),
            _clean_key(payload.get("browser_agent_id"), _clean_key(payload.get("pc_agent_id"), "")),
        )
        excluded_agent_ids = {
            str(value or "").strip()
            for value in payload.get("excluded_browser_agent_ids", [])
            if str(value or "").strip()
        }
        if required_agent_id and required_agent_id != agent_id:
            continue
        if agent_id and agent_id in excluded_agent_ids:
            continue
        next_run_at = _parse_dt(row.get("next_run_at")) or now_value
        finished_at = _parse_dt(row.get("finished_at"))
        min_interval = int(row.get("min_interval_seconds") or 0)
        if next_run_at > now_value:
            continue
        if finished_at and finished_at + timedelta(seconds=min_interval) > now_value:
            continue
        due.append(row)
    if not due:
        return None
    due.sort(key=lambda item: (int(item.get("priority") or 50), str(item.get("next_run_at") or ""), str(item.get("created_at") or "")))
    item = due[0]
    item["status"] = "running"
    item["lease_agent_id"] = agent_id
    item["attempt_count"] = int(item.get("attempt_count") or 0) + 1
    item["started_at"] = _now_text()
    item["updated_at"] = _now_text()
    _write_json_queue(rows)
    return item


async def _claim_next_db(*, agent_id: str, now_value: datetime) -> dict[str, Any] | None:
    pool = await _ensure_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH candidate AS (
                SELECT q.id
                  FROM pc_agent_collection_queue q
                 WHERE q.status = 'queued'
                   AND q.next_run_at <= $1
                   AND (
                       COALESCE(
                           NULLIF(q.payload->>'required_browser_agent_id', ''),
                           NULLIF(q.payload->>'browser_agent_id', ''),
                           NULLIF(q.payload->>'pc_agent_id', ''),
                           ''
                       ) = ''
                       OR COALESCE(
                           NULLIF(q.payload->>'required_browser_agent_id', ''),
                           NULLIF(q.payload->>'browser_agent_id', ''),
                           NULLIF(q.payload->>'pc_agent_id', ''),
                           ''
                       ) = $2
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements_text(
                             CASE
                                 WHEN jsonb_typeof(q.payload->'excluded_browser_agent_ids') = 'array'
                                 THEN q.payload->'excluded_browser_agent_ids'
                                 ELSE '[]'::jsonb
                             END
                         ) AS excluded(agent_id)
                        WHERE excluded.agent_id = $2
                   )
                   AND (
                       q.finished_at IS NULL
                       OR q.finished_at + make_interval(secs => q.min_interval_seconds) <= $1
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pc_agent_collection_queue active
                        WHERE active.resource_key = q.resource_key
                          AND active.status = 'running'
                   )
                 ORDER BY q.priority ASC, q.next_run_at ASC, q.created_at ASC
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE pc_agent_collection_queue q
               SET status = 'running',
                   lease_agent_id = $2,
                   attempt_count = q.attempt_count + 1,
                   started_at = NOW(),
                   updated_at = NOW()
              FROM candidate
             WHERE q.id = candidate.id
            RETURNING q.*
            """,
            now_value,
            agent_id,
        )
    return _row_to_item(row) if row else None


def complete_collection_item(
    item_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str = "",
    message: str = "",
    next_run_at: str = "",
) -> dict[str, Any] | None:
    final_status = status if status in TERMINAL_STATUSES | {"action_required", "queued"} else "failed"
    db_item = _run_db(
        _complete_db(
            item_id=item_id,
            status=final_status,
            result=result or {},
            error_code=error_code,
            message=message,
            next_run_at=next_run_at,
        )
    )
    if isinstance(db_item, dict):
        return db_item
    rows = _read_json_queue()
    item = next((row for row in rows if row["id"] == item_id), None)
    if not item:
        return None
    item["status"] = final_status
    item["result"] = result or {}
    item["error_code"] = error_code
    item["message"] = message
    item["finished_at"] = _now_text() if final_status in TERMINAL_STATUSES | {"action_required"} else ""
    item["updated_at"] = _now_text()
    if next_run_at:
        item["next_run_at"] = next_run_at
    _write_json_queue(rows)
    return item


async def _complete_db(
    *,
    item_id: str,
    status: str,
    result: dict[str, Any],
    error_code: str,
    message: str,
    next_run_at: str,
) -> dict[str, Any] | None:
    pool = await _ensure_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE pc_agent_collection_queue
               SET status = $2,
                   result = $3::jsonb,
                   error_code = $4,
                   message = $5,
                   next_run_at = COALESCE(NULLIF($6, '')::timestamptz, next_run_at),
                   finished_at = CASE WHEN $2 IN ('succeeded','failed','cancelled','superseded','action_required') THEN NOW() ELSE finished_at END,
                   updated_at = NOW()
             WHERE id = $1
            RETURNING *
            """,
            uuid.UUID(str(item_id)),
            status,
            json.dumps(result, ensure_ascii=False),
            error_code,
            message,
            next_run_at,
        )
    return _row_to_item(row) if row else None


def queue_snapshot(limit: int = 50) -> list[dict[str, Any]]:
    db_rows = _run_db(_snapshot_db(limit=limit))
    if isinstance(db_rows, list):
        return db_rows
    rows = _read_json_queue()
    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rows[:limit]


async def _snapshot_db(*, limit: int) -> list[dict[str, Any]]:
    pool = await _ensure_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
              FROM pc_agent_collection_queue
             ORDER BY updated_at DESC
             LIMIT $1
            """,
            max(1, min(int(limit), 200)),
        )
    return [_row_to_item(row) for row in rows]


async def _ensure_pool() -> Any:
    from app.core.db_pool import get_pool, init_pool

    try:
        return get_pool()
    except RuntimeError:
        return await init_pool()
