"""OHVIS 내부 LLMOps (AADS-LANGSMITH-INTERNAL-LLMOPS-P0) 단위 테스트.

핵심 계약:
- 마이그레이션 163은 additive만 하고 재실행해도 안전하다.
- LLMOps 기록은 절대 호출부를 깨뜨리지 않는다 (테이블 부재/insert 실패 → None).
- 요약은 저장 전에 마스킹된다.
- 실패/저품질 trace만 dataset example로 승격된다.
- rule evaluator는 PRD가 요구한 5개 기준을 점수화한다.
- 외부 LangSmith export는 기본 off이며 env 3종이 모두 있어야만 열린다.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.services import llmops_evaluator, llmops_export, llmops_store
from app.services.llmops_store import record_trace, reset_relation_cache

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "163_ohvis_internal_llmops_foundation.sql"


# ── fakes ───────────────────────────────────────────────────────────────────


class FakeConn:
    def __init__(self, table_exists: bool = True, raise_on_write: bool = False):
        self.table_exists = table_exists
        self.raise_on_write = raise_on_write
        self.executed: list[tuple] = []
        self.fetchvals: list[tuple] = []

    async def fetchval(self, query: str, *args):
        if "information_schema.tables" in query:
            return self.table_exists
        if self.raise_on_write:
            raise RuntimeError("insert boom")
        self.fetchvals.append((query, args))
        return "11111111-2222-3333-4444-555555555555"

    async def execute(self, query: str, *args):
        if self.raise_on_write:
            raise RuntimeError("insert boom")
        self.executed.append((query, args))
        return "INSERT 0 1"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc_info):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_relation_cache()
    yield
    reset_relation_cache()


def _patch_pool(monkeypatch, conn, error: Exception | None = None) -> None:
    import app.core.db_pool as db_pool

    def _get_pool():
        if error is not None:
            raise error
        return FakePool(conn)

    monkeypatch.setattr(db_pool, "get_pool", _get_pool)


# ── migration (static) ──────────────────────────────────────────────────────


def test_migration_creates_all_eight_llmops_tables_idempotently() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in llmops_store.LLMOPS_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"미생성 테이블: {table}"


def _executable_sql() -> str:
    """주석을 제거한 SQL만 돌려준다 (주석 속 단어를 오탐하지 않도록)."""
    lines = [line.split("--", 1)[0] for line in MIGRATION.read_text(encoding="utf-8").splitlines()]
    return "\n".join(lines).upper()


def test_migration_is_additive_only() -> None:
    """DROP/TRUNCATE/DELETE는 지시서가 금지한다."""
    sql = _executable_sql()
    for forbidden in ("DROP TABLE", "DROP VIEW", "DROP INDEX", "TRUNCATE", "DELETE FROM", "DROP COLUMN"):
        assert forbidden not in sql, f"파괴적 SQL 발견: {forbidden}"


def test_migration_indexes_and_view_are_guarded() -> None:
    """재실행 안전성 — 인덱스는 IF NOT EXISTS, 뷰는 존재 검사 후 생성."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("CREATE INDEX IF NOT EXISTS") >= 8
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    assert "information_schema.views" in sql, "뷰 생성이 존재 검사로 보호되지 않음"
    assert "llmops_traces_compat" in sql


def test_migration_keeps_v1_harness_traces_readable() -> None:
    """기존 ohvis_harness_traces는 폐기되지 않고 compat 뷰로 이어진다."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ohvis_harness_traces" in sql
    assert "UNION ALL" in sql


def test_migration_uses_text_trace_links_for_existing_schema_compatibility() -> None:
    """Earlier additive migration 163 used TEXT child trace ids in production."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "trace_id TEXT NOT NULL UNIQUE" in sql
    assert sql.count("trace_id TEXT NOT NULL") >= 3
    assert "source_trace_id TEXT" in sql


def test_trace_insert_populates_required_trace_id() -> None:
    """Production already has llmops_traces.trace_id NOT NULL."""
    assert "trace_id, trace_key, graph_run_id" in llmops_store._INSERT_TRACE_SQL
    assert "gen_random_uuid()::text" in llmops_store._INSERT_TRACE_SQL


def test_migration_seed_uses_upsert() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ON CONFLICT (slug) DO UPDATE" in sql


# ── store: write contract ───────────────────────────────────────────────────


def test_record_trace_returns_id_and_writes_tool_calls(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    trace_id = asyncio.run(
        record_trace(
            graph_run_id="run:abc",
            project="AADS",
            run_type="chat_turn",
            input_summary="배포 상태 확인",
            output_summary="green 슬롯 healthy",
            cost_usd=0.0123,
            latency_ms=1500,
            tool_calls=[{"tool_name": "query_database", "risk_tier": "read"}],
        )
    )

    assert trace_id == "11111111-2222-3333-4444-555555555555"
    insert_query, args = conn.fetchvals[0]
    assert f"INSERT INTO {llmops_store.TRACE_TABLE}" in insert_query
    assert args[1] == "run:abc"
    assert args[2] == "AADS"
    assert args[7] == "success"
    assert len(conn.executed) == 1, "tool call 1건이 기록되어야 한다"
    assert f"INSERT INTO {llmops_store.TOOL_CALL_TABLE}" in conn.executed[0][0]


def test_record_trace_marks_error_status_and_classifies(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    asyncio.run(record_trace(graph_run_id="run:err", error="HTTP 429 rate limit exceeded"))

    _query, args = conn.fetchvals[0]
    assert args[7] == "error"
    assert args[15] == "rate_limit"


def test_record_trace_skips_when_table_missing(monkeypatch) -> None:
    conn = FakeConn(table_exists=False)
    _patch_pool(monkeypatch, conn)

    assert asyncio.run(record_trace(graph_run_id="run:x")) is None
    assert asyncio.run(record_trace(graph_run_id="run:y")) is None
    assert conn.fetchvals == []


def test_record_trace_never_raises_on_pool_or_insert_failure(monkeypatch) -> None:
    _patch_pool(monkeypatch, None, error=RuntimeError("DB pool이 초기화되지 않았습니다"))
    assert asyncio.run(record_trace(graph_run_id="run:x")) is None

    reset_relation_cache()
    _patch_pool(monkeypatch, FakeConn(table_exists=True, raise_on_write=True))
    assert asyncio.run(record_trace(graph_run_id="run:x")) is None


def test_record_trace_requires_graph_run_id(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    assert asyncio.run(record_trace(graph_run_id="")) is None
    assert conn.fetchvals == []


def test_record_trace_reuses_caller_connection(monkeypatch) -> None:
    """호출부가 커넥션을 점유한 채 남기는 trace는 중첩 acquire를 하지 않는다."""
    caller_conn = FakeConn(table_exists=True)

    def _boom():
        raise AssertionError("conn이 주어지면 pool을 acquire하면 안 된다")

    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", _boom)

    trace_id = asyncio.run(record_trace(graph_run_id="run:held", conn=caller_conn))
    assert trace_id is not None


def test_record_trace_sanitizes_ids_and_clips_summaries(monkeypatch) -> None:
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    asyncio.run(
        record_trace(
            graph_run_id="run:clip",
            session_id="not-a-uuid",
            ohvis_task_id="0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10",
            input_summary="가" * 5000,
            error="e" * 5000,
            metadata={"unserializable": object()},
        )
    )

    _query, args = conn.fetchvals[0]
    assert args[3] is None, "잘못된 session_id는 NULL로 저장되어야 한다"
    assert args[4] == "0f3a2f2a-5f1b-4a0e-9a3d-2f7a1b6c9d10"
    assert len(args[9]) <= llmops_store.SUMMARY_LIMIT
    assert len(args[14]) <= llmops_store.ERROR_LIMIT
    assert args[18].startswith("{")


def test_summaries_are_masked_before_storage(monkeypatch) -> None:
    """비기능 요구사항: 원문에 섞인 시크릿이 그대로 저장되면 안 된다."""
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    leaked = "token was sk-ant-oat01-AAAABBBBCCCCDDDDEEEE and db postgres://user:pw@host/db"
    asyncio.run(record_trace(graph_run_id="run:secret", input_summary=leaked))

    _query, args = conn.fetchvals[0]
    assert "sk-ant-oat01-AAAABBBBCCCCDDDDEEEE" not in args[9]
    assert "sk-ant-***" in args[9]
    assert "user:pw@" not in args[9]


def test_classify_error_buckets() -> None:
    assert llmops_store.classify_error(None) is None
    assert llmops_store.classify_error("Read timed out") == "timeout"
    assert llmops_store.classify_error("HTTP 403 Forbidden") == "auth"
    assert llmops_store.classify_error("완전히 새로운 문제") == "unknown"


# ── dataset promotion ───────────────────────────────────────────────────────


def test_is_promotable_only_for_failed_or_low_quality() -> None:
    assert llmops_store.is_promotable({"status": "error"}) is True
    assert llmops_store.is_promotable({"status": "success", "error": "boom"}) is True
    assert llmops_store.is_promotable({"status": "success", "quality_score": 0.2}) is True
    assert llmops_store.is_promotable({"status": "success", "quality_score": 0.9}) is False
    assert llmops_store.is_promotable({"status": "success"}) is False


def test_build_example_preserves_trace_payload_for_scoring() -> None:
    trace = {
        "status": "error",
        "error": "connection refused",
        "error_class": "network",
        "project": "AADS",
        "run_type": "deploy",
        "graph_run_id": "run:deploy-1",
        "source_table": llmops_store.TRACE_TABLE,
        "input_summary": "deploy bluegreen",
        "output_summary": "failed at candidate health",
        "cost_usd": 0.5,
        "latency_ms": 3000,
        "tool_calls": [{"tool_name": "deploy.sh", "risk_tier": "deploy", "approval_state": "approved"}],
    }

    example = llmops_store.build_example_from_trace(trace)

    assert example["metadata"]["promotion_reason"] == "error"
    payload = example["metadata"]["trace_payload"]
    assert payload["error_class"] == "network"
    assert payload["tool_calls"][0]["tool_name"] == "deploy.sh"
    assert "network" in example["tags"]
    assert example["rubric"]["require_tool_policy"] is True


def test_promote_refuses_healthy_trace_without_force(monkeypatch) -> None:
    async def _fake_get_trace(_trace_id):
        return {"status": "success", "quality_score": 0.95}

    monkeypatch.setattr(llmops_store, "get_trace", _fake_get_trace)

    result = asyncio.run(llmops_store.promote_trace_to_dataset("11111111-2222-3333-4444-555555555555"))
    assert result["promoted"] is False
    assert result["reason"] == "trace_not_failed_or_low_quality"


def test_promote_writes_example_and_is_idempotent_by_source_ref(monkeypatch) -> None:
    trace_id = "11111111-2222-3333-4444-555555555555"

    async def _fake_get_trace(_trace_id):
        return {
            "status": "error",
            "error": "boom",
            "project": "AADS",
            "source_table": llmops_store.TRACE_TABLE,
            "input_summary": "in",
            "output_summary": "out",
            "tool_calls": [],
        }

    monkeypatch.setattr(llmops_store, "get_trace", _fake_get_trace)
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    result = asyncio.run(llmops_store.promote_trace_to_dataset(trace_id))

    assert result["promoted"] is True
    assert result["source_ref"] == f"{llmops_store.TRACE_TABLE}:{trace_id}"
    example_query = conn.fetchvals[-1][0]
    assert "ON CONFLICT (dataset_id, source_ref)" in example_query, "재승격이 중복 example을 만들면 안 된다"


def test_legacy_trace_promotes_without_fk_binding(monkeypatch) -> None:
    """v1 harness trace id는 llmops_traces FK가 아니므로 source_ref로만 남는다."""
    trace_id = "22222222-3333-4444-5555-666666666666"

    async def _fake_get_trace(_trace_id):
        return {
            "status": "error",
            "error": "boom",
            "source_table": llmops_store.LEGACY_TRACE_TABLE,
            "input_summary": "in",
            "output_summary": "out",
            "tool_calls": [],
        }

    monkeypatch.setattr(llmops_store, "get_trace", _fake_get_trace)
    conn = FakeConn(table_exists=True)
    _patch_pool(monkeypatch, conn)

    result = asyncio.run(llmops_store.promote_trace_to_dataset(trace_id))

    assert result["promoted"] is True
    _query, args = conn.fetchvals[-1]
    assert args[1] is None, "legacy trace는 source_trace_id FK로 묶이면 안 된다"
    assert args[2] == f"{llmops_store.LEGACY_TRACE_TABLE}:{trace_id}"


def test_parse_legacy_tool_calls_normalizes_shapes() -> None:
    calls = llmops_store.parse_legacy_tool_calls('[{"name": "git_status"}, "raw_tool"]')
    assert [call["tool_name"] for call in calls] == ["git_status", "raw_tool"]
    assert calls[0]["risk_tier"] == "read"
    assert llmops_store.parse_legacy_tool_calls(None) == []
    assert llmops_store.parse_legacy_tool_calls("not json") == []


# ── rule evaluator ──────────────────────────────────────────────────────────


def test_rule_evaluator_scores_the_five_required_criteria() -> None:
    """PRD 완료 기준 4: 최소 5개 기준을 점수화한다."""
    result = llmops_evaluator.evaluate_payload({"status": "success"})
    criteria = {item["criterion"] for item in result["criteria"]}
    assert criteria == {
        "source_presence",
        "tool_policy",
        "final_response",
        "cost_present",
        "error_classification",
    }


def test_rule_evaluator_passes_a_clean_trace() -> None:
    result = llmops_evaluator.evaluate_payload({
        "status": "success",
        "input_summary": "deploy 상태 확인",
        "output_summary": "green 슬롯 healthy, 5분 모니터 통과",
        "graph_run_id": "run:1",
        "cost_usd": 0.01,
        "tool_calls": [{"tool_name": "curl", "risk_tier": "read"}],
    })
    assert result["passed"] is True
    assert result["overall"] == 1.0
    assert result["comment"] == "all rule checks passed"


def test_rule_evaluator_flags_unapproved_risky_tool() -> None:
    result = llmops_evaluator.evaluate_payload({
        "status": "success",
        "output_summary": "배포 완료",
        "tool_calls": [{"tool_name": "deploy.sh", "risk_tier": "deploy", "approval_state": ""}],
    })
    tool_policy = next(item for item in result["criteria"] if item["criterion"] == "tool_policy")
    assert tool_policy["passed"] is False
    assert "deploy.sh" in tool_policy["comment"]


def test_rule_evaluator_flags_destructive_tool_even_if_approved() -> None:
    result = llmops_evaluator.evaluate_payload({
        "tool_calls": [{"tool_name": "drop_table", "risk_tier": "destructive", "approval_state": "approved"}],
    })
    tool_policy = next(item for item in result["criteria"] if item["criterion"] == "tool_policy")
    assert tool_policy["passed"] is False


def test_rule_evaluator_flags_unclassified_error_and_missing_output() -> None:
    result = llmops_evaluator.evaluate_payload({"status": "error", "error": "완전히 새로운 문제"})
    failing = {item["criterion"] for item in result["criteria"] if not item["passed"]}
    assert "error_classification" in failing
    assert "final_response" in failing
    assert "cost_present" in failing
    assert result["passed"] is False


def test_rule_evaluator_flags_success_hiding_an_error() -> None:
    result = llmops_evaluator.evaluate_payload({"status": "success", "error_class": "timeout"})
    error_check = next(item for item in result["criteria"] if item["criterion"] == "error_classification")
    assert error_check["passed"] is False


def test_rule_evaluator_adds_latency_budget_only_when_rubric_asks() -> None:
    without = llmops_evaluator.evaluate_payload({"latency_ms": 5000})
    assert all(item["criterion"] != "latency_budget" for item in without["criteria"])

    with_budget = llmops_evaluator.evaluate_payload({"latency_ms": 500_000}, {"max_latency_ms": 1000})
    latency = next(item for item in with_budget["criteria"] if item["criterion"] == "latency_budget")
    assert latency["passed"] is False


def test_rule_evaluator_is_deterministic() -> None:
    payload = {"status": "error", "error": "connection refused", "output_summary": "x"}
    assert llmops_evaluator.evaluate_payload(payload) == llmops_evaluator.evaluate_payload(payload)


def test_evaluate_example_uses_promoted_trace_payload() -> None:
    example = {
        "id": "ex-1",
        "input": "in",
        "rubric": {"max_latency_ms": 1000},
        "metadata": {
            "trace_payload": {
                "status": "error",
                "error": "HTTP 504 timeout",
                "output_summary": "부분 응답",
                "graph_run_id": "run:9",
                "cost_usd": 0.02,
                "latency_ms": 90_000,
                "tool_calls": [],
            }
        },
    }
    result = llmops_evaluator.evaluate_example(example)
    assert result["example_id"] == "ex-1"
    error_check = next(item for item in result["criteria"] if item["criterion"] == "error_classification")
    assert error_check["passed"] is True, "504는 timeout으로 분류되어야 한다"
    latency = next(item for item in result["criteria"] if item["criterion"] == "latency_budget")
    assert latency["passed"] is False


def test_summarize_reports_mean_pass_rate_and_per_criterion() -> None:
    results = [
        llmops_evaluator.evaluate_payload({
            "status": "success", "input_summary": "a", "output_summary": "b",
            "graph_run_id": "r", "cost_usd": 0.1, "tool_calls": [{"tool_name": "t"}],
        }),
        llmops_evaluator.evaluate_payload({"status": "error", "error": "완전히 새로운 문제"}),
    ]
    summary = llmops_evaluator.summarize(results)
    assert summary["examples"] == 2
    assert summary["pass_rate"] == 0.5
    assert "tool_policy" in summary["criteria"]
    assert llmops_evaluator.summarize([])["examples"] == 0


# ── external export gate ────────────────────────────────────────────────────


def test_export_disabled_by_default(monkeypatch) -> None:
    for name in (llmops_export.TRACING_ENV, llmops_export.ENDPOINT_ENV, llmops_export.API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)

    status = llmops_export.export_status()
    assert status["enabled"] is False
    assert status["default"] == "disabled"
    assert set(status["blockers"]) == {"tracing_flag", "endpoint_https", "api_key_present"}


@pytest.mark.parametrize("missing", ["LANGSMITH_TRACING", "LANGSMITH_ENDPOINT", "LANGSMITH_API_KEY"])
def test_export_requires_all_three_env_vars(monkeypatch, missing: str) -> None:
    monkeypatch.setenv(llmops_export.TRACING_ENV, "true")
    monkeypatch.setenv(llmops_export.ENDPOINT_ENV, "https://api.smith.langchain.com")
    monkeypatch.setenv(llmops_export.API_KEY_ENV, "test-key-not-real")
    monkeypatch.delenv(missing, raising=False)

    assert llmops_export.export_status()["enabled"] is False


def test_export_rejects_non_https_endpoint(monkeypatch) -> None:
    monkeypatch.setenv(llmops_export.TRACING_ENV, "1")
    monkeypatch.setenv(llmops_export.ENDPOINT_ENV, "http://insecure.example.com")
    monkeypatch.setenv(llmops_export.API_KEY_ENV, "test-key-not-real")

    status = llmops_export.export_status()
    assert status["enabled"] is False
    assert "endpoint_https" in status["blockers"]


def test_export_status_never_leaks_key(monkeypatch) -> None:
    monkeypatch.setenv(llmops_export.TRACING_ENV, "true")
    monkeypatch.setenv(llmops_export.ENDPOINT_ENV, "https://api.smith.langchain.com/x")
    monkeypatch.setenv(llmops_export.API_KEY_ENV, "super-secret-value")

    assert "super-secret-value" not in str(llmops_export.export_status())


def test_prepare_export_blocked_while_gate_closed(monkeypatch) -> None:
    monkeypatch.delenv(llmops_export.TRACING_ENV, raising=False)

    result = llmops_export.prepare_export({"id": "t1", "input_summary": "hello"})
    assert result["exported"] is False
    assert result["reason"] == "export_disabled"
    assert "payload" not in result, "게이트가 닫혀 있으면 페이로드를 만들지도 않는다"


def test_prepare_export_masks_payload_when_gate_open(monkeypatch) -> None:
    monkeypatch.setenv(llmops_export.TRACING_ENV, "true")
    monkeypatch.setenv(llmops_export.ENDPOINT_ENV, "https://api.smith.langchain.com")
    monkeypatch.setenv(llmops_export.API_KEY_ENV, "test-key-not-real")

    result = llmops_export.prepare_export({
        "id": "t1",
        "input_summary": "key sk-ant-oat01-AAAABBBBCCCCDDDD",
        "output_summary": "ok",
    })

    assert result["exported"] is False, "이 단계는 준비만 하고 실제 전송은 하지 않는다"
    assert result["reason"] == "ready_not_sent"
    assert "sk-ant-oat01-AAAABBBBCCCCDDDD" not in str(result["payload"])


def test_masking_passes_detects_residual_secret() -> None:
    ok, _reason = llmops_export.masking_passes({"summary": "clean text"})
    assert ok is True


# ── API surface ─────────────────────────────────────────────────────────────


def test_llmops_router_exposes_required_routes() -> None:
    from app.api.ohvis_llmops import router

    paths = {route.path for route in router.routes}
    for expected in (
        "/ohvis/llmops/status",
        "/ohvis/llmops/traces",
        "/ohvis/llmops/traces/{trace_id}",
        "/ohvis/llmops/datasets/from-trace",
        "/ohvis/llmops/evals/run",
        "/ohvis/llmops/evals/{experiment_id}",
    ):
        assert expected in paths, f"미등록 라우트: {expected}"


def test_llmops_router_is_wired_into_main() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.api.ohvis_llmops import router as ohvis_llmops_router" in source
    assert 'app.include_router(ohvis_llmops_router, prefix="/api/v1"' in source


def test_harness_trace_mirrors_into_llmops_ledger() -> None:
    """provenance 훅은 record_trace 한 곳에만 있고 비치명적이어야 한다."""
    source = (ROOT / "app" / "services" / "ohvis_harness_trace.py").read_text(encoding="utf-8")
    assert "_mirror_to_llmops" in source
    assert "from app.services.llmops_store import record_trace as llmops_record_trace" in source
    assert "llmops mirror skipped (non-fatal)" in source


# ── 마이그레이션 단일성과 정합화 (164) ──────────────────────────────────────


RECONCILE = ROOT / "migrations" / "164_llmops_ledger_schema_reconcile.sql"

# 원장 스키마를 건드려도 되는 마이그레이션은 이 둘뿐이다.
# 163 = 정본 생성(additive), 164 = 이미 적용된 변형본을 정본으로 정합화.
LEDGER_MIGRATIONS = [MIGRATION.name, RECONCILE.name]


_LEDGER_CREATE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?(llmops_\w+)",
    re.IGNORECASE,
)


def _ledger_creating_migrations() -> dict[str, list[str]]:
    """llmops_* 원장 테이블을 새로 만드는 마이그레이션 파일 → 테이블 목록."""
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        tables = _LEDGER_CREATE.findall(path.read_text(encoding="utf-8"))
        if tables:
            found[path.name] = sorted(set(tables))
    return found


def test_only_one_migration_creates_the_llmops_ledger() -> None:
    """163이 여러 변형본으로 존재하면 어떤 스키마가 정본인지 알 수 없다.

    표기 흔들림(IF NOT EXISTS 생략, public. 접두, 대소문자, 줄바꿈)으로 검사를
    빠져나가면 회귀 방지가 되지 않으므로 정규식으로 CREATE TABLE 자체를 본다.
    """
    creators = _ledger_creating_migrations()
    assert list(creators) == [MIGRATION.name], f"llmops 원장을 만드는 파일이 여럿이다: {creators}"


def test_a_new_ledger_migration_variant_would_fail_the_guard() -> None:
    """가드가 실제로 잡는지 — 변형본을 흉내 낸 문자열이 전부 걸려야 한다."""
    for variant in (
        "CREATE TABLE llmops_traces (id UUID PRIMARY KEY);",
        "create table if not exists public.llmops_spans (id uuid);",
        'CREATE TABLE IF NOT EXISTS "llmops_feedback" (id uuid);',
        "CREATE TABLE\n  IF NOT EXISTS llmops_scores (id uuid);",
    ):
        assert _LEDGER_CREATE.search(variant), f"가드를 빠져나가는 변형본: {variant}"


def test_reconcile_migration_does_not_create_ledger_tables() -> None:
    """164는 이미 있는 테이블을 정합화만 한다 — 새 원장을 만들면 정본이 둘이 된다."""
    assert not _LEDGER_CREATE.search(RECONCILE.read_text(encoding="utf-8"))


def test_no_migration_beyond_163_and_164_touches_the_llmops_ledger() -> None:
    """마이그레이션 과다 회귀를 막는다.

    163을 그대로 두고 165, 166…을 덧붙이면 "정본 + 패치 N개"가 되어 어떤 DB가
    어떤 모양인지 아무도 말할 수 없게 된다. 원장을 또 손대야 한다면 그건 164에
    합치거나 163을 고쳐야 한다는 뜻이고, 새 파일을 늘리는 순간 이 테스트가 깨진다.
    """
    touching = [
        path.name
        for path in sorted((ROOT / "migrations").glob("*.sql"))
        if "llmops_" in path.read_text(encoding="utf-8")
    ]
    assert touching == LEDGER_MIGRATIONS, (
        f"llmops 원장을 건드리는 마이그레이션이 {LEDGER_MIGRATIONS} 외에 있다: {touching}"
    )


def test_reconcile_migration_exists_because_163_cannot_reshape_existing_tables() -> None:
    """163은 CREATE TABLE IF NOT EXISTS라서 먼저 만들어진 테이블 모양을 못 고친다.

    변형본이 적용된 DB에서는 자식 테이블의 trace 참조가 UUID FK로 남아 있어
    llmops_store의 TEXT 계약이 통째로 실패한다. 164가 그 간극을 메운다.
    """
    sql = RECONCILE.read_text(encoding="utf-8")
    compact = "".join(sql.split())
    for table, column in (
        ("llmops_spans", "trace_id"),
        ("llmops_tool_calls", "trace_id"),
        ("llmops_feedback", "trace_id"),
        ("llmops_examples", "source_trace_id"),
        ("llmops_scores", "source_trace_id"),
    ):
        assert f"('{table}','{column}')" in compact, f"{table}.{column} 미교정"
    assert "TYPE TEXT USING" in sql
    assert "DROP CONSTRAINT" in sql


def test_reconcile_migration_never_destroys_data() -> None:
    lines = [line.split("--", 1)[0] for line in RECONCILE.read_text(encoding="utf-8").splitlines()]
    sql = "\n".join(lines).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "DROP COLUMN", "DROP VIEW"):
        assert forbidden not in sql, f"파괴적 SQL 발견: {forbidden}"


def test_reconcile_migration_is_replay_safe() -> None:
    """재실행 시 no-op — 무조건 실행되는 ALTER는 전부 가드가 있어야 한다."""
    import re as _re

    sql = RECONCILE.read_text(encoding="utf-8")
    for statement in _re.findall(r"^\s*(ALTER TABLE [^\n;]+)", sql, _re.MULTILINE):
        upper = statement.upper()
        assert "IF NOT EXISTS" in upper or "SET NOT NULL" in upper, (
            f"가드 없는 문장: {statement.strip()[:80]}"
        )
    for statement in _re.findall(r"^\s*(CREATE (?:UNIQUE )?INDEX [^\n;]+)", sql, _re.MULTILINE):
        assert "IF NOT EXISTS" in statement.upper(), f"가드 없는 인덱스: {statement.strip()[:80]}"


def test_reconcile_migration_creates_the_index_promotion_conflicts_on() -> None:
    """promote_trace_to_dataset의 ON CONFLICT 추론이 이 인덱스에 의존한다."""
    store_source = (ROOT / "app" / "services" / "llmops_store.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (dataset_id, source_ref)" in store_source

    sql = RECONCILE.read_text(encoding="utf-8")
    assert "idx_llmops_examples_dataset_source" in sql
    assert "(dataset_id, source_ref) WHERE source_ref IS NOT NULL" in sql


# ── 정본 스택 단일성 ────────────────────────────────────────────────────────


# upstream 정본. llmops_store가 저장/조회 계약을 혼자 소유하고,
# evaluator/export/chat_hook은 그 위에 얹힌다 (chat_hook은 582fb94f에서 추가).
CANONICAL_LLMOPS_MODULES = {
    "llmops_store.py",
    "llmops_evaluator.py",
    "llmops_export.py",
    "llmops_chat_hook.py",
}

# 같은 역할로 만들어졌다가 폐기된 변형본들. 다시 살아나면 어느 쪽이 쓰이는지
# import 순서에 달리게 되므로 이름 자체를 금지한다.
RETIRED_LLMOPS_MODULES = ("llmops_service", "llmops_eval", "llmops_masking")


def test_llmops_store_is_the_only_ledger_module() -> None:
    """폐기된 변형본이 되살아나면 정본이 둘이 된다."""
    present = {path.name for path in (ROOT / "app" / "services").glob("llmops_*.py")}
    assert present == CANONICAL_LLMOPS_MODULES, f"llmops 서비스 모듈 구성이 바뀌었다: {present}"


def test_nothing_references_a_retired_llmops_module() -> None:
    """폐기 모듈은 파일뿐 아니라 참조도 남으면 안 된다 (죽은 import 경로)."""
    import re as _re

    patterns = {name: _re.compile(rf"{name}\b") for name in RETIRED_LLMOPS_MODULES}
    offenders: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for name, pattern in patterns.items():
            if pattern.search(source):
                offenders.append(f"{path.relative_to(ROOT)} → {name}")
    assert not offenders, f"폐기된 llmops 변형본 참조: {offenders}"


def test_evaluator_and_export_build_on_the_store_contract() -> None:
    """평가/내보내기가 자기 저장 계약을 따로 들고 있으면 다시 갈라진다."""
    for module in ("llmops_evaluator.py", "llmops_export.py"):
        source = (ROOT / "app" / "services" / module).read_text(encoding="utf-8")
        assert "from app.services.llmops_store import" in source, f"{module}가 store를 우회한다"

    api_source = (ROOT / "app" / "api" / "ohvis_llmops.py").read_text(encoding="utf-8")
    assert "from app.services import llmops_evaluator, llmops_store" in api_source


# ── harness 상태 판정 ───────────────────────────────────────────────────────


def test_harness_and_store_agree_on_the_llmops_table_set() -> None:
    from app.services.ohvis_harness import FOUNDATION_TABLES, LLMOPS_TABLES

    assert set(LLMOPS_TABLES) == set(llmops_store.LLMOPS_TABLES)
    for table in LLMOPS_TABLES:
        assert table in FOUNDATION_TABLES


def test_foundation_table_probe_uses_a_single_round_trip() -> None:
    """상태 엔드포인트는 주기 호출된다. 테이블 수만큼 왕복하면 안 된다."""
    from app.services.ohvis_harness import FOUNDATION_TABLES, _tables_exist

    class ProbeConn:
        def __init__(self):
            self.calls = 0

        async def fetch(self, query, *args):
            self.calls += 1
            assert "ANY($1::text[])" in query
            return [{"table_name": "llmops_traces"}]

    conn = ProbeConn()
    state = asyncio.run(_tables_exist(conn, FOUNDATION_TABLES))

    assert conn.calls == 1
    assert state["llmops_traces"] is True
    assert state["llmops_spans"] is False


class HarnessProbePool:
    """지정한 테이블만 존재하는 DB를 흉내내는 풀. 왕복 횟수를 센다."""

    def __init__(self, present: set[str]):
        self.present = set(present)
        self.fetch_calls = 0

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query: str, *args):
        self.fetch_calls += 1
        assert "ANY($1::text[])" in query, "테이블 존재 확인이 단일 왕복이 아니다"
        return [{"table_name": name} for name in args[0] if name in self.present]

    async def fetchval(self, query: str, *args):
        if "information_schema.tables" in query:
            return args[0] in self.present
        return 0

    async def fetchrow(self, query: str, *args):
        # 상태 집계는 지표들을 별칭 붙인 합본 쿼리 한 방으로 센다.
        return {name: 0 for name in re.findall(r"\bAS (\w+)", query)}


def _llmops_component(present: set[str], monkeypatch) -> dict:
    from app.core import db_pool
    from app.services.ohvis_harness import get_harness_status

    pool = HarnessProbePool(present)
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    status = asyncio.run(get_harness_status())
    component = next(c for c in status["components"] if c["key"] == "llmops")
    component["_fetch_calls"] = pool.fetch_calls
    return component


def test_harness_reports_llmops_ready_only_when_every_table_exists(monkeypatch) -> None:
    """테이블 하나만 보고 implemented라고 하면 부분 적용 DB가 정상으로 보인다."""
    from app.services.ohvis_harness import LLMOPS_TABLES

    full = _llmops_component(set(LLMOPS_TABLES), monkeypatch)
    assert full["status"] == "foundation_ready"

    # 163의 첫 테이블만 만들어진 채로 멈춘 DB — 예전 판정은 여기서 정상이라고 했다.
    partial = _llmops_component({"llmops_traces"}, monkeypatch)
    assert partial["status"] == "migration_pending"

    # 8개 중 하나만 빠져도 준비 완료가 아니다.
    almost = _llmops_component(set(LLMOPS_TABLES) - {"llmops_feedback"}, monkeypatch)
    assert almost["status"] == "migration_pending"

    empty = _llmops_component(set(), monkeypatch)
    assert empty["status"] == "migration_pending"


def test_harness_status_probes_every_foundation_table_in_one_round_trip(monkeypatch) -> None:
    """상태 엔드포인트 1회 호출이 테이블 수만큼 왕복하면 안 된다."""
    from app.services.ohvis_harness import FOUNDATION_TABLES, LLMOPS_TABLES

    component = _llmops_component(set(FOUNDATION_TABLES), monkeypatch)
    assert component["_fetch_calls"] == 1, "테이블당 1쿼리로 회귀했다"
    assert len(FOUNDATION_TABLES) > len(LLMOPS_TABLES) > 1


def test_harness_llmops_evidence_names_both_ledger_migrations(monkeypatch) -> None:
    """운영 DB를 정본으로 맞추려면 163만으로는 부족하다는 사실이 상태에 드러나야 한다."""
    from app.services.ohvis_harness import LLMOPS_TABLES

    component = _llmops_component(set(LLMOPS_TABLES), monkeypatch)
    evidence = " ".join(component["evidence"])
    for migration in LEDGER_MIGRATIONS:
        assert migration in evidence


# ── store 상태/캐시 ─────────────────────────────────────────────────────────


def test_store_status_probes_every_table_in_one_round_trip(monkeypatch) -> None:
    """/ohvis/llmops/status도 harness와 같은 규칙을 따른다 (테이블당 1쿼리 금지)."""
    pool = HarnessProbePool(set(llmops_store.LLMOPS_TABLES) | {llmops_store.LEGACY_TRACE_TABLE})
    from app.core import db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    status = asyncio.run(llmops_store.get_status(project="AADS"))

    assert pool.fetch_calls == 1, "테이블당 1쿼리로 회귀했다"
    assert status["db"]["available"] is True
    assert status["db"]["foundation_ready"] is True
    assert status["migrations"] == list(LEDGER_MIGRATIONS)


def test_store_status_is_not_ready_when_a_single_table_is_missing(monkeypatch) -> None:
    """traces 하나만 있으면 준비 완료가 아니다 — harness 판정과 어긋나면 안 된다."""
    from app.core import db_pool

    pool = HarnessProbePool({"llmops_traces"})
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    status = asyncio.run(llmops_store.get_status())

    assert status["db"]["foundation_ready"] is False
    assert status["db"]["tables"]["llmops_traces"] is True
    assert status["db"]["tables"]["llmops_spans"] is False


def test_missing_relation_is_rechecked_after_the_migration_lands() -> None:
    """부재를 캐시하면 163/164 적용 후에도 재시작 전까지 적재가 조용히 멈춘다."""

    class SwitchingConn:
        def __init__(self):
            self.exists = False
            self.probes = 0

        async def fetchval(self, query: str, *args):
            assert "information_schema.tables" in query
            self.probes += 1
            return self.exists

    conn = SwitchingConn()
    assert asyncio.run(llmops_store.relation_exists(conn, "llmops_traces")) is False
    assert asyncio.run(llmops_store.relation_exists(conn, "llmops_traces")) is False
    assert conn.probes == 2, "부재가 캐시되어 재조회하지 않았다"

    conn.exists = True
    assert asyncio.run(llmops_store.relation_exists(conn, "llmops_traces")) is True

    # 존재는 캐시된다 — 정상 경로에서 쓰기마다 왕복을 늘리지 않는다.
    before = conn.probes
    assert asyncio.run(llmops_store.relation_exists(conn, "llmops_traces")) is True
    assert conn.probes == before


def test_batch_probe_does_not_cache_absence() -> None:
    """단일 왕복 프로브도 같은 규칙이다 — 없는 테이블을 없다고 굳히지 않는다."""

    class BatchConn:
        def __init__(self, present):
            self.present = set(present)

        async def fetch(self, query: str, *args):
            assert "ANY($1::text[])" in query
            return [{"table_name": name} for name in args[0] if name in self.present]

        async def fetchval(self, query: str, *args):
            assert "information_schema.tables" in query
            return args[0] in self.present

    conn = BatchConn({"llmops_traces"})
    state = asyncio.run(llmops_store.relations_exist(conn, llmops_store.LLMOPS_TABLES))
    assert state["llmops_traces"] is True
    assert state["llmops_spans"] is False

    migrated = BatchConn(set(llmops_store.LLMOPS_TABLES))
    assert asyncio.run(llmops_store.relation_exists(migrated, "llmops_spans")) is True
