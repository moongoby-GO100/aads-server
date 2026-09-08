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

import json
import logging
import re
import uuid
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

# 테이블/뷰 존재 여부 캐시 (relation 이름 → bool)
_relation_present: dict[str, bool] = {}


def reset_relation_cache() -> None:
    """relation 존재 캐시 초기화 (테스트/마이그레이션 직후용)."""
    _relation_present.clear()


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


async def relation_exists(conn: Any, relation: str) -> bool:
    """public 스키마에 해당 테이블이 있는지 (캐시됨)."""
    cached = _relation_present.get(relation)
    if cached is not None:
        return cached
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
    _relation_present[relation] = present
    if not present:
        logger.warning(
            "%s is missing; LLMOps writes are skipped until migration 163 is applied", relation
        )
    return present


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


async def get_status(project: Optional[str] = None) -> dict[str, Any]:
    """LLMOps foundation 상태 — 테이블 적용 여부와 최근 집계."""
    from app.services.llmops_export import export_status

    status: dict[str, Any] = {
        "project": project,
        "migration": "163_ohvis_internal_llmops_foundation.sql",
        "external_export": export_status(),
        "db": {"available": False},
    }
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            tables = {table: await relation_exists(conn, table) for table in LLMOPS_TABLES}
            tables[LEGACY_TRACE_TABLE] = await relation_exists(conn, LEGACY_TRACE_TABLE)
            db: dict[str, Any] = {"available": True, "tables": tables}
            if tables.get(TRACE_TABLE):
                db["traces"] = {
                    "total": await conn.fetchval(f"SELECT COUNT(*) FROM {TRACE_TABLE}"),
                    "error": await conn.fetchval(
                        f"SELECT COUNT(*) FROM {TRACE_TABLE} WHERE status = 'error'"
                    ),
                    "last_24h": await conn.fetchval(
                        f"SELECT COUNT(*) FROM {TRACE_TABLE} "
                        "WHERE created_at >= NOW() - INTERVAL '24 hours'"
                    ),
                }
            if tables.get(LEGACY_TRACE_TABLE):
                db["legacy_traces"] = {
                    "total": await conn.fetchval(f"SELECT COUNT(*) FROM {LEGACY_TRACE_TABLE}")
                }
            if tables.get(DATASET_TABLE):
                db["datasets"] = await conn.fetchval(f"SELECT COUNT(*) FROM {DATASET_TABLE}")
                db["examples"] = await conn.fetchval(f"SELECT COUNT(*) FROM {EXAMPLE_TABLE}")
                db["experiments"] = await conn.fetchval(f"SELECT COUNT(*) FROM {EXPERIMENT_TABLE}")
            status["db"] = db
    except Exception as exc:  # noqa: BLE001
        status["db"] = {"available": False, "error": str(exc)[:200]}
    return status
