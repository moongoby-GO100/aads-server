"""OHVIS 내부 LangSmith-compatible LLMOps 저장/조회 계층.

`migrations/163_ohvis_internal_llmops_foundation.sql`이 만든 `llmops_*` 테이블을
읽고 쓴다. 설계 원칙은 `ohvis_harness_trace`와 동일하다.

- **trace 기록은 절대 호출부를 깨뜨리지 않는다.** 실패하면 None/False를 돌려줄 뿐이다.
- 테이블이 없으면 (마이그레이션 미적용) 경고 1회 후 조용히 skip한다.
- 조회는 `llmops_traces`를 우선하고, 마이그레이션 이전 기록은
  `ohvis_harness_traces`로 폴백해 기존 기록이 사라져 보이지 않게 한다.
- 원문이 아니라 마스킹된 요약만 저장한다. trace가 본문 저장소가 되면 안 된다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

TRACE_TABLE = "llmops_traces"
SPAN_TABLE = "llmops_spans"
TOOL_CALL_TABLE = "llmops_tool_calls"
DATASET_TABLE = "llmops_datasets"
EXAMPLE_TABLE = "llmops_examples"
EXPERIMENT_TABLE = "llmops_experiments"
SCORE_TABLE = "llmops_scores"
FEEDBACK_TABLE = "llmops_feedback"
INGEST_CLIENT_TABLE = "llmops_ingest_clients"
LEGACY_TRACE_TABLE = "ohvis_harness_traces"

LLMOPS_TABLES = (
    TRACE_TABLE,
    SPAN_TABLE,
    TOOL_CALL_TABLE,
    DATASET_TABLE,
    EXAMPLE_TABLE,
    EXPERIMENT_TABLE,
    SCORE_TABLE,
    FEEDBACK_TABLE,
)

SUMMARY_LIMIT = 2000
ERROR_LIMIT = 1000
MAX_LIMIT = 200
DEFAULT_FAILURE_DATASET = "aads-failed-traces"
QUALITY_FLOOR = 0.4
INGEST_SCHEMA_VERSION = "1.0"
MAX_INGEST_PAYLOAD_BYTES = 65_536
MAX_INGEST_TOOL_CALLS = 50

_SENSITIVE_METADATA_KEY = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|api[_-]?key|auth[_-]?token|access[_-]?token)"
)

# 요약 저장 전 마스킹 — 원문에 섞여 들어온 시크릿이 DB나 외부 export로 새지 않게 한다.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "AIza***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh_***"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "xox-***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "jwt.***"),
    (re.compile(r"postgres(?:ql)?://[^\s:/]+:[^\s@]+@"), "postgres://***:***@"),
    (re.compile(r"(?i)\b(api[_-]?key|auth[_-]?token|secret|password|passwd)\b\s*[:=]\s*\S+"), r"\1=***"),
)

# 존재가 확인된 relation만 캐시한다 (부재는 캐시하지 않는다 — relation_exists 참고)
_relation_present: dict[str, bool] = {}
# 부재 경고를 relation당 1회로 줄이기 위한 기록
_relation_missing_warned: set[str] = set()


def reset_relation_cache() -> None:
    """relation 존재 캐시 초기화 (테스트/마이그레이션 직후용)."""
    _relation_present.clear()
    _relation_missing_warned.clear()


def mask_secrets(text: str) -> str:
    """요약 문자열에서 알려진 시크릿 패턴을 마스킹한다."""
    masked = text
    for pattern, replacement in _SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def clip(value: Any, limit: int = SUMMARY_LIMIT) -> str:
    """요약을 마스킹하고 길이를 제한한다."""
    text = "" if value is None else str(value)
    text = mask_secrets(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _as_uuid_text(value: Any) -> Optional[str]:
    """UUID로 해석 가능하면 문자열로, 아니면 None (::uuid 캐스팅 실패 방지)."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_json(value: Any, default: str = "{}") -> str:
    if value is None:
        return default
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return default


def loads_json(value: Any) -> dict[str, Any]:
    """asyncpg가 dict 또는 str로 돌려주는 JSONB를 dict로 정규화한다."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def ingest_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def redact_ingest_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact external metadata before it reaches the ledger."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key)[:120]: (
                "[redacted]"
                if _SENSITIVE_METADATA_KEY.search(str(key))
                else redact_ingest_value(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [redact_ingest_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return clip(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clip(value)


def validate_ingest_payload_size(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) > MAX_INGEST_PAYLOAD_BYTES:
        raise ValueError("payload_too_large")


async def authenticate_ingest_client(token: str) -> dict[str, str] | None:
    """Resolve an active client while comparing token digests in constant time."""
    if not token:
        return None
    from app.core.db_pool import get_pool

    digest = ingest_token_digest(token)
    rows = await get_pool().fetch(
        f"SELECT client_id, project, token_hash FROM {INGEST_CLIENT_TABLE} WHERE is_active = TRUE"
    )
    matched: dict[str, str] | None = None
    for row in rows:
        if hmac.compare_digest(str(row["token_hash"]), digest):
            matched = {"client_id": str(row["client_id"]), "project": str(row["project"])}
    return matched


async def mark_ingest_client_used(client_id: str) -> None:
    from app.core.db_pool import get_pool

    await get_pool().execute(
        f"UPDATE {INGEST_CLIENT_TABLE} SET last_used_at = NOW(), updated_at = NOW() WHERE client_id = $1",
        client_id,
    )


async def provision_ingest_client(*, client_id: str, project: str, created_by: str) -> dict[str, Any]:
    """Create/rotate a client and return the raw credential exactly once."""
    from app.core.db_pool import get_pool

    raw_token = "ohvis_ingest_" + secrets.token_urlsafe(32)
    row = await get_pool().fetchrow(
        f"""
        INSERT INTO {INGEST_CLIENT_TABLE} (client_id, project, token_hash, is_active, created_by)
        VALUES ($1, $2, $3, TRUE, $4)
        ON CONFLICT (client_id) DO UPDATE SET
            project = EXCLUDED.project,
            token_hash = EXCLUDED.token_hash,
            is_active = TRUE,
            created_by = EXCLUDED.created_by,
            rotated_at = NOW(),
            revoked_at = NULL,
            updated_at = NOW()
        RETURNING client_id, project, created_at, rotated_at
        """,
        client_id,
        project,
        ingest_token_digest(raw_token),
        created_by,
    )
    return {
        "client_id": str(row["client_id"]),
        "project": str(row["project"]),
        "token": raw_token,
        "token_notice": "shown_once",
        "created_at": row["created_at"],
        "rotated_at": row["rotated_at"],
    }


async def revoke_ingest_client(client_id: str) -> bool:
    from app.core.db_pool import get_pool

    result = await get_pool().execute(
        f"UPDATE {INGEST_CLIENT_TABLE} SET is_active = FALSE, revoked_at = NOW(), updated_at = NOW() "
        "WHERE client_id = $1 AND is_active = TRUE",
        client_id,
    )
    return result.endswith(" 1")


async def ingest_external_trace(payload: dict[str, Any], *, client_id: str) -> dict[str, Any]:
    """Atomically ingest one external trace; a replay performs no child writes."""
    from app.core.db_pool import get_pool

    validate_ingest_payload_size(payload)
    project = str(payload["project"])
    external_trace_id = str(payload["external_trace_id"])
    trace_key = f"external:{project}:{external_trace_id}"
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", trace_key)
            existing = await conn.fetchrow(
                f"SELECT id::text AS id, created_at FROM {TRACE_TABLE} WHERE trace_key = $1",
                trace_key,
            )
            if existing:
                return {
                    "accepted": True,
                    "deduplicated": True,
                    "external_trace_id": external_trace_id,
                    "trace_id": str(existing["id"]),
                    "project": project,
                    "schema_version": INGEST_SCHEMA_VERSION,
                    "ingested_at": existing["created_at"],
                }

            safe_metadata = redact_ingest_value(payload.get("metadata") or {})
            safe_metadata["ingest_client_id"] = client_id
            safe_metadata["ingest_schema_version"] = INGEST_SCHEMA_VERSION
            trace_id = await record_trace(
                graph_run_id=str(payload.get("graph_run_id") or external_trace_id),
                project=project,
                session_id=payload.get("session_id"),
                source="external_ingest",
                run_type=str(payload.get("run_type") or "chain"),
                status=str(payload.get("status") or "success"),
                model=payload.get("model"),
                input_summary=payload.get("input_summary") or "",
                output_summary=payload.get("output_summary") or "",
                latency_ms=payload.get("latency_ms"),
                cost_usd=payload.get("cost_usd"),
                quality_score=payload.get("quality_score"),
                error=payload.get("error"),
                external_trace_id=external_trace_id,
                tags=list(payload.get("tags") or []),
                metadata=safe_metadata,
                trace_key=trace_key,
                tool_calls=redact_ingest_value(payload.get("tool_calls") or []),
                conn=conn,
            )
            if not trace_id:
                raise RuntimeError("trace_store_unavailable")
            return {
                "accepted": True,
                "deduplicated": False,
                "external_trace_id": external_trace_id,
                "trace_id": trace_id,
                "project": project,
                "schema_version": INGEST_SCHEMA_VERSION,
                "ingested_at": datetime.now(timezone.utc),
            }


def _note_missing(relation: str) -> None:
    """부재는 프로세스당 1회만 경고한다 (매 쓰기마다 로그가 쌓이면 안 된다)."""
    if relation in _relation_missing_warned:
        return
    _relation_missing_warned.add(relation)
    logger.warning(
        "%s is missing; LLMOps writes are skipped until migrations 163/164 are applied", relation
    )


async def relation_exists(conn: Any, relation: str) -> bool:
    """public 스키마에 해당 테이블이 있는지 (존재만 캐시됨).

    **부재는 캐시하지 않는다.** 마이그레이션은 서버가 떠 있는 동안 적용되므로,
    부재를 캐시하면 163/164를 적용한 뒤에도 프로세스를 재시작할 때까지 적재가
    조용히 skip된다. 부재 상태의 재조회 비용(쓰기당 1쿼리)은 그 사고보다 싸다.
    """
    if _relation_present.get(relation):
        return True
    present = bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='public' AND table_name=$1
            )
            """,
            relation,
        )
    )
    if present:
        _relation_present[relation] = True
    else:
        _note_missing(relation)
    return present


async def relations_exist(conn: Any, relations: Sequence[str]) -> dict[str, bool]:
    """여러 relation의 존재 여부를 한 번의 왕복으로 확인한다.

    상태 엔드포인트는 대시보드가 주기적으로 호출한다. relation당 1쿼리로 돌면
    테이블이 늘어날 때마다 왕복이 그대로 늘어난다.
    """
    wanted = list(dict.fromkeys(relations))
    rows = await conn.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name = ANY($1::text[])
        """,
        wanted,
    )
    present = {row["table_name"] for row in rows}
    state = {relation: relation in present for relation in wanted}
    for relation, exists in state.items():
        if exists:
            _relation_present[relation] = True
        else:
            _note_missing(relation)
    return state


class PoolConn:
    """호출부 커넥션이 있으면 재사용하고, 없을 때만 풀에서 acquire하는 헬퍼.

    이미 커넥션을 점유한 핸들러 안에서 중첩 acquire를 하면 풀이 고갈된다.
    """

    def __init__(self, conn: Any = None):
        self._conn = conn
        self._ctx = None

    async def __aenter__(self):
        if self._conn is not None:
            return self._conn
        from app.core.db_pool import get_pool

        self._ctx = get_pool().acquire()
        return await self._ctx.__aenter__()

    async def __aexit__(self, *exc_info):
        if self._ctx is not None:
            return await self._ctx.__aexit__(*exc_info)
        return False


def classify_error(error: Optional[str]) -> Optional[str]:
    """에러 문자열을 안정적인 버킷으로 분류한다 (평가/집계 기준)."""
    if not error:
        return None
    lowered = str(error).lower()
    buckets = (
        ("timeout", ("timeout", "timed out", "deadline")),
        ("auth", ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication")),
        ("rate_limit", ("429", "rate limit", "too many requests", "overloaded")),
        ("not_found", ("404", "not found", "does not exist")),
        ("db", ("asyncpg", "postgres", "duplicate key", "deadlock", "relation")),
        ("network", ("connection", "econnrefused", "dns", "unreachable", "ssl")),
        ("tool", ("permission denied", "command failed", "exit code", "tool")),
        ("validation", ("validation", "pydantic", "invalid", "schema")),
    )
    for name, needles in buckets:
        if any(needle in lowered for needle in needles):
            return name
    return "unknown"


# ── write path ──────────────────────────────────────────────────────────────

_INSERT_TRACE_SQL = f"""
INSERT INTO {TRACE_TABLE} (
    trace_id, trace_key, graph_run_id, project, session_id, ohvis_task_id,
    source, run_type, status, model, input_summary, output_summary,
    latency_ms, cost_usd, quality_score, error, error_class,
    external_trace_id, tags, metadata, ended_at
)
VALUES (
    COALESCE(NULLIF($17, ''), NULLIF($1, ''), gen_random_uuid()::text),
    $1, $2, $3, $4::uuid, $5::uuid,
    $6, $7, $8, $9, $10, $11,
    $12, $13, $14, $15, $16,
    $17, $18::text[], $19::jsonb, $20
)
ON CONFLICT (trace_key) WHERE trace_key IS NOT NULL DO UPDATE
SET status = EXCLUDED.status,
    output_summary = EXCLUDED.output_summary,
    latency_ms = COALESCE(EXCLUDED.latency_ms, {TRACE_TABLE}.latency_ms),
    cost_usd = COALESCE(EXCLUDED.cost_usd, {TRACE_TABLE}.cost_usd),
    quality_score = COALESCE(EXCLUDED.quality_score, {TRACE_TABLE}.quality_score),
    error = EXCLUDED.error,
    error_class = EXCLUDED.error_class,
    metadata = {TRACE_TABLE}.metadata || EXCLUDED.metadata,
    ended_at = COALESCE(EXCLUDED.ended_at, {TRACE_TABLE}.ended_at)
RETURNING id
"""


async def record_trace(
    *,
    graph_run_id: str,
    project: Optional[str] = None,
    session_id: Optional[str] = None,
    ohvis_task_id: Optional[str] = None,
    source: str = "internal",
    run_type: str = "chain",
    status: Optional[str] = None,
    model: Optional[str] = None,
    input_summary: Any = "",
    output_summary: Any = "",
    latency_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    quality_score: Optional[float] = None,
    error: Optional[str] = None,
    external_trace_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
    trace_key: Optional[str] = None,
    tool_calls: Optional[list[Any]] = None,
    conn: Any = None,
) -> Optional[str]:
    """trace 1건을 기록하고 trace id를 돌려준다. 실패/skip이면 None (예외 없음).

    `trace_key`를 주면 같은 키의 재기록이 새 행을 만들지 않고 갱신된다
    (mirror/재시도 멱등성).
    """
    if not graph_run_id:
        return None

    resolved_status = status or ("error" if error else "success")

    try:
        async with PoolConn(conn) as target:
            if not await relation_exists(target, TRACE_TABLE):
                return None
            trace_id = await target.fetchval(
                _INSERT_TRACE_SQL,
                (str(trace_key)[:200] if trace_key else None),
                str(graph_run_id)[:200],
                (project or None),
                _as_uuid_text(session_id),
                _as_uuid_text(ohvis_task_id),
                (source or "internal")[:40],
                (run_type or "chain")[:60],
                resolved_status[:30],
                (str(model)[:120] if model else None),
                clip(input_summary),
                clip(output_summary),
                int(latency_ms) if latency_ms is not None else None,
                float(cost_usd) if cost_usd is not None else None,
                float(quality_score) if quality_score is not None else None,
                clip(error, ERROR_LIMIT) if error else None,
                classify_error(error),
                (str(external_trace_id)[:200] if external_trace_id else None),
                [str(tag)[:60] for tag in (tags or [])],
                _as_json(metadata),
                None,
            )
            if trace_id and tool_calls:
                await record_tool_calls(target, str(trace_id), tool_calls)
            return str(trace_id) if trace_id else None
    except Exception as exc:  # noqa: BLE001 — trace는 절대 호출부를 깨뜨리지 않는다
        logger.warning("llmops trace insert failed (non-fatal): %s", str(exc)[:200])
        return None


def normalize_tool_call(call: Any, index: int = 0) -> dict[str, Any]:
    """다양한 모양의 tool call 표현을 하나의 스키마로 정규화한다."""
    payload = call if isinstance(call, dict) else {"tool_name": str(call)}
    latency = payload.get("latency_ms")
    try:
        latency_ms = int(latency) if latency is not None else None
    except (TypeError, ValueError):
        latency_ms = None
    return {
        "tool_name": str(payload.get("tool_name") or payload.get("name") or "unknown")[:120],
        "risk_tier": str(payload.get("risk_tier") or "read")[:30],
        "approval_state": str(payload.get("approval_state") or "not_required")[:30],
        "status": str(payload.get("status") or ("error" if payload.get("error") else "success"))[:30],
        "sequence": int(payload.get("sequence", index)),
        "input_summary": clip(payload.get("input") or payload.get("input_summary") or ""),
        "output_summary": clip(payload.get("output") or payload.get("output_summary") or ""),
        "latency_ms": latency_ms,
        "error": clip(payload.get("error"), ERROR_LIMIT) if payload.get("error") else None,
        "metadata": payload.get("metadata") or {},
    }


async def record_tool_calls(conn: Any, trace_id: str, tool_calls: list[Any]) -> int:
    """trace에 딸린 tool call들을 기록한다. 실패해도 예외를 올리지 않는다."""
    if not await relation_exists(conn, TOOL_CALL_TABLE):
        return 0
    inserted = 0
    for index, call in enumerate(tool_calls or []):
        item = normalize_tool_call(call, index)
        try:
            await conn.execute(
                f"""
                INSERT INTO {TOOL_CALL_TABLE} (
                    trace_id, tool_name, risk_tier, approval_state, status,
                    sequence, input_summary, output_summary, latency_ms, error, metadata
                )
                VALUES ($1::text, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                """,
                trace_id,
                item["tool_name"],
                item["risk_tier"],
                item["approval_state"],
                item["status"],
                item["sequence"],
                item["input_summary"],
                item["output_summary"],
                item["latency_ms"],
                item["error"],
                _as_json(item["metadata"]),
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("llmops tool_call insert failed (non-fatal): %s", str(exc)[:200])
    return inserted


async def record_span(
    *,
    trace_id: str,
    name: str,
    span_type: str = "chain",
    parent_span_id: Optional[str] = None,
    sequence: int = 0,
    status: Optional[str] = None,
    model: Optional[str] = None,
    input_summary: Any = "",
    output_summary: Any = "",
    latency_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    error: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> Optional[str]:
    """span 1건을 기록하고 span id를 돌려준다. 실패/skip이면 None."""
    resolved_trace_id = _as_uuid_text(trace_id)
    if not resolved_trace_id:
        return None
    try:
        async with PoolConn(conn) as target:
            if not await relation_exists(target, SPAN_TABLE):
                return None
            span_id = await target.fetchval(
                f"""
                INSERT INTO {SPAN_TABLE} (
                    trace_id, parent_span_id, span_type, name, sequence, status,
                    model, input_summary, output_summary, latency_ms, cost_usd, error, metadata
                )
                VALUES ($1::text, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb)
                RETURNING id
                """,
                resolved_trace_id,
                _as_uuid_text(parent_span_id),
                (span_type or "chain")[:60],
                str(name or "")[:200],
                int(sequence),
                (status or ("error" if error else "success"))[:30],
                (str(model)[:120] if model else None),
                clip(input_summary),
                clip(output_summary),
                int(latency_ms) if latency_ms is not None else None,
                float(cost_usd) if cost_usd is not None else None,
                clip(error, ERROR_LIMIT) if error else None,
                _as_json(metadata),
            )
            return str(span_id) if span_id else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops span insert failed (non-fatal): %s", str(exc)[:200])
        return None


async def record_feedback(
    *,
    trace_id: Optional[str] = None,
    source_ref: Optional[str] = None,
    rating: Optional[int] = None,
    label: Optional[str] = None,
    comment: str = "",
    created_by: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> bool:
    """CEO/사용자 피드백 1건을 기록한다. 실패/skip이면 False."""
    try:
        async with PoolConn(conn) as target:
            if not await relation_exists(target, FEEDBACK_TABLE):
                return False
            await target.execute(
                f"""
                INSERT INTO {FEEDBACK_TABLE}
                    (trace_id, source_ref, rating, label, comment, created_by, metadata)
                VALUES ($1::text, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                _as_uuid_text(trace_id),
                (str(source_ref)[:200] if source_ref else None),
                int(rating) if rating is not None else None,
                (str(label)[:60] if label else None),
                clip(comment),
                (str(created_by)[:120] if created_by else None),
                _as_json(metadata),
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops feedback insert failed (non-fatal): %s", str(exc)[:200])
        return False


# ── read path ───────────────────────────────────────────────────────────────


def _row_to_trace(row: Any, source_table: str) -> dict[str, Any]:
    data = dict(row)
    cost = data.get("cost_usd")
    created = data.get("created_at")
    return {
        "id": str(data.get("id")),
        "source_table": source_table,
        "graph_run_id": data.get("graph_run_id"),
        "project": data.get("project"),
        "session_id": str(data["session_id"]) if data.get("session_id") else None,
        "ohvis_task_id": str(data["ohvis_task_id"]) if data.get("ohvis_task_id") else None,
        "source": data.get("source") or data.get("provider"),
        "run_type": data.get("run_type"),
        "status": data.get("status"),
        "model": data.get("model"),
        "input_summary": data.get("input_summary"),
        "output_summary": data.get("output_summary"),
        "latency_ms": data.get("latency_ms"),
        "cost_usd": float(cost) if cost is not None else None,
        "quality_score": data.get("quality_score"),
        "error": data.get("error"),
        "error_class": data.get("error_class") or classify_error(data.get("error")),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


async def list_traces(
    *,
    project: Optional[str] = None,
    status: Optional[str] = None,
    graph_run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    hours: int = 24,
    limit: int = 50,
    include_legacy: bool = True,
) -> dict[str, Any]:
    """trace 목록을 최신순으로 반환한다.

    `llmops_traces`를 우선 읽고, 마이그레이션 이전 기록도 보이도록
    `ohvis_harness_traces`를 함께 읽어 합친다 (v1 호환 폴백).
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
    except Exception as exc:  # noqa: BLE001
        return {"traces": [], "count": 0, "available": False, "error": str(exc)[:200]}

    traces: list[dict[str, Any]] = []
    sources: list[str] = []
    try:
        async with pool.acquire() as conn:
            if await relation_exists(conn, TRACE_TABLE):
                rows = await conn.fetch(
                    f"""
                    SELECT id, graph_run_id, project, session_id, ohvis_task_id, source,
                           run_type, status, model, input_summary, output_summary,
                           latency_ms, cost_usd, quality_score, error, error_class, created_at
                    FROM {TRACE_TABLE}
                    WHERE ($1::text IS NULL OR project = $1)
                      AND ($2::text IS NULL OR status = $2)
                      AND ($3::text IS NULL OR graph_run_id = $3)
                      AND ($4::uuid IS NULL OR session_id = $4::uuid)
                      AND created_at >= NOW() - MAKE_INTERVAL(hours => $5)
                    ORDER BY created_at DESC
                    LIMIT $6
                    """,
                    project,
                    status,
                    graph_run_id,
                    _as_uuid_text(session_id),
                    int(hours),
                    limit,
                )
                sources.append(TRACE_TABLE)
                traces.extend(_row_to_trace(row, TRACE_TABLE) for row in rows)

            if include_legacy and len(traces) < limit and await relation_exists(conn, LEGACY_TRACE_TABLE):
                rows = await conn.fetch(
                    f"""
                    SELECT id, graph_run_id, project, session_id, ohvis_task_id, provider,
                           run_type, input_summary, output_summary, latency_ms, cost_usd,
                           error, created_at,
                           CASE WHEN error IS NULL OR error = '' THEN 'success' ELSE 'error' END AS status
                    FROM {LEGACY_TRACE_TABLE}
                    WHERE ($1::text IS NULL OR project = $1)
                      AND ($2::text IS NULL OR graph_run_id = $2)
                      AND ($3::uuid IS NULL OR session_id = $3::uuid)
                      AND created_at >= NOW() - MAKE_INTERVAL(hours => $4)
                    ORDER BY created_at DESC
                    LIMIT $5
                    """,
                    project,
                    graph_run_id,
                    _as_uuid_text(session_id),
                    int(hours),
                    limit,
                )
                sources.append(LEGACY_TRACE_TABLE)
                legacy = [_row_to_trace(row, LEGACY_TRACE_TABLE) for row in rows]
                if status:
                    legacy = [item for item in legacy if item["status"] == status]
                traces.extend(legacy)
    except Exception as exc:  # noqa: BLE001
        return {"traces": [], "count": 0, "available": False, "error": str(exc)[:200]}

    traces.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {
        "traces": traces[:limit],
        "count": len(traces[:limit]),
        "available": True,
        "sources": sources,
    }


async def get_trace(trace_id: str) -> Optional[dict[str, Any]]:
    """trace 상세 + span/tool call/feedback을 반환한다. 없으면 None.

    v2 `llmops_traces`에 없으면 v1 `ohvis_harness_traces`에서 찾는다.
    """
    resolved = _as_uuid_text(trace_id)
    if not resolved:
        return None
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
    except Exception:  # noqa: BLE001
        return None

    try:
        async with pool.acquire() as conn:
            row = None
            if await relation_exists(conn, TRACE_TABLE):
                row = await conn.fetchrow(
                    f"""
                    SELECT id, graph_run_id, project, session_id, ohvis_task_id, source,
                           run_type, status, model, input_summary, output_summary,
                           latency_ms, cost_usd, quality_score, error, error_class,
                           external_trace_id, tags, metadata, created_at
                    FROM {TRACE_TABLE} WHERE id = $1::uuid
                    """,
                    resolved,
                )
            if row is not None:
                trace = _row_to_trace(row, TRACE_TABLE)
                trace["tags"] = list(row["tags"] or [])
                trace["external_trace_id"] = row["external_trace_id"]
                trace["metadata"] = loads_json(row["metadata"])
                trace["spans"] = await _fetch_spans(conn, resolved)
                trace["tool_calls"] = await _fetch_tool_calls(conn, resolved)
                trace["feedback"] = await _fetch_feedback(conn, resolved)
                return trace

            if not await relation_exists(conn, LEGACY_TRACE_TABLE):
                return None
            legacy_row = await conn.fetchrow(
                f"""
                SELECT id, graph_run_id, project, session_id, ohvis_task_id, provider,
                       run_type, input_summary, output_summary, latency_ms, cost_usd,
                       error, tool_calls, metadata, created_at,
                       CASE WHEN error IS NULL OR error = '' THEN 'success' ELSE 'error' END AS status
                FROM {LEGACY_TRACE_TABLE} WHERE id = $1::uuid
                """,
                resolved,
            )
            if legacy_row is None:
                return None
            trace = _row_to_trace(legacy_row, LEGACY_TRACE_TABLE)
            trace["tags"] = []
            trace["external_trace_id"] = None
            trace["metadata"] = loads_json(legacy_row["metadata"])
            trace["spans"] = []
            trace["tool_calls"] = parse_legacy_tool_calls(legacy_row["tool_calls"])
            trace["feedback"] = []
            return trace
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops trace fetch failed: %s", str(exc)[:200])
        return None


def parse_legacy_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """v1 trace의 JSONB tool_calls를 v2 tool call 모양으로 정규화한다."""
    if not raw:
        return []
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, list):
        return []
    return [normalize_tool_call(item, index) for index, item in enumerate(payload)]


async def _fetch_spans(conn: Any, trace_id: str) -> list[dict[str, Any]]:
    if not await relation_exists(conn, SPAN_TABLE):
        return []
    rows = await conn.fetch(
        f"""
        SELECT id, parent_span_id, span_type, name, sequence, status, model,
               input_summary, output_summary, latency_ms, cost_usd, error, started_at
        FROM {SPAN_TABLE} WHERE trace_id::text = $1::text
        ORDER BY sequence, started_at
        """,
        trace_id,
    )
    spans: list[dict[str, Any]] = []
    for row in rows:
        cost = row["cost_usd"]
        spans.append({
            "id": str(row["id"]),
            "parent_span_id": str(row["parent_span_id"]) if row["parent_span_id"] else None,
            "span_type": row["span_type"],
            "name": row["name"],
            "sequence": row["sequence"],
            "status": row["status"],
            "model": row["model"],
            "input_summary": row["input_summary"],
            "output_summary": row["output_summary"],
            "latency_ms": row["latency_ms"],
            "cost_usd": float(cost) if cost is not None else None,
            "error": row["error"],
        })
    return spans


async def _fetch_tool_calls(conn: Any, trace_id: str) -> list[dict[str, Any]]:
    if not await relation_exists(conn, TOOL_CALL_TABLE):
        return []
    rows = await conn.fetch(
        f"""
        SELECT id, span_id, tool_name, risk_tier, approval_state, status,
               sequence, input_summary, output_summary, latency_ms, error
        FROM {TOOL_CALL_TABLE} WHERE trace_id::text = $1::text
        ORDER BY sequence, id
        """,
        trace_id,
    )
    return [
        {
            "id": str(row["id"]),
            "span_id": str(row["span_id"]) if row["span_id"] else None,
            "tool_name": row["tool_name"],
            "risk_tier": row["risk_tier"],
            "approval_state": row["approval_state"],
            "status": row["status"],
            "sequence": row["sequence"],
            "input_summary": row["input_summary"],
            "output_summary": row["output_summary"],
            "latency_ms": row["latency_ms"],
            "error": row["error"],
        }
        for row in rows
    ]


async def _fetch_feedback(conn: Any, trace_id: str) -> list[dict[str, Any]]:
    if not await relation_exists(conn, FEEDBACK_TABLE):
        return []
    rows = await conn.fetch(
        f"""
        SELECT id, rating, label, comment, created_by, created_at
        FROM {FEEDBACK_TABLE} WHERE trace_id::text = $1::text
        ORDER BY created_at DESC LIMIT 50
        """,
        trace_id,
    )
    return [
        {
            "id": str(row["id"]),
            "rating": row["rating"],
            "label": row["label"],
            "comment": row["comment"],
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]


# ── dataset promotion ───────────────────────────────────────────────────────


async def ensure_dataset(
    conn: Any,
    *,
    slug: str,
    project: Optional[str] = None,
    title: str = "",
    purpose: str = "",
) -> Optional[str]:
    """dataset을 slug 기준으로 확보하고 id를 반환한다 (idempotent)."""
    if not await relation_exists(conn, DATASET_TABLE):
        return None
    dataset_id = await conn.fetchval(
        f"""
        INSERT INTO {DATASET_TABLE} (slug, project, title, purpose)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (slug) DO UPDATE SET updated_at = NOW()
        RETURNING id
        """,
        str(slug)[:120],
        project,
        (title or slug)[:200],
        purpose[:1000],
    )
    return str(dataset_id) if dataset_id else None


def is_promotable(trace: dict[str, Any], quality_floor: float = QUALITY_FLOOR) -> bool:
    """실패했거나 품질 하한 미만인 trace만 dataset으로 승격한다 (FR-004)."""
    if (trace.get("status") or "success") == "error" or trace.get("error"):
        return True
    score = trace.get("quality_score")
    try:
        return score is not None and float(score) < quality_floor
    except (TypeError, ValueError):
        return False


def build_example_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """trace 1건을 평가 example 페이로드로 변환한다.

    `expected`는 정답 문자열이 아니라 "이 trace가 만족했어야 할 조건"이다.
    rule evaluator가 metadata에 보존된 trace payload를 그대로 채점한다.
    """
    status = trace.get("status") or "success"
    error_class = trace.get("error_class") or classify_error(trace.get("error"))
    tool_calls = trace.get("tool_calls") or []
    reason = "error" if status == "error" else "low_quality"
    rubric = {
        "require_final_response": True,
        "require_no_error": True,
        "require_cost_recorded": True,
        "require_tool_policy": bool(tool_calls),
        "max_latency_ms": 120_000,
    }
    expected = (
        f"status=success; no {error_class or 'error'}; final response persisted"
        if status == "error"
        else f"status=success with quality_score >= {QUALITY_FLOOR}"
    )
    return {
        "input": clip(trace.get("input_summary") or ""),
        "expected": clip(expected),
        "rubric": rubric,
        "tags": [
            str(tag) for tag in (reason, error_class, trace.get("project"), trace.get("run_type")) if tag
        ],
        "metadata": {
            "promotion_reason": reason,
            "source_status": status,
            "source_error_class": error_class,
            "graph_run_id": trace.get("graph_run_id"),
            "source_table": trace.get("source_table"),
            "trace_payload": {
                "status": status,
                "error": trace.get("error"),
                "error_class": error_class,
                "input_summary": trace.get("input_summary"),
                "output_summary": trace.get("output_summary"),
                "cost_usd": trace.get("cost_usd"),
                "latency_ms": trace.get("latency_ms"),
                "quality_score": trace.get("quality_score"),
                "graph_run_id": trace.get("graph_run_id"),
                "tool_calls": [
                    {
                        "tool_name": call.get("tool_name"),
                        "risk_tier": call.get("risk_tier"),
                        "approval_state": call.get("approval_state"),
                        "status": call.get("status"),
                    }
                    for call in tool_calls[:20]
                ],
            },
        },
    }


async def promote_trace_to_dataset(
    trace_id: str,
    *,
    dataset_slug: str = DEFAULT_FAILURE_DATASET,
    project: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """실패/저품질 trace 1건을 dataset example로 승격한다 (FR-002).

    같은 trace를 두 번 승격해도 example은 하나다 (dataset_id+source_ref unique).
    """
    trace = await get_trace(trace_id)
    if trace is None:
        return {"promoted": False, "reason": "trace_not_found", "trace_id": trace_id}
    if not force and not is_promotable(trace):
        return {
            "promoted": False,
            "reason": "trace_not_failed_or_low_quality",
            "trace_id": trace_id,
            "status": trace.get("status"),
            "quality_score": trace.get("quality_score"),
        }

    payload = build_example_from_trace(trace)
    source_ref = f"{trace.get('source_table')}:{trace_id}"

    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            dataset_id = await ensure_dataset(
                conn,
                slug=dataset_slug,
                project=project or trace.get("project"),
                title=f"{dataset_slug} promoted traces",
                purpose="Failed or low-quality traces promoted for offline evaluation.",
            )
            if not dataset_id:
                return {"promoted": False, "reason": "llmops_tables_missing", "trace_id": trace_id}

            # v2 trace만 FK로 연결한다. v1 legacy id는 source_ref로만 남긴다.
            source_trace_id = trace_id if trace.get("source_table") == TRACE_TABLE else None
            example_id = await conn.fetchval(
                f"""
                INSERT INTO {EXAMPLE_TABLE}
                    (dataset_id, source_trace_id, source_ref, input, expected, rubric, tags, metadata)
                VALUES ($1::uuid, $2::text, $3, $4, $5, $6::jsonb, $7::text[], $8::jsonb)
                ON CONFLICT (dataset_id, source_ref) WHERE source_ref IS NOT NULL
                DO UPDATE SET input = EXCLUDED.input,
                              expected = EXCLUDED.expected,
                              rubric = EXCLUDED.rubric,
                              tags = EXCLUDED.tags,
                              metadata = EXCLUDED.metadata
                RETURNING id
                """,
                dataset_id,
                source_trace_id,
                source_ref,
                payload["input"],
                payload["expected"],
                _as_json(payload["rubric"]),
                payload["tags"],
                _as_json(payload["metadata"]),
            )
            return {
                "promoted": bool(example_id),
                "trace_id": trace_id,
                "dataset_id": dataset_id,
                "dataset_slug": dataset_slug,
                "example_id": str(example_id) if example_id else None,
                "source_ref": source_ref,
                "reason": payload["metadata"]["promotion_reason"],
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops dataset promotion failed: %s", str(exc)[:200])
        return {"promoted": False, "reason": "error", "error": str(exc)[:200], "trace_id": trace_id}


async def find_promotion_candidates(
    *,
    project: Optional[str] = None,
    hours: int = 24,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """최근 실패/저품질 trace를 승격 후보로 돌려준다 (FR-004)."""
    listed = await list_traces(project=project, hours=hours, limit=MAX_LIMIT)
    candidates = [trace for trace in listed.get("traces", []) if is_promotable(trace)]
    return candidates[:limit]


# ── status counts ───────────────────────────────────────────────────────────
#
# 집계는 두 가지를 동시에 지켜야 한다.
#
# 1. **프로젝트 격리.** `?project=AADS`로 물으면 AADS가 소유한 행만 세야 한다.
#    project 컬럼이 없는 원장(examples/experiments/scores/feedback)은 소유
#    관계를 따라가야 하며, 소유를 증명할 수 없는 행은 세지 않는다. 그래야
#    다른 프로젝트(또는 project=NULL)의 행이 AADS 총계로 새지 않는다.
# 2. **부분 적용 내성.** 163/164가 절반만 적용된 DB에서 없는 테이블 하나가
#    나머지 집계 전부를 죽이면 안 된다. 각 지표는 자기가 필요한 relation이
#    전부 있을 때만 계산되고, 없으면 0이 아니라 **키 자체가 빠진다**
#    (대시보드는 키 부재를 "미제공"으로 표시한다 — 0과 구분되어야 한다).

# project 인자는 항상 $1. NULL이면 전역(기존 semantics 그대로).
_PROJECT_PREDICATE = "($1::text IS NULL OR {col} = $1)"


def _owns(col: str) -> str:
    return _PROJECT_PREDICATE.format(col=col)


def _dataset_owned_count(table: str, scoped: bool) -> str:
    """dataset을 통해 project를 소유하는 자식 테이블(examples/experiments) 집계."""
    if not scoped:
        # 전역 집계는 dataset 고아 행까지 포함한다 (기존 semantics 보존).
        return f"SELECT COUNT(*) FROM {table}"
    return (
        f"SELECT COUNT(*) FROM {table} child "
        f"JOIN {DATASET_TABLE} d ON d.id = child.dataset_id WHERE d.project = $1"
    )


def build_status_count_specs(
    tables: dict[str, bool], project: Optional[str]
) -> list[tuple[str, str]]:
    """(지표 키, 스칼라 서브쿼리 SQL) 목록 — 계산 가능한 지표만 포함한다.

    반환에 없는 키는 "그 지표는 이 DB에서 낼 수 없다"는 뜻이지 0이 아니다.
    """
    scoped = project is not None
    has_dataset = bool(tables.get(DATASET_TABLE))
    specs: list[tuple[str, str]] = []

    if tables.get(TRACE_TABLE):
        where = _owns("project")
        specs += [
            ("traces_total", f"SELECT COUNT(*) FROM {TRACE_TABLE} WHERE {where}"),
            (
                "traces_error",
                f"SELECT COUNT(*) FROM {TRACE_TABLE} WHERE {where} AND status = 'error'",
            ),
            (
                "traces_last_24h",
                (
                    f"SELECT COUNT(*) FROM {TRACE_TABLE} WHERE {where} "
                    "AND created_at >= NOW() - INTERVAL '24 hours'"
                ),
            ),
        ]

    if tables.get(LEGACY_TRACE_TABLE):
        specs.append(
            (
                "legacy_traces_total",
                f"SELECT COUNT(*) FROM {LEGACY_TRACE_TABLE} WHERE {_owns('project')}",
            )
        )

    if has_dataset:
        specs.append(("datasets", f"SELECT COUNT(*) FROM {DATASET_TABLE} WHERE {_owns('project')}"))

    for key, table in (("examples", EXAMPLE_TABLE), ("experiments", EXPERIMENT_TABLE)):
        # project 스코프 집계는 dataset 원장이 있어야 소유를 증명할 수 있다.
        if tables.get(table) and (has_dataset or not scoped):
            specs.append((key, _dataset_owned_count(table, scoped)))

    if tables.get(SCORE_TABLE):
        if not scoped:
            specs.append(("scores", f"SELECT COUNT(*) FROM {SCORE_TABLE}"))
        else:
            # score는 experiment / example / 원본 trace 중 어느 경로로든 소유가
            # 증명되면 이 프로젝트 것이다. 사용할 수 있는 경로만 조립한다.
            paths: list[str] = []
            if has_dataset and tables.get(EXPERIMENT_TABLE):
                paths.append(
                    f"EXISTS (SELECT 1 FROM {EXPERIMENT_TABLE} x "
                    f"JOIN {DATASET_TABLE} d ON d.id = x.dataset_id "
                    "WHERE x.id = s.experiment_id AND d.project = $1)"
                )
            if has_dataset and tables.get(EXAMPLE_TABLE):
                paths.append(
                    f"EXISTS (SELECT 1 FROM {EXAMPLE_TABLE} e "
                    f"JOIN {DATASET_TABLE} d ON d.id = e.dataset_id "
                    "WHERE e.id = s.example_id AND d.project = $1)"
                )
            if tables.get(TRACE_TABLE):
                paths.append(
                    f"EXISTS (SELECT 1 FROM {TRACE_TABLE} t "
                    "WHERE t.id::text = s.source_trace_id AND t.project = $1)"
                )
            if paths:
                specs.append(
                    ("scores", f"SELECT COUNT(*) FROM {SCORE_TABLE} s WHERE " + " OR ".join(paths))
                )

    if tables.get(FEEDBACK_TABLE):
        if not scoped:
            specs.append(("feedback", f"SELECT COUNT(*) FROM {FEEDBACK_TABLE}"))
        elif tables.get(TRACE_TABLE):
            # trace에 매달리지 않은 feedback(source_ref만 있는 행)은 소유를
            # 증명할 수 없으므로 프로젝트 총계에서 뺀다.
            specs.append(
                (
                    "feedback",
                    (
                        f"SELECT COUNT(*) FROM {FEEDBACK_TABLE} f WHERE EXISTS ("
                        f"SELECT 1 FROM {TRACE_TABLE} t "
                        "WHERE t.id::text = f.trace_id AND t.project = $1)"
                    ),
                )
            )

    return specs


def _project_args(sql: str, project: Optional[str]) -> tuple[Any, ...]:
    """`$1`을 실제로 쓰는 쿼리에만 project 인자를 넘긴다.

    전역 집계 SQL 일부는 파라미터가 아예 없다 (`SELECT COUNT(*) FROM llmops_scores`).
    asyncpg는 인자 개수가 맞지 않으면 InterfaceError("the server expects 0
    arguments")를 던지므로, 그냥 project를 붙이면 지표별 재시도 경로에서
    scores/feedback/examples/experiments가 통째로 사라진다. 합본 쿼리도 $1을 쓰는
    지표가 하나도 없으면(부분 적용 DB) 같은 이유로 깨진다.
    """
    return (project,) if "$1" in sql else ()


async def _collect_status_counts(
    conn: Any, specs: list[tuple[str, str]], project: Optional[str]
) -> dict[str, int]:
    """지표들을 한 번의 왕복으로 센다. 실패하면 지표별로 다시 시도한다.

    상태 엔드포인트는 대시보드가 주기적으로 호출하므로 정상 경로는 1왕복이다.
    다만 한 지표의 실패가 나머지를 전부 못 쓰게 만들면 안 되므로, 합본 쿼리가
    깨지면 지표별로 나눠 세고 실패한 것만 결과에서 뺀다.
    """
    if not specs:
        return {}
    combined = "SELECT " + ", ".join(f"({sql}) AS {key}" for key, sql in specs)
    try:
        row = await conn.fetchrow(combined, *_project_args(combined, project))
        if row is not None:
            return {key: int(row[key] or 0) for key, _ in specs}
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops status combined count failed, degrading: %s", str(exc)[:200])

    counts: dict[str, int] = {}
    for key, sql in specs:
        try:
            counts[key] = int(await conn.fetchval(sql, *_project_args(sql, project)) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llmops status count %s unavailable: %s", key, str(exc)[:200])
    return counts


async def get_status(project: Optional[str] = None) -> dict[str, Any]:
    """LLMOps foundation 상태 — 테이블 적용 여부와 최근 집계.

    `project`를 주면 모든 집계가 그 프로젝트 소유 행으로 좁혀진다.
    `None`이면 기존과 같은 전역 집계다.
    """
    from app.services.llmops_export import export_status

    status: dict[str, Any] = {
        "project": project,
        "migrations": [
            "163_ohvis_internal_llmops_foundation.sql",
            "164_llmops_ledger_schema_reconcile.sql",
        ],
        "external_export": export_status(),
        "db": {"available": False},
    }
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            tables = await relations_exist(conn, (*LLMOPS_TABLES, LEGACY_TRACE_TABLE))
            db: dict[str, Any] = {
                "available": True,
                "tables": tables,
                # 원장 8개가 전부 있어야 foundation_ready다. traces 하나만 보면
                # 부분 적용된 DB가 정상으로 보인다 (ohvis_harness와 같은 판정).
                "foundation_ready": all(tables.get(table) for table in LLMOPS_TABLES),
                "project_scoped": project is not None,
            }
            counts = await _collect_status_counts(
                conn, build_status_count_specs(tables, project), project
            )
            traces = {
                field: counts[f"traces_{field}"]
                for field in ("total", "error", "last_24h")
                if f"traces_{field}" in counts
            }
            if traces:
                db["traces"] = traces
            if "legacy_traces_total" in counts:
                db["legacy_traces"] = {"total": counts["legacy_traces_total"]}
            for key in ("datasets", "examples", "experiments", "scores", "feedback"):
                if key in counts:
                    db[key] = counts[key]
            status["db"] = db
    except Exception as exc:  # noqa: BLE001
        status["db"] = {"available": False, "error": str(exc)[:200]}
    return status
