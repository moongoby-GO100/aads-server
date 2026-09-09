"""Global PC Agent collection queue for authenticated site automation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
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
FINANCIAL_QUEUE_PROJECTS = {"BANKING"}
FINANCIAL_QUEUE_TYPES = {"bank", "financial"}
FINANCIAL_RESOURCE_KEY = "financial_exclusive"
_RESOURCE_KEY_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "passcode",
    "secret",
    "token",
    "credential",
    "approved_input",
    "otp",
    "captcha",
    "resident_registration",
)
logger = logging.getLogger(__name__)
_DB_RECONCILED = False


def _now() -> datetime:
    return datetime.now(KST)


def _now_text() -> str:
    return _now().isoformat(timespec="seconds")


def _queue_owner_instance(agent_id: str = "") -> str:
    """Return the stable process identity used to fence a claimed lease."""
    return _clean_key(
        os.getenv("AADS_INSTANCE_ID"),
        _clean_key(os.getenv("AADS_CONTAINER_NAME"), _clean_key(agent_id, "local")),
    )


def _lease_seconds() -> int:
    return _as_int(
        os.getenv("YEOLJEONG_QUEUE_LEASE_SECONDS"),
        default=1800,
        minimum=60,
        maximum=86400,
    )


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


def _resource_key_part(value: Any, fallback: str = "") -> str:
    text = _clean_key(value, fallback).strip()
    if not text:
        return fallback
    cleaned = "".join(ch if ch in _RESOURCE_KEY_SAFE_CHARS else "-" for ch in text)
    return cleaned[:96] or fallback


def build_resource_key(item: dict[str, Any]) -> str:
    payload = _json_dict(item.get("payload"))
    project_key = str(payload.get("project_key") or item.get("business_id") or "").strip().upper()
    site_key_raw = str(item.get("site_key") or item.get("service") or payload.get("site_key") or "").strip().lower()
    category_values = payload.get("record_types") or payload.get("data_categories") or []
    categories = {
        str(value or "").strip().lower()
        for value in category_values
        if str(value or "").strip()
    } if isinstance(category_values, list) else set()
    if (
        item.get("queue_type") in FINANCIAL_QUEUE_TYPES
        or project_key in FINANCIAL_QUEUE_PROJECTS
        or any(marker in site_key_raw for marker in ("bank", "banking", "card", "shinhan"))
        or bool(categories & {"transactions", "balances", "statements", "card_usage", "approvals"})
    ):
        tenant = _clean_key(item.get("tenant_id"), "global")
        agent_hint = _clean_key(
            payload.get("required_browser_agent_id"),
            _clean_key(payload.get("browser_agent_id"), _clean_key(payload.get("pc_agent_id"), "default")),
        )
        account_hint = _clean_key(payload.get("bank_account_id"), _clean_key(item.get("work_key"), "account"))
        service_hint = _clean_key(item.get("service"), _clean_key(payload.get("service"), _clean_key(item.get("site_key"), "bank")))
        return "|".join(
            [
                FINANCIAL_RESOURCE_KEY,
                _resource_key_part(tenant, "global"),
                _resource_key_part(agent_hint, "default"),
                _resource_key_part(service_hint, "bank"),
                _resource_key_part(account_hint, "account"),
            ]
        )
    site_key = _clean_key(item.get("site_key"), _clean_key(item.get("service"), "site"))
    work_key = _clean_key(item.get("work_key"), site_key)
    runtime = _clean_key(item.get("runtime"), "pc_agent")
    tenant = _clean_key(item.get("tenant_id"), "")
    prefix = f"{_resource_key_part(tenant)}|" if tenant else ""
    return f"{prefix}{runtime}|{site_key}|{work_key}"


def _is_financial_resource_key(value: Any) -> bool:
    return str(value or "").startswith(f"{FINANCIAL_RESOURCE_KEY}|")


def _financial_resource_agent_hint(value: Any) -> str:
    parts = str(value or "").split("|")
    return parts[-1].strip() if len(parts) >= 3 else ""


def _financial_running_blocks_agent(row: dict[str, Any], *, agent_id: str) -> bool:
    if row.get("status") != "running" or not _is_financial_resource_key(row.get("resource_key")):
        return False
    return True


def _queued_financial_blocks_delivery(row: dict[str, Any], *, now_value: datetime) -> bool:
    if row.get("status") != "queued" or not _is_financial_resource_key(row.get("resource_key")):
        return False
    next_run_at = _parse_dt(row.get("next_run_at")) or now_value
    return next_run_at <= now_value


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


def _read_json_queue(*, strict: bool = False) -> list[dict[str, Any]]:
    if not QUEUE_PATH.exists():
        return []
    try:
        parsed = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if strict:
            raise RuntimeError(
                f"Cannot reconcile malformed legacy queue file: {QUEUE_PATH}"
            ) from exc
        return []
    if not isinstance(parsed, list):
        if strict:
            raise RuntimeError(
                f"Cannot reconcile legacy queue file with non-list root: {QUEUE_PATH}"
            )
        return []
    return [normalize_queue_item(item) for item in parsed if isinstance(item, dict)]


def _write_json_queue(rows: list[dict[str, Any]]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QUEUE_PATH)


def _db_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("YEOLJEONG_FINANCE_DATABASE_URL"))


def _sanitize_migration_value(value: Any) -> tuple[Any, int]:
    """Remove credential-like values before legacy JSON rows enter PostgreSQL."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        removed = 0
        for key, nested in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if any(marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS):
                removed += 1
                continue
            cleaned_value, nested_removed = _sanitize_migration_value(nested)
            cleaned[str(key)] = cleaned_value
            removed += nested_removed
        return cleaned, removed
    if isinstance(value, list):
        cleaned_list = []
        removed = 0
        for nested in value:
            cleaned_value, nested_removed = _sanitize_migration_value(nested)
            cleaned_list.append(cleaned_value)
            removed += nested_removed
        return cleaned_list, removed
    return value, 0


async def reconcile_json_queue_to_db() -> dict[str, Any]:
    """Idempotently import legacy JSON queue rows without copying secrets.

    PostgreSQL remains authoritative: an existing job_key is never overwritten and
    an active row for the same resource prevents importing a second active job.
    """
    if not _db_enabled():
        return {"db_enabled": False, "json_rows": 0, "imported": 0, "skipped": 0, "secrets_removed": 0}

    rows = _read_json_queue(strict=True)
    if not rows:
        return {"db_enabled": True, "json_rows": 0, "imported": 0, "skipped": 0, "secrets_removed": 0}

    pool = await _ensure_pool()
    imported = 0
    skipped = 0
    secrets_removed = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('pc_agent_collection_queue_json_reconcile'))"
            )
            for source in rows:
                sanitized_payload, payload_removed = _sanitize_migration_value(source.get("payload") or {})
                sanitized_result, result_removed = _sanitize_migration_value(source.get("result") or {})
                secrets_removed += payload_removed + result_removed
                item = normalize_queue_item(
                    {**source, "payload": sanitized_payload, "result": sanitized_result}
                )
                try:
                    item_uuid = uuid.UUID(item["id"])
                    tenant_uuid = uuid.UUID(item["tenant_id"]) if item.get("tenant_id") else None
                except (TypeError, ValueError, AttributeError):
                    skipped += 1
                    logger.error("pc_agent_queue_reconcile_invalid_uuid item_id=%s", item.get("id"))
                    continue
                next_run_at = _parse_dt(item.get("next_run_at")) or _now()
                started_at = _parse_dt(item.get("started_at"))
                finished_at = _parse_dt(item.get("finished_at"))
                created_at = _parse_dt(item.get("created_at")) or _now()
                updated_at = _parse_dt(item.get("updated_at")) or created_at
                row = await conn.fetchrow(
                    """
                    INSERT INTO pc_agent_collection_queue (
                        id, tenant_id, job_key, queue_type, site_key, service,
                        business_id, branch, work_key, resource_key, runtime,
                        priority, min_interval_seconds, latest_only, status,
                        next_run_at, lease_agent_id, attempt_count, max_attempts,
                        payload, result, error_code, message, created_by,
                        started_at, finished_at, created_at, updated_at
                    )
                    SELECT
                        $1, $2, $3, $4, $5, $6,
                        $7, $8, $9, $10, $11,
                        $12, $13, $14, $15,
                        $16::timestamptz, $17, $18, $19,
                        $20::jsonb, $21::jsonb, $22, $23, $24,
                        $25::timestamptz, $26::timestamptz,
                        $27::timestamptz, $28::timestamptz
                    WHERE NOT EXISTS (
                        SELECT 1
                          FROM pc_agent_collection_queue active
                         WHERE active.resource_key = $10
                           AND active.job_key <> $3
                           AND active.status IN ('queued', 'running', 'action_required')
                           AND $15 IN ('queued', 'running', 'action_required')
                    )
                    ON CONFLICT (job_key) DO NOTHING
                    RETURNING id
                    """,
                    item_uuid,
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
                    item["status"],
                    next_run_at,
                    item["lease_agent_id"],
                    item["attempt_count"],
                    item["max_attempts"],
                    json.dumps(item["payload"], ensure_ascii=False),
                    json.dumps(item["result"], ensure_ascii=False),
                    item["error_code"],
                    item["message"],
                    item["created_by"],
                    started_at,
                    finished_at,
                    created_at,
                    updated_at,
                )
                if row:
                    imported += 1
                else:
                    skipped += 1
    logger.info(
        "pc_agent_queue_reconciled json_rows=%d imported=%d skipped=%d secrets_removed=%d",
        len(rows),
        imported,
        skipped,
        secrets_removed,
    )
    return {
        "db_enabled": True,
        "json_rows": len(rows),
        "imported": imported,
        "skipped": skipped,
        "secrets_removed": secrets_removed,
    }


async def ensure_queue_storage_ready() -> dict[str, Any]:
    global _DB_RECONCILED
    if not _db_enabled():
        return {"db_enabled": False, "json_rows": 0, "imported": 0, "skipped": 0, "secrets_removed": 0}
    if _DB_RECONCILED:
        return {"db_enabled": True, "already_reconciled": True}
    result = await reconcile_json_queue_to_db()
    _DB_RECONCILED = True
    return result


async def _enqueue_db(item: dict[str, Any]) -> dict[str, Any]:
    await ensure_queue_storage_ready()
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
    for key in (
        "created_at", "updated_at", "next_run_at", "started_at", "finished_at",
        "lease_expires_at",
    ):
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
        raise RuntimeError(
            "PostgreSQL queue operation called through synchronous API inside an active event loop; "
            "use the *_async queue API"
        )
    return _run_on_sync_loop(coro)


_SYNC_LOOP: asyncio.AbstractEventLoop | None = None
_SYNC_LOOP_LOCK = threading.Lock()
_SYNC_POOL: Any = None


def _run_on_sync_loop(coro: Any) -> Any:
    """Run a queue coroutine on one persistent private event loop.

    ``asyncio.run()`` closes its loop after every call, but an asyncpg pool stays
    bound to the loop that created it. Reusing that pool from the next
    ``asyncio.run()`` raised ``Event loop is closed`` /
    ``another operation is in progress`` and made every FOOD queue drain exit=1.
    The private loop is serialized so threadpool callers cannot overlap.
    """
    global _SYNC_LOOP
    with _SYNC_LOOP_LOCK:
        if _SYNC_LOOP is None or _SYNC_LOOP.is_closed():
            _SYNC_LOOP = asyncio.new_event_loop()
        return _SYNC_LOOP.run_until_complete(coro)


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


async def enqueue_collection_item_async(item: dict[str, Any]) -> dict[str, Any]:
    if _db_enabled():
        return await _enqueue_db(normalize_queue_item(item))
    return enqueue_collection_item(item)


def enqueue_collection_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    queued = [enqueue_collection_item(item) for item in items]
    return {
        "queued": True,
        "count": len(queued),
        "items": queued,
        "job_ids": [item.get("id") for item in queued],
    }


async def enqueue_collection_items_async(items: list[dict[str, Any]]) -> dict[str, Any]:
    queued = [await enqueue_collection_item_async(item) for item in items]
    return {
        "queued": True,
        "count": len(queued),
        "items": queued,
        "job_ids": [item.get("id") for item in queued],
    }


def _stale_running_seconds() -> int:
    return _as_int(
        os.getenv("YEOLJEONG_QUEUE_STALE_RUNNING_SECONDS"),
        default=1800,
        minimum=60,
        maximum=86400,
    )


def recover_stale_running_items(rows: list[dict[str, Any]], *, now_value: datetime | None = None) -> int:
    """lease_agent_id 없이 status=running으로 방치된 좀비 항목을 queued로 되돌린다.

    큐 드레이너가 오래(예: hot-reload로 판정 소스가 어긋나 온라인 확인에 계속 실패)
    멈춰 있으면, 이전 시도의 running 항목이 영영 claim_next_collection_item()의
    resource_key 충돌 검사에 걸려 재시도되지 못한다. lease_agent_id가 비어 있고
    updated_at 기준 임계값(기본 1800초)을 넘긴 항목만 복구 대상으로 삼아, 정상적으로
    에이전트가 lease를 쥐고 있는 running 항목은 건드리지 않는다.
    """
    now_value = now_value or _now()
    threshold = timedelta(seconds=_stale_running_seconds())
    recovered = 0
    for row in rows:
        if row.get("status") != "running" or _clean_key(row.get("lease_agent_id")):
            continue
        updated_at = _parse_dt(row.get("updated_at")) or _parse_dt(row.get("started_at"))
        if updated_at is None or (now_value - updated_at) < threshold:
            continue
        row["status"] = "queued"
        row["message"] = (
            f"auto-recovered: stale running without lease_agent_id "
            f"(updated_at={row.get('updated_at')})"
        )
        row["updated_at"] = _now_text()
        recovered += 1
    return recovered


def claim_next_collection_item(*, agent_id: str = "", now: datetime | None = None) -> dict[str, Any] | None:
    now_value = now or _now()
    db_item = _run_db(_claim_next_db(agent_id=agent_id, now_value=now_value))
    if isinstance(db_item, dict):
        return db_item
    rows = _read_json_queue()
    if recover_stale_running_items(rows, now_value=now_value):
        _write_json_queue(rows)
    running_resources = {row["resource_key"] for row in rows if row["status"] == "running"}
    financial_running_blocks = any(
        _financial_running_blocks_agent(row, agent_id=agent_id)
        for row in rows
    )
    queued_financial_blocks = any(
        _queued_financial_blocks_delivery(row, now_value=now_value)
        for row in rows
    )
    due: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] != "queued" or row["resource_key"] in running_resources:
            continue
        if (financial_running_blocks or queued_financial_blocks) and not _is_financial_resource_key(row.get("resource_key")):
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


async def claim_next_collection_item_async(
    *, agent_id: str = "", now: datetime | None = None
) -> dict[str, Any] | None:
    now_value = now or _now()
    if _db_enabled():
        return await _claim_next_db(agent_id=agent_id, now_value=now_value)
    return claim_next_collection_item(agent_id=agent_id, now=now_value)


async def _claim_next_db(*, agent_id: str, now_value: datetime) -> dict[str, Any] | None:
    await ensure_queue_storage_ready()
    pool = await _ensure_pool()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE pc_agent_collection_queue
               SET status = 'queued',
                   message = 'auto-recovered: stale running without lease_agent_id',
                   owner_instance = '',
                   lease_expires_at = NULL,
                   updated_at = NOW()
             WHERE status = 'running'
               AND (
                    (lease_expires_at IS NOT NULL AND lease_expires_at <= $1)
                    OR (
                        lease_expires_at IS NULL
                        AND COALESCE(lease_agent_id, '') = ''
                        AND updated_at <= $2
                    )
               )
            """,
            now_value,
            now_value - timedelta(seconds=_stale_running_seconds()),
        )
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
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pc_agent_collection_queue financial_active
                        WHERE financial_active.status = 'running'
                          AND financial_active.resource_key LIKE 'financial_exclusive|%'
                          AND q.resource_key <> financial_active.resource_key
                          AND q.resource_key NOT LIKE 'financial_exclusive|%'
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pc_agent_collection_queue financial_queued
                        WHERE financial_queued.status = 'queued'
                          AND financial_queued.next_run_at <= $1
                          AND financial_queued.resource_key LIKE 'financial_exclusive|%'
                          AND q.resource_key NOT LIKE 'financial_exclusive|%'
                   )
                 ORDER BY q.priority ASC, q.next_run_at ASC, q.created_at ASC
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE pc_agent_collection_queue q
               SET status = 'running',
                   lease_agent_id = $2,
                   owner_instance = $3,
                   owner_epoch = q.owner_epoch + 1,
                   lease_expires_at = $1 + make_interval(secs => $4),
                   attempt_count = q.attempt_count + 1,
                   started_at = NOW(),
                   updated_at = NOW()
              FROM candidate
             WHERE q.id = candidate.id
            RETURNING q.*
            """,
            now_value,
            agent_id,
            _queue_owner_instance(agent_id),
            _lease_seconds(),
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
    owner_instance: str = "",
    owner_epoch: int | None = None,
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
            owner_instance=owner_instance,
            owner_epoch=owner_epoch,
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


async def complete_collection_item_async(
    item_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str = "",
    message: str = "",
    next_run_at: str = "",
    owner_instance: str = "",
    owner_epoch: int | None = None,
) -> dict[str, Any] | None:
    final_status = status if status in TERMINAL_STATUSES | {"action_required", "queued"} else "failed"
    if _db_enabled():
        return await _complete_db(
            item_id=item_id,
            status=final_status,
            result=result or {},
            error_code=error_code,
            message=message,
            next_run_at=next_run_at,
            owner_instance=owner_instance,
            owner_epoch=owner_epoch,
        )
    return complete_collection_item(
        item_id,
        status=final_status,
        result=result,
        error_code=error_code,
        message=message,
        next_run_at=next_run_at,
        owner_instance=owner_instance,
        owner_epoch=owner_epoch,
    )


async def _complete_db(
    *,
    item_id: str,
    status: str,
    result: dict[str, Any],
    error_code: str,
    message: str,
    next_run_at: str,
    owner_instance: str = "",
    owner_epoch: int | None = None,
) -> dict[str, Any] | None:
    await ensure_queue_storage_ready()
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
                   lease_agent_id = '',
                   owner_instance = '',
                   lease_expires_at = NULL,
                   updated_at = NOW()
             WHERE id = $1
               AND (
                    (status <> 'running' AND NULLIF($7, '') IS NULL AND $8 IS NULL)
                    OR (owner_instance = $7 AND owner_epoch = $8)
               )
            RETURNING *
            """,
            uuid.UUID(str(item_id)),
            status,
            json.dumps(result, ensure_ascii=False),
            error_code,
            message,
            next_run_at,
            owner_instance,
            owner_epoch,
        )
    return _row_to_item(row) if row else None


def queue_snapshot(limit: int = 50) -> list[dict[str, Any]]:
    db_rows = _run_db(_snapshot_db(limit=limit))
    if isinstance(db_rows, list):
        return db_rows
    rows = _read_json_queue()
    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rows[:limit]


async def queue_snapshot_async(limit: int = 50) -> list[dict[str, Any]]:
    if _db_enabled():
        return await _snapshot_db(limit=limit)
    return queue_snapshot(limit=limit)


async def _snapshot_db(*, limit: int) -> list[dict[str, Any]]:
    await ensure_queue_storage_ready()
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

    if _SYNC_LOOP is not None and asyncio.get_running_loop() is _SYNC_LOOP:
        # Synchronous callers (scripts, threadpool) must never share the API
        # server pool: it belongs to a different event loop.
        return await _ensure_sync_pool()
    try:
        return get_pool()
    except RuntimeError:
        return await init_pool()


async def _ensure_sync_pool() -> Any:
    """Small private asyncpg pool owned by the synchronous queue loop."""
    global _SYNC_POOL
    if _SYNC_POOL is not None and not getattr(_SYNC_POOL, "_closed", False):
        return _SYNC_POOL
    import asyncpg
    from app.core.db_pool import _db_url

    dsn = _db_url()
    if not dsn:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다")
    _SYNC_POOL = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=max(1, int(os.getenv("YEOLJEONG_QUEUE_SYNC_POOL_MAX_SIZE", "3") or 3)),
        timeout=10,
        command_timeout=30,
    )
    return _SYNC_POOL
