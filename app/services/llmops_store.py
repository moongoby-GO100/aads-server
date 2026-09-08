"""OHVIS internal LangSmith-compatible LLMOps store (trace read/write).

Backed by `migrations/163_ohvis_internal_llmops_v1.sql`.

Design contract (mirrors `ohvis_harness_trace`):
- **Writes never raise.** A trace failure must not break chat/runner/deploy.
- **Reads degrade.** If migration 163 has not been applied the read path falls
  back to migration 158's `ohvis_harness_traces`, so `/ohvis/llmops/traces`
  keeps answering with legacy rows instead of 500-ing.
- Summaries are clipped; this ledger is evidence, not a body store.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

TRACE_TABLE = "llmops_traces"
SPAN_TABLE = "llmops_spans"
TOOL_CALL_TABLE = "llmops_tool_calls"
UNIFIED_VIEW = "llmops_trace_unified"
LEGACY_TRACE_TABLE = "ohvis_harness_traces"

LLMOPS_TABLES = (
    "llmops_traces",
    "llmops_spans",
    "llmops_tool_calls",
    "llmops_datasets",
    "llmops_examples",
    "llmops_experiments",
    "llmops_scores",
    "llmops_feedback",
)

SUMMARY_LIMIT = 2000
ERROR_LIMIT = 2000
MAX_LIMIT = 200

# Deterministic namespace so every row of one graph run resolves to the same
# trace id without a coordination round-trip.
_TRACE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://aads.newtalk.kr/ohvis/llmops")

_relation_cache: dict[str, bool] = {}


def reset_relation_cache() -> None:
    """Clear the relation-presence cache (tests / right after a migration)."""
    _relation_cache.clear()


def trace_id_for_graph_run(graph_run_id: str) -> str:
    """Stable trace id for a graph run id.

    Deterministic so that independent writers on the same run (chat turn,
    pipeline runner, deploy step) land on one trace without sharing state.
    """
    return str(uuid.uuid5(_TRACE_NAMESPACE, f"graph_run:{graph_run_id}"))


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def clip(value: Any, limit: int = SUMMARY_LIMIT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _as_uuid_text(value: Any) -> Optional[str]:
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


async def relation_exists(conn: Any, relation: str) -> bool:
    """True when a table *or* view named `relation` exists in public schema."""
    cached = _relation_cache.get(relation)
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
    _relation_cache[relation] = present
    return present


class _PoolConn:
    """`async with` helper that reuses a caller connection when given one.

    Nested `pool.acquire()` inside a handler that already holds a connection is
    how this pool gets exhausted, so callers pass theirs down.
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


# ── write path ────────────────────────────────────────────────────────────

_TRACE_UPSERT_SQL = f"""
INSERT INTO {TRACE_TABLE} (
    trace_id, graph_run_id, project, session_id, ohvis_task_id, task_ref,
    source, run_type, name, status, input_summary, output_summary, error,
    cost_usd, latency_ms, quality_score, metadata, ended_at
)
VALUES (
    $1, $2, $3, $4::uuid, $5::uuid, $6,
    $7, $8, $9, $10, $11, $12, $13,
    $14, $15, $16, $17::jsonb, $18
)
ON CONFLICT (trace_id) DO UPDATE SET
    graph_run_id = COALESCE(EXCLUDED.graph_run_id, {TRACE_TABLE}.graph_run_id),
    project = COALESCE(EXCLUDED.project, {TRACE_TABLE}.project),
    session_id = COALESCE(EXCLUDED.session_id, {TRACE_TABLE}.session_id),
    ohvis_task_id = COALESCE(EXCLUDED.ohvis_task_id, {TRACE_TABLE}.ohvis_task_id),
    task_ref = COALESCE(EXCLUDED.task_ref, {TRACE_TABLE}.task_ref),
    status = EXCLUDED.status,
    name = COALESCE(NULLIF(EXCLUDED.name, ''), {TRACE_TABLE}.name),
    input_summary = COALESCE(NULLIF(EXCLUDED.input_summary, ''), {TRACE_TABLE}.input_summary),
    output_summary = COALESCE(NULLIF(EXCLUDED.output_summary, ''), {TRACE_TABLE}.output_summary),
    error = COALESCE(EXCLUDED.error, {TRACE_TABLE}.error),
    cost_usd = COALESCE(EXCLUDED.cost_usd, {TRACE_TABLE}.cost_usd),
    latency_ms = COALESCE(EXCLUDED.latency_ms, {TRACE_TABLE}.latency_ms),
    quality_score = COALESCE(EXCLUDED.quality_score, {TRACE_TABLE}.quality_score),
    metadata = {TRACE_TABLE}.metadata || EXCLUDED.metadata,
    ended_at = COALESCE(EXCLUDED.ended_at, {TRACE_TABLE}.ended_at),
    updated_at = NOW()
RETURNING trace_id
"""


async def upsert_trace(
    *,
    graph_run_id: str,
    trace_id: Optional[str] = None,
    project: Optional[str] = None,
    session_id: Optional[str] = None,
    ohvis_task_id: Optional[str] = None,
    task_ref: Optional[str] = None,
    source: str = "internal",
    run_type: str = "chain",
    name: str = "",
    status: str = "running",
    input_summary: Any = "",
    output_summary: Any = "",
    error: Optional[str] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    quality_score: Optional[float] = None,
    metadata: Optional[dict[str, Any]] = None,
    ended_at: Any = None,
    conn: Any = None,
) -> Optional[str]:
    """Create or update one trace. Returns the trace id, or None on skip/failure."""
    if not graph_run_id and not trace_id:
        return None
    resolved = trace_id or trace_id_for_graph_run(graph_run_id)
    try:
        async with _PoolConn(conn) as target:
            if not await relation_exists(target, TRACE_TABLE):
                return None
            return await target.fetchval(
                _TRACE_UPSERT_SQL,
                resolved,
                (str(graph_run_id)[:200] if graph_run_id else None),
                project or None,
                _as_uuid_text(session_id),
                _as_uuid_text(ohvis_task_id),
                task_ref,
                source or "internal",
                run_type or "chain",
                clip(name, 200),
                status or "running",
                clip(input_summary),
                clip(output_summary),
                clip(error, ERROR_LIMIT) if error else None,
                cost_usd,
                int(latency_ms) if latency_ms is not None else None,
                float(quality_score) if quality_score is not None else None,
                _as_json(metadata),
                ended_at,
            )
    except Exception as exc:  # noqa: BLE001 — LLMOps must never break the caller
        logger.warning("llmops trace upsert failed (non-fatal): %s", str(exc)[:200])
        return None


async def record_span(
    *,
    trace_id: str,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    span_type: str = "chain",
    name: str = "",
    model_id: Optional[str] = None,
    status: str = "success",
    input_summary: Any = "",
    output_summary: Any = "",
    error: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> Optional[str]:
    """Record one span under a trace. Returns the span id, or None on skip/failure."""
    if not trace_id:
        return None
    resolved = span_id or new_span_id()
    try:
        async with _PoolConn(conn) as target:
            if not await relation_exists(target, SPAN_TABLE):
                return None
            await target.execute(
                f"""
                INSERT INTO {SPAN_TABLE} (
                    trace_id, span_id, parent_span_id, span_type, name, model_id,
                    status, input_summary, output_summary, error,
                    prompt_tokens, completion_tokens, cost_usd, latency_ms, metadata
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)
                ON CONFLICT (trace_id, span_id) DO NOTHING
                """,
                trace_id,
                resolved,
                parent_span_id,
                span_type or "chain",
                clip(name, 200),
                model_id,
                status or "success",
                clip(input_summary),
                clip(output_summary),
                clip(error, ERROR_LIMIT) if error else None,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                int(latency_ms) if latency_ms is not None else None,
                _as_json(metadata),
            )
            return resolved
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops span insert failed (non-fatal): %s", str(exc)[:200])
        return None


async def record_tool_call(
    *,
    trace_id: str,
    tool_name: str,
    span_id: Optional[str] = None,
    risk_tier: str = "read",
    approval_state: str = "not_required",
    status: str = "success",
    input_summary: Any = "",
    output_summary: Any = "",
    error: Optional[str] = None,
    latency_ms: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> bool:
    if not trace_id or not tool_name:
        return False
    try:
        async with _PoolConn(conn) as target:
            if not await relation_exists(target, TOOL_CALL_TABLE):
                return False
            await target.execute(
                f"""
                INSERT INTO {TOOL_CALL_TABLE} (
                    trace_id, span_id, tool_name, risk_tier, approval_state,
                    status, input_summary, output_summary, error, latency_ms, metadata
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
                """,
                trace_id,
                span_id,
                clip(tool_name, 120),
                risk_tier or "read",
                approval_state or "not_required",
                status or "success",
                clip(input_summary),
                clip(output_summary),
                clip(error, ERROR_LIMIT) if error else None,
                int(latency_ms) if latency_ms is not None else None,
                _as_json(metadata),
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("llmops tool call insert failed (non-fatal): %s", str(exc)[:200])
        return False


# ── read path ─────────────────────────────────────────────────────────────

_UNIFIED_COLUMNS = (
    "trace_id, graph_run_id, project, session_id, ohvis_task_id, run_type, name, "
    "status, input_summary, output_summary, error, cost_usd, latency_ms, "
    "quality_score, metadata, created_at, ledger"
)

# Legacy-only shape: same column contract, derived from ohvis_harness_traces.
_LEGACY_SELECT = f"""
SELECT COALESCE(NULLIF(h.trace_id, ''), 'harness:' || h.id::TEXT) AS trace_id,
       h.graph_run_id, h.project, h.session_id, h.ohvis_task_id, h.run_type,
       COALESCE(NULLIF(h.run_type, ''), 'chain') AS name,
       CASE WHEN h.error IS NULL OR h.error = '' THEN 'success' ELSE 'error' END AS status,
       h.input_summary, h.output_summary, h.error, h.cost_usd, h.latency_ms,
       NULL::DOUBLE PRECISION AS quality_score, h.metadata, h.created_at,
       'harness'::TEXT AS ledger
FROM {LEGACY_TRACE_TABLE} h
"""


async def _resolve_read_source(conn: Any) -> Optional[str]:
    """Pick the widest available read relation: unified view → legacy table."""
    if await relation_exists(conn, UNIFIED_VIEW):
        return "unified"
    if await relation_exists(conn, LEGACY_TRACE_TABLE):
        return "legacy"
    return None


def _row_to_trace(row: Any) -> dict[str, Any]:
    data = dict(row)
    metadata = data.get("metadata")
    if isinstance(metadata, str):
        try:
            data["metadata"] = json.loads(metadata)
        except (TypeError, ValueError):
            data["metadata"] = {}
    for key in ("session_id", "ohvis_task_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    if data.get("cost_usd") is not None:
        data["cost_usd"] = float(data["cost_usd"])
    created = data.get("created_at")
    if created is not None and not isinstance(created, str):
        data["created_at"] = created.isoformat()
    return data


async def list_traces(
    *,
    project: Optional[str] = None,
    status: Optional[str] = None,
    graph_run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    since_hours: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    conn: Any = None,
) -> dict[str, Any]:
    """Search traces across the internal ledger and the legacy harness ledger."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))

    async with _PoolConn(conn) as target:
        source = await _resolve_read_source(target)
        if source is None:
            return {
                "traces": [],
                "count": 0,
                "source": "unavailable",
                "degraded": True,
                "degraded_reason": "llmops_migration_163_not_applied_and_no_legacy_table",
            }

        base = (
            f"SELECT {_UNIFIED_COLUMNS} FROM {UNIFIED_VIEW}"
            if source == "unified"
            else _LEGACY_SELECT
        )
        clauses: list[str] = []
        args: list[Any] = []

        def _add(clause_tmpl: str, value: Any) -> None:
            args.append(value)
            clauses.append(clause_tmpl.format(n=len(args)))

        if project:
            _add("project = ${n}", project)
        if graph_run_id:
            _add("graph_run_id = ${n}", graph_run_id)
        if session_id and _as_uuid_text(session_id):
            _add("session_id = ${n}::uuid", _as_uuid_text(session_id))
        if since_hours:
            _add("created_at >= NOW() - (${n}::int * INTERVAL '1 hour')", int(since_hours))

        query = f"SELECT * FROM ({base}) AS t"
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        args.extend([limit, offset])
        query += f" ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"

        rows = await target.fetch(query, *args)
        traces = [_row_to_trace(row) for row in rows]
        return {
            "traces": traces,
            "count": len(traces),
            "source": source,
            "degraded": source == "legacy",
            "degraded_reason": "llmops_migration_163_not_applied" if source == "legacy" else None,
        }


async def get_trace(trace_id: str, *, conn: Any = None) -> Optional[dict[str, Any]]:
    """Trace detail with its spans and tool calls (empty lists for legacy rows)."""
    if not trace_id:
        return None

    async with _PoolConn(conn) as target:
        source = await _resolve_read_source(target)
        if source is None:
            return None
        base = (
            f"SELECT {_UNIFIED_COLUMNS} FROM {UNIFIED_VIEW}"
            if source == "unified"
            else _LEGACY_SELECT
        )
        row = await target.fetchrow(
            f"SELECT * FROM ({base}) AS t WHERE trace_id = $1 ORDER BY created_at DESC LIMIT 1",
            trace_id,
        )
        if row is None:
            return None

        detail = _row_to_trace(row)
        detail["spans"] = []
        detail["tool_calls"] = []

        if await relation_exists(target, SPAN_TABLE):
            span_rows = await target.fetch(
                f"""
                SELECT span_id, parent_span_id, span_type, name, model_id, status,
                       input_summary, output_summary, error, prompt_tokens,
                       completion_tokens, cost_usd, latency_ms, metadata,
                       started_at, ended_at
                FROM {SPAN_TABLE} WHERE trace_id = $1 ORDER BY started_at ASC LIMIT 500
                """,
                trace_id,
            )
            detail["spans"] = [_row_to_trace(r) for r in span_rows]

        if await relation_exists(target, TOOL_CALL_TABLE):
            tool_rows = await target.fetch(
                f"""
                SELECT tool_name, span_id, risk_tier, approval_state, status,
                       input_summary, output_summary, error, latency_ms, metadata, created_at
                FROM {TOOL_CALL_TABLE} WHERE trace_id = $1 ORDER BY created_at ASC LIMIT 500
                """,
                trace_id,
            )
            detail["tool_calls"] = [_row_to_trace(r) for r in tool_rows]

        # Legacy harness rows carry their tool calls inline in a JSONB column.
        if not detail["tool_calls"] and detail.get("ledger") == "harness":
            legacy = await target.fetchval(
                f"SELECT tool_calls FROM {LEGACY_TRACE_TABLE} "
                f"WHERE COALESCE(NULLIF(trace_id, ''), 'harness:' || id::TEXT) = $1 "
                "ORDER BY created_at DESC LIMIT 1",
                trace_id,
            )
            if isinstance(legacy, str):
                try:
                    legacy = json.loads(legacy)
                except (TypeError, ValueError):
                    legacy = []
            if isinstance(legacy, list):
                detail["tool_calls"] = legacy

        return detail


async def list_eval_candidates(
    *,
    project: Optional[str] = None,
    quality_threshold: float = 0.4,
    since_hours: int = 168,
    limit: int = 20,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Failed or low-quality traces worth promoting into an eval dataset (FR-004)."""
    result = await list_traces(
        project=project,
        since_hours=since_hours,
        limit=MAX_LIMIT,
        conn=conn,
    )
    candidates = [
        trace
        for trace in result["traces"]
        if trace.get("status") == "error"
        or (trace.get("error") or "")
        or (
            trace.get("quality_score") is not None
            and float(trace["quality_score"]) < quality_threshold
        )
    ]
    return candidates[: max(1, int(limit))]


async def get_status(*, project: Optional[str] = None, conn: Any = None) -> dict[str, Any]:
    """LLMOps foundation readiness: table presence, counts, export gate."""
    from app.services.llmops_export import export_gate_status

    tables: dict[str, Any] = {}
    counts: dict[str, Any] = {}
    available = False
    error: Optional[str] = None
    unified = False

    try:
        async with _PoolConn(conn) as target:
            available = True
            for table in LLMOPS_TABLES:
                present = await relation_exists(target, table)
                tables[table] = present
                if present:
                    counts[table] = await target.fetchval(f"SELECT COUNT(*)::int FROM {table}")
            unified = await relation_exists(target, UNIFIED_VIEW)
            if await relation_exists(target, LEGACY_TRACE_TABLE):
                counts[LEGACY_TRACE_TABLE] = await target.fetchval(
                    f"SELECT COUNT(*)::int FROM {LEGACY_TRACE_TABLE}"
                )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:200]

    migration_applied = all(tables.get(table) for table in LLMOPS_TABLES)
    return {
        "project": project,
        "migration": {
            "file": "migrations/163_ohvis_internal_llmops_v1.sql",
            "applied": migration_applied,
            "tables": tables,
            "unified_view": unified,
        },
        "counts": counts,
        "db_available": available,
        "error": error,
        "backfill": {
            "legacy_rows_copied": False,
            "legacy_readable_via": UNIFIED_VIEW if unified else LEGACY_TRACE_TABLE,
            "note": (
                "ohvis_harness_traces is not copied; legacy rows are read through the "
                "unified view. Only new writes land in llmops_traces."
            ),
        },
        "external_export": export_gate_status(),
    }
