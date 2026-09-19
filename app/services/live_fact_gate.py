"""Fail-closed freshness gate for facts shown by OVIS Smart Browser.

Facts such as price, stock, delivery date, ranking, and operational status are
observations, not durable knowledge.  This module keeps source adapters behind
a server-side registry and removes a fact value from display whenever TTL,
source, entity/variant/account context, or evidence cannot be verified.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from app.core.db_pool import get_pool


class FreshnessStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


class LiveFactError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


Revalidator = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
_REVALIDATORS: dict[str, Revalidator] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
_VOLATILE_FIELDS = frozenset({
    "price", "stock", "inventory", "shipping", "shipping_date", "delivery_date",
    "seller", "rank", "ranking", "operational_status", "availability", "value",
})
_EVIDENCE_FIELDS = frozenset({
    "capture_id", "screenshot_id", "response_hash", "source_status", "selector",
    "trace_id", "reason", "error_type", "provider_version", "object_evidence_refs",
})


def register_fact_revalidator(key: str, revalidator: Revalidator) -> None:
    """Register an audited server callable; DB strings are never imported/eval'd."""
    normalized = str(key or "").strip().lower()
    if not normalized or not callable(revalidator):
        raise LiveFactError("INVALID_REVALIDATOR")
    _REVALIDATORS[normalized] = revalidator


def unregister_fact_revalidator(key: str) -> None:
    _REVALIDATORS.pop(str(key or "").strip().lower(), None)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: Any) -> Any:
    """Normalize asyncpg JSON/JSONB text without guessing about plain strings."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def value_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def hash_account_context(value: str) -> str:
    normalized = str(value or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def normalize_source_url(value: str) -> str:
    """Keep source identity while dropping credentials, query tokens, and fragments."""
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise LiveFactError("INVALID_FACT_SOURCE")
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme.lower(), f"{parsed.hostname.lower()}{port}", parsed.path or "/", "", ""))


def _evidence_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    evidence = {key: item for key, item in dict(value or {}).items() if key in _EVIDENCE_FIELDS}
    refs = evidence.get("object_evidence_refs")
    if refs is not None:
        from app.services.site_knowledge import evidence_refs

        evidence["object_evidence_refs"] = evidence_refs(refs if isinstance(refs, list) else [])
    return evidence


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _context_matches(record: Mapping[str, Any], expected: Mapping[str, Any] | None) -> bool:
    if not expected:
        return True
    for key in ("entity_key", "variant_key", "account_context_hash"):
        wanted = str(expected.get(key) or "")
        if wanted and wanted != str(record.get(key) or ""):
            return False
    return True


def display_fact(
    record: Mapping[str, Any], *, now: datetime | None = None,
    expected_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a display-safe fact. Non-current facts never include the value."""
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    status = str(record.get("freshness_status") or FreshnessStatus.UNAVAILABLE)
    observed_value = _json_value(record.get("observed_value", record.get("value")))
    observed_value_hash = str(
        record.get("observed_value_hash") or record.get("value_hash") or ""
    )
    expires_at = _as_utc(record.get("expires_at"))
    evidence_id = str(record.get("evidence_id") or "")
    if (
        not _context_matches(record, expected_context)
        or observed_value_hash
        and observed_value_hash != value_hash(observed_value)
    ):
        status = FreshnessStatus.CONFLICT
    elif not expires_at or expires_at <= instant:
        status = FreshnessStatus.STALE
    elif not evidence_id or not record.get("revalidated_at") or status not in {item.value for item in FreshnessStatus}:
        status = FreshnessStatus.UNAVAILABLE
    safe = {
        "fact_id": str(record.get("id") or record.get("fact_id") or ""),
        "fact_type": str(record.get("fact_type") or ""),
        "entity_key": str(record.get("entity_key") or ""),
        "variant_key": str(record.get("variant_key") or ""),
        "source_url": str(record.get("source_url") or ""),
        "observed_at": _as_utc(record.get("observed_at")).isoformat() if _as_utc(record.get("observed_at")) else None,
        "revalidated_at": _as_utc(record.get("revalidated_at")).isoformat() if _as_utc(record.get("revalidated_at")) else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "evidence_id": evidence_id or None,
        "freshness_status": str(status),
        "retry_action": {"action": "revalidate_fact", "fact_id": str(record.get("id") or "")},
    }
    safe["value"] = observed_value if status == FreshnessStatus.CURRENT else None
    return safe


async def record_live_fact(
    *, tenant_id: str, session_id: str | None, task_id: str | None, fact_type: str,
    entity_key: str, variant_key: str, account_context_hash: str, source_url: str,
    source_kind: str, revalidator_key: str, observed_value: Any, observed_at: datetime,
    expires_at: datetime, evidence_id: str, evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    observed = _as_utc(observed_at)
    expires = _as_utc(expires_at)
    if not observed or not expires or expires <= observed:
        raise LiveFactError("INVALID_FACT_TTL")
    if not all(str(item or "").strip() for item in (fact_type, entity_key, source_url, revalidator_key, evidence_id)):
        raise LiveFactError("INCOMPLETE_FACT_PROVENANCE")
    normalized_source_url = normalize_source_url(source_url)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO browser_live_facts
               (tenant_id,session_id,task_id,fact_type,entity_key,variant_key,account_context_hash,
                source_url,source_kind,revalidator_key,observed_value,observed_value_hash,
                observed_at,expires_at,revalidated_at,freshness_status,evidence_id,evidence)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,
                      $13,$14,$13,'CURRENT',$15,$16::jsonb) RETURNING *""",
            tenant_id, session_id, task_id, fact_type, entity_key, variant_key,
            account_context_hash, normalized_source_url, source_kind, revalidator_key,
            _canonical(observed_value), value_hash(observed_value), observed, expires,
            evidence_id, _canonical(_evidence_metadata(evidence)),
        )
    return display_fact(dict(row))


async def _load_fact(*, tenant_id: str, fact_id: str, conn: Any | None = None) -> dict[str, Any] | None:
    async def _fetch(connection: Any) -> dict[str, Any] | None:
        row = await connection.fetchrow(
            "SELECT * FROM browser_live_facts WHERE tenant_id=$1::uuid AND id=$2::uuid",
            tenant_id, UUID(str(fact_id)),
        )
        return dict(row) if row else None

    if conn is not None:
        return await _fetch(conn)
    async with get_pool().acquire() as connection:
        return await _fetch(connection)


async def revalidate_live_fact(
    *, tenant_id: str, fact_id: str, expected_context: Mapping[str, Any] | None = None,
    force: bool = True, now: datetime | None = None,
) -> dict[str, Any]:
    """Re-read one fact through its registered provider and persist evidence."""
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    lock = _LOCKS.setdefault(f"{tenant_id}:{fact_id}", asyncio.Lock())
    async with lock:
        record = await _load_fact(tenant_id=tenant_id, fact_id=fact_id)
        if not record:
            raise LiveFactError("FACT_NOT_FOUND")
        current = display_fact(record, now=instant, expected_context=expected_context)
        if not force and current["freshness_status"] == FreshnessStatus.CURRENT:
            return current
        provider = _REVALIDATORS.get(str(record.get("revalidator_key") or "").lower())
        if not provider:
            return await _persist_revalidation(
                record, status=FreshnessStatus.UNAVAILABLE, now=instant,
                evidence={"reason": "REVALIDATOR_UNAVAILABLE"},
            )
        try:
            observed = dict(await provider(dict(record)))
        except Exception as exc:  # noqa: BLE001 - provider failures must fail closed.
            return await _persist_revalidation(
                record, status=FreshnessStatus.UNAVAILABLE, now=instant,
                evidence={"reason": "SOURCE_READ_FAILED", "error_type": type(exc).__name__},
            )
        context_ok = _context_matches(observed, {
            "entity_key": record.get("entity_key"),
            "variant_key": record.get("variant_key"),
            "account_context_hash": record.get("account_context_hash"),
        })
        try:
            observed_source = normalize_source_url(str(observed.get("source_url") or ""))
        except LiveFactError:
            observed_source = ""
        source_ok = observed_source == str(record.get("source_url") or "")
        evidence_ok = bool(str(observed.get("evidence_id") or "").strip())
        status = FreshnessStatus.CURRENT if context_ok and source_ok and evidence_ok else FreshnessStatus.CONFLICT
        if not evidence_ok:
            status = FreshnessStatus.UNAVAILABLE
        return await _persist_revalidation(record, status=status, now=instant, evidence=observed)


async def _persist_revalidation(
    record: Mapping[str, Any], *, status: FreshnessStatus, now: datetime,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    value = (
        evidence.get("value")
        if status == FreshnessStatus.CURRENT
        else _json_value(record.get("observed_value"))
    )
    observed_at = _as_utc(evidence.get("observed_at")) or now
    expires_at = _as_utc(evidence.get("expires_at")) or record.get("expires_at")
    evidence_id = str(evidence.get("evidence_id") or record.get("evidence_id") or "")
    tenant_id = str(record["tenant_id"])
    fact_id = str(record["id"])
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """UPDATE browser_live_facts SET observed_value=$3::jsonb,observed_value_hash=$4,
                   observed_at=$5,expires_at=$6,revalidated_at=$7,freshness_status=$8,
                   evidence_id=NULLIF($9,''),evidence=$10::jsonb,version=version+1,updated_at=NOW()
                   WHERE tenant_id=$1::uuid AND id=$2::uuid AND version=$11 RETURNING *""",
            tenant_id, UUID(fact_id), _canonical(value), value_hash(value), observed_at,
            expires_at, now, status.value, evidence_id, _canonical(_evidence_metadata(evidence)),
            int(record.get("version") or 1),
        )
        if not row:
            winner = await _load_fact(tenant_id=tenant_id, fact_id=fact_id, conn=conn)
            if not winner:
                raise LiveFactError("FACT_NOT_FOUND")
            return display_fact(winner, now=now)
        await conn.execute(
            """INSERT INTO browser_live_fact_events
                   (tenant_id,fact_id,status,value_hash,evidence_id,evidence,observed_at)
                   VALUES($1::uuid,$2::uuid,$3,$4,NULLIF($5,''),$6::jsonb,$7)""",
            tenant_id, UUID(fact_id), status.value, value_hash(value), evidence_id,
            _canonical(_evidence_metadata(evidence)), now,
        )
    return display_fact(dict(row), now=now)


def _collect_fact_ids(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        refs = value.get("live_fact_ids")
        if isinstance(refs, list):
            result.update(str(item) for item in refs if item)
        fact_id = value.get("fact_id") if "freshness_status" in value or "live_fact" in value else None
        if fact_id:
            result.add(str(fact_id))
        for item in value.values():
            result.update(_collect_fact_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_fact_ids(item))
    return result


def sanitize_inline_facts(value: Any, *, now: datetime | None = None) -> Any:
    """Strip expired inline facts from cached/SSE replay without source I/O."""
    if isinstance(value, list):
        return [sanitize_inline_facts(item, now=now) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {key: sanitize_inline_facts(item, now=now) for key, item in value.items()}
    if "freshness_status" in result and ("fact_id" in result or "fact_type" in result):
        safe = display_fact(result, now=now)
        return safe
    return result


def sanitize_sse_event(event_data: str, *, now: datetime | None = None) -> str:
    """Apply the same fail-closed TTL rule when a cached SSE frame is replayed."""
    prefix = "data: " if event_data.startswith("data: ") else ""
    suffix = "\n\n" if event_data.endswith("\n\n") else ""
    raw = event_data[len(prefix):]
    if suffix:
        raw = raw[:-len(suffix)]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return event_data
    return f"{prefix}{json.dumps(sanitize_inline_facts(parsed, now=now), ensure_ascii=False)}{suffix}"


async def guard_payload_for_display(
    payload: Mapping[str, Any], *, tenant_id: str,
    expected_context: Mapping[str, Any] | None = None, force: bool = True,
) -> dict[str, Any]:
    """Revalidate referenced facts and return a serializer-safe payload copy."""
    result = deepcopy(dict(payload))
    fact_ids = sorted(_collect_fact_ids(result))
    if not fact_ids:
        return sanitize_inline_facts(result)
    facts: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        try:
            facts.append(await revalidate_live_fact(
                tenant_id=tenant_id, fact_id=fact_id,
                expected_context=expected_context, force=force,
            ))
        except (LiveFactError, ValueError):
            facts.append({
                "fact_id": fact_id, "value": None,
                "freshness_status": FreshnessStatus.UNAVAILABLE,
                "retry_action": {"action": "revalidate_fact", "fact_id": fact_id},
            })
    blocked = any(item["freshness_status"] != FreshnessStatus.CURRENT for item in facts)
    if blocked:
        _redact_volatile_values(result)
    result["freshness_gate"] = {
        "status": "BLOCKED" if blocked else "CURRENT",
        "facts": facts,
        "retry_action": {"action": "revalidate_all", "fact_ids": fact_ids} if blocked else None,
    }
    return sanitize_inline_facts(result)


def _redact_volatile_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key.lower() in _VOLATILE_FIELDS:
                value[key] = None
            else:
                _redact_volatile_values(item)
    elif isinstance(value, list):
        for item in value:
            _redact_volatile_values(item)
