"""OHVIS internal LangSmith-compatible LLMOps unit tests.

Covers: migration text safety/idempotence, store read fallback to the legacy
harness ledger, rule evaluator scoring, dataset promotion, export gating, and
API route import.
"""
import asyncio
import re
from pathlib import Path

import pytest

from app.services import llmops_export, llmops_store
from app.services.llmops_eval import (
    DatasetPromotionError,
    classify_error,
    evaluate_trace,
    promote_trace_to_dataset,
    slugify,
)
from app.services.llmops_store import (
    get_trace,
    list_eval_candidates,
    list_traces,
    reset_relation_cache,
    trace_id_for_graph_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "migrations/163_ohvis_internal_llmops_v1.sql"

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


@pytest.fixture(autouse=True)
def _clear_relation_cache():
    reset_relation_cache()
    yield
    reset_relation_cache()


# ── migration text ────────────────────────────────────────────────────────


def test_migration_defines_every_llmops_table_idempotently():
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in LLMOPS_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"{table} missing or not idempotent"


def test_migration_creates_no_destructive_statement():
    sql = MIGRATION.read_text(encoding="utf-8")
    # Strip comments: the header explains what is *not* done and must not trip this.
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for forbidden in ("DROP TABLE", "DROP VIEW", "DROP INDEX", "TRUNCATE", "DELETE FROM", "ALTER TABLE"):
        assert forbidden not in body.upper(), f"destructive statement found: {forbidden}"


def test_migration_indexes_are_idempotent():
    sql = MIGRATION.read_text(encoding="utf-8")
    creates = re.findall(r"CREATE(?:\s+UNIQUE)?\s+INDEX(?:\s+IF NOT EXISTS)?", sql, re.IGNORECASE)
    assert creates, "expected index definitions"
    assert all("IF NOT EXISTS" in stmt.upper() for stmt in creates)


def test_migration_keeps_legacy_harness_ledger_readable():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW llmops_trace_unified" in sql
    assert "FROM ohvis_harness_traces h" in sql


# ── store: read fallback ──────────────────────────────────────────────────


class FakeConn:
    def __init__(self, relations, rows=None, row=None):
        self.relations = set(relations)
        self.rows = rows or []
        self.row = row
        self.queries = []

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return args[0] in self.relations
        self.queries.append((query, args))
        return None

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        return list(self.rows)

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self.row


def test_list_traces_uses_unified_view_when_migration_applied():
    conn = FakeConn({"llmops_trace_unified", "llmops_traces"}, rows=[])
    result = asyncio.run(list_traces(project="AADS", limit=10, conn=conn))

    assert result["source"] == "unified"
    assert result["degraded"] is False
    assert "FROM llmops_trace_unified" in conn.queries[0][0]


def test_list_traces_falls_back_to_legacy_harness_table():
    conn = FakeConn({"ohvis_harness_traces"}, rows=[])
    result = asyncio.run(list_traces(project="AADS", limit=10, conn=conn))

    assert result["source"] == "legacy"
    assert result["degraded"] is True
    assert result["degraded_reason"] == "llmops_migration_163_not_applied"
    assert "ohvis_harness_traces" in conn.queries[0][0]


def test_list_traces_reports_unavailable_without_any_ledger():
    conn = FakeConn(set())
    result = asyncio.run(list_traces(conn=conn))

    assert result["traces"] == []
    assert result["source"] == "unavailable"
    assert result["degraded"] is True


def test_list_traces_filters_are_bound_as_parameters():
    conn = FakeConn({"llmops_trace_unified"}, rows=[])
    asyncio.run(
        list_traces(
            project="AADS",
            status="error",
            graph_run_id="goal:abc",
            since_hours=24,
            limit=5,
            conn=conn,
        )
    )
    query, args = conn.queries[0]
    assert "project = $1" in query
    assert "graph_run_id = $2" in query
    assert "$5" in query and "$6" in query  # limit / offset
    assert args[:4] == ("AADS", "goal:abc", 24, "error")


def test_get_trace_returns_none_when_missing():
    conn = FakeConn({"llmops_trace_unified"}, row=None)
    assert asyncio.run(get_trace("nope", conn=conn)) is None


def test_get_trace_returns_detail_with_span_and_tool_lists():
    conn = FakeConn(
        {"llmops_trace_unified"},
        row={
            "trace_id": "t-1",
            "graph_run_id": "goal:abc",
            "project": "AADS",
            "session_id": None,
            "ohvis_task_id": None,
            "status": "error",
            "error": "connection refused",
            "metadata": '{"component": "runner"}',
            "created_at": "2026-09-08T00:00:00+09:00",
            "ledger": "llmops",
        },
    )
    detail = asyncio.run(get_trace("t-1", conn=conn))

    assert detail["trace_id"] == "t-1"
    assert detail["metadata"] == {"component": "runner"}
    assert detail["spans"] == []
    assert detail["tool_calls"] == []


def test_trace_id_for_graph_run_is_deterministic_and_run_scoped():
    first = trace_id_for_graph_run("goal:abc")
    assert first == trace_id_for_graph_run("goal:abc")
    assert first != trace_id_for_graph_run("goal:def")


def test_upsert_trace_never_raises_when_pool_is_missing(monkeypatch):
    import app.core.db_pool as db_pool

    def _boom():
        raise RuntimeError("DB pool이 초기화되지 않았습니다")

    monkeypatch.setattr(db_pool, "get_pool", _boom)
    assert asyncio.run(llmops_store.upsert_trace(graph_run_id="goal:abc")) is None


def test_eval_candidates_select_failed_and_low_quality_traces():
    conn = FakeConn(
        {"llmops_trace_unified"},
        rows=[
            {"trace_id": "ok", "status": "success", "error": None, "quality_score": 0.9,
             "metadata": "{}", "created_at": "2026-09-08T00:00:00+09:00"},
            {"trace_id": "failed", "status": "error", "error": "timeout", "quality_score": None,
             "metadata": "{}", "created_at": "2026-09-08T00:00:00+09:00"},
            {"trace_id": "weak", "status": "success", "error": None, "quality_score": 0.2,
             "metadata": "{}", "created_at": "2026-09-08T00:00:00+09:00"},
        ],
    )
    candidates = asyncio.run(list_eval_candidates(project="AADS", conn=conn))

    assert [item["trace_id"] for item in candidates] == ["failed", "weak"]


# ── rule evaluator ────────────────────────────────────────────────────────


def test_rule_evaluator_scores_five_criteria():
    result = evaluate_trace({"trace_id": "t-1", "graph_run_id": "goal:abc", "status": "success"})
    keys = [check["key"] for check in result["checks"]]

    assert keys == [
        "source_presence",
        "tool_policy",
        "final_response_persistence",
        "cost_present",
        "error_classification",
    ]
    assert result["evaluator"] == "rule_v1"
    assert 0.0 <= result["overall_score"] <= 1.0


def test_rule_evaluator_passes_a_complete_healthy_trace():
    result = evaluate_trace(
        {
            "trace_id": "t-1",
            "graph_run_id": "goal:abc",
            "project": "AADS",
            "session_id": "0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10",
            "status": "success",
            "output_summary": "deployed",
            "cost_usd": 0.02,
            "latency_ms": 1200,
            "error": None,
            "tool_calls": [{"tool_name": "deploy.sh", "risk_tier": "deploy", "approval_state": "approved"}],
        }
    )

    assert result["passed"] is True
    assert result["overall_score"] == 1.0


def test_rule_evaluator_flags_unapproved_risky_tool_call():
    result = evaluate_trace(
        {
            "graph_run_id": "goal:abc",
            "status": "success",
            "output_summary": "done",
            "cost_usd": 0.01,
            "tool_calls": [{"tool_name": "write_remote_file", "risk_tier": "write", "approval_state": "pending"}],
        }
    )
    tool_policy = next(c for c in result["checks"] if c["key"] == "tool_policy")

    assert tool_policy["passed"] is False
    assert result["passed"] is False
    assert "write_remote_file" in tool_policy["comment"]


def test_rule_evaluator_rejects_destructive_tier_outright():
    result = evaluate_trace(
        {
            "graph_run_id": "goal:abc",
            "status": "success",
            "output_summary": "done",
            "tool_calls": [{"tool_name": "drop_table", "risk_tier": "destructive", "approval_state": "approved"}],
        }
    )
    tool_policy = next(c for c in result["checks"] if c["key"] == "tool_policy")

    assert tool_policy["passed"] is False
    assert "never allowed" in tool_policy["comment"]


def test_rule_evaluator_flags_terminal_trace_without_output():
    result = evaluate_trace({"graph_run_id": "goal:abc", "status": "success", "output_summary": ""})
    persistence = next(c for c in result["checks"] if c["key"] == "final_response_persistence")

    assert persistence["passed"] is False
    assert persistence["score"] == 0.0


def test_rule_evaluator_flags_error_status_without_error_text():
    result = evaluate_trace({"graph_run_id": "goal:abc", "status": "error", "output_summary": "x"})
    classification = next(c for c in result["checks"] if c["key"] == "error_classification")

    assert classification["passed"] is False


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Read timed out after 30s", "timeout"),
        ("401 Unauthorized", "auth"),
        ("429 Too Many Requests", "rate_limit"),
        ("asyncpg duplicate key value", "db"),
        ("connection refused", "network"),
        (None, "none"),
        ("완전히 새로운 종류의 문제", "unclassified"),
    ],
)
def test_classify_error_labels_known_failure_shapes(message, expected):
    assert classify_error(message) == expected


def test_rule_evaluator_tolerates_json_encoded_tool_calls():
    result = evaluate_trace(
        {
            "graph_run_id": "goal:abc",
            "status": "success",
            "output_summary": "ok",
            "tool_calls": '[{"tool_name": "read_file", "risk_tier": "read"}]',
        }
    )
    tool_policy = next(c for c in result["checks"] if c["key"] == "tool_policy")

    assert tool_policy["evidence"]["tool_call_count"] == 1
    assert tool_policy["passed"] is True


# ── dataset promotion ─────────────────────────────────────────────────────


class PromotionConn(FakeConn):
    def __init__(self, relations, trace_row):
        super().__init__(relations, rows=[], row=trace_row)
        self.dataset_id = "11111111-2222-3333-4444-555555555555"

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return args[0] in self.relations
        self.queries.append((query, args))
        if "INSERT INTO llmops_datasets" in query:
            return self.dataset_id
        return None

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "INSERT INTO llmops_examples" in query:
            return {"id": "99999999-2222-3333-4444-555555555555", "inserted": True}
        return self.row


_FAILED_TRACE = {
    "trace_id": "harness:0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10",
    "graph_run_id": "runner:AADS-1",
    "project": "AADS",
    "session_id": None,
    "ohvis_task_id": None,
    "status": "error",
    "input_summary": "deploy AADS api",
    "output_summary": "",
    "error": "connection refused",
    "metadata": "{}",
    "created_at": "2026-09-08T00:00:00+09:00",
    "ledger": "harness",
}


def test_promote_failed_trace_creates_dataset_example():
    conn = PromotionConn(
        {"llmops_datasets", "llmops_examples", "llmops_trace_unified"},
        trace_row=_FAILED_TRACE,
    )
    result = asyncio.run(
        promote_trace_to_dataset(trace_id=_FAILED_TRACE["trace_id"], project="AADS", conn=conn)
    )

    assert result["created"] is True
    assert result["dataset_slug"] == "aads-failure-regression"
    assert result["evaluation"]["evaluator"] == "rule_v1"
    assert result["trace_id"] == _FAILED_TRACE["trace_id"]
    # The promoted example records the classified failure for later regression.
    insert = next(q for q, _ in conn.queries if "INSERT INTO llmops_examples" in q)
    assert "ON CONFLICT (dataset_id, source_trace_id)" in insert


def test_promote_requires_migration_applied():
    conn = PromotionConn(set(), trace_row=_FAILED_TRACE)
    with pytest.raises(DatasetPromotionError, match="163"):
        asyncio.run(promote_trace_to_dataset(trace_id="t-1", conn=conn))


def test_promote_rejects_unknown_trace():
    conn = PromotionConn({"llmops_datasets", "llmops_examples", "llmops_trace_unified"}, trace_row=None)
    with pytest.raises(DatasetPromotionError, match="trace not found"):
        asyncio.run(promote_trace_to_dataset(trace_id="missing", conn=conn))


def test_slugify_normalizes_dataset_names():
    assert slugify("AADS 실패 Regression!") == "aads-regression"
    assert slugify("") == "llmops-dataset"


# ── external export gate ──────────────────────────────────────────────────


def test_export_is_disabled_by_default(monkeypatch):
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_ENDPOINT", "LANGSMITH_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    gate = llmops_export.export_gate_status()

    assert gate["enabled"] is False
    assert gate["default"] == "disabled"
    assert set(gate["blockers"]) == {
        "LANGSMITH_TRACING_not_enabled",
        "LANGSMITH_ENDPOINT_missing",
        "LANGSMITH_API_KEY_missing",
    }


def test_export_stays_blocked_until_all_three_env_vars_exist(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    gate = llmops_export.export_gate_status()

    assert gate["enabled"] is False
    assert gate["blockers"] == ["LANGSMITH_API_KEY_missing"]


def test_export_gate_never_returns_the_api_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com/v1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_abcdefghijklmnop0123456789")

    gate = llmops_export.export_gate_status()

    assert gate["enabled"] is True
    assert gate["api_key_present"] is True
    assert gate["endpoint_host"] == "api.smith.langchain.com"
    assert "lsv2_pt_abcdefghijklmnop0123456789" not in repr(gate)


def test_prepare_export_refuses_when_gate_closed(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    result = llmops_export.prepare_export([{"input_summary": "hello"}])

    assert result["blocked"] is True
    assert result["reason"] == "export_gate_closed"
    assert result["payload"] == []


def test_masking_redacts_secrets_and_pii():
    masked = llmops_export.mask_text(
        "token sk-ant-oat01-AbCdEfGhIjKlMnOp mail moong76@gmail.com key AIza"
        + "B" * 35
    )

    assert "sk-ant-oat01" not in masked
    assert "moong76@gmail.com" not in masked
    assert "AIza" not in masked
    assert llmops_export.MASK in masked


def test_prepare_export_masks_payload_before_egress(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "1")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_abcdefghijklmnop0123456789")

    result = llmops_export.prepare_export(
        [{"input_summary": "key sk-ant-oat01-AbCdEfGhIjKlMnOp", "output_summary": "ok"}]
    )

    # Even with the gate open, nothing is actually sent yet.
    assert result["exported"] == 0
    assert result["blocked"] is True
    assert "sk-ant-oat01" not in str(result["payload"])
    assert result["payload"][0]["masking_policy"] == llmops_export.MASKING_POLICY_VERSION


# ── API surface ───────────────────────────────────────────────────────────


def test_llmops_router_exposes_the_prd_routes():
    from app.api.ohvis_llmops import router

    paths = {route.path for route in router.routes}

    assert "/ohvis/llmops/status" in paths
    assert "/ohvis/llmops/traces" in paths
    assert "/ohvis/llmops/traces/{trace_id:path}" in paths
    assert "/ohvis/llmops/datasets/from-trace" in paths
    assert "/ohvis/llmops/evals/run" in paths
    assert "/ohvis/llmops/evals/{experiment_id}" in paths


def test_llmops_router_declares_no_extra_auth_dependency():
    """OHVIS APIs are gated by the global jwt middleware, not per-route deps."""
    from app.api.ohvis_llmops import router

    assert router.dependencies == []


def test_harness_status_lists_llmops_foundation_tables():
    from app.services.ohvis_harness import FOUNDATION_TABLES

    for table in LLMOPS_TABLES:
        assert table in FOUNDATION_TABLES


def test_harness_trace_writes_a_deterministic_trace_id():
    """Legacy harness rows now carry a run-scoped trace id (provenance hook)."""
    from app.services.ohvis_harness_trace import _derive_trace_id

    assert _derive_trace_id("goal:abc") == trace_id_for_graph_run("goal:abc")
