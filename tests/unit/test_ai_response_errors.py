"""AI 응답 오류 통합 VIEW(ai_response_errors) + GET /api/v1/ops/ai-response-errors 계약 검증.

DB 없이 도는 것만 본다: 마이그레이션 SQL 텍스트의 kind 블록 조건, 라우트 등록,
kind 허용 목록, 인증 의존성, 입력 검증, VIEW 부재 시 503, 읽기 전용 보장.
"""
import asyncio
import inspect
import re
from pathlib import Path

import asyncpg
import pytest
from fastapi import HTTPException

from app.api import ops
from app.api.ops import AI_RESPONSE_ERROR_KINDS, router

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "20261002_ai_response_errors_view.sql"
ROLLBACK = ROOT / "migrations" / "20261002_ai_response_errors_view_rollback.sql"
OUTAGE_PREFIX = "'⚠️ _전체 LLM 장애'"


def _strip_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def _blocks() -> dict:
    sql = _strip_comments(MIGRATION.read_text(encoding="utf-8"))
    body = sql.split("CREATE OR REPLACE VIEW ai_response_errors AS", 1)[1]
    body = body.split("COMMENT ON VIEW", 1)[0]
    blocks = {}
    for part in body.split("UNION ALL"):
        m = re.search(r"'(\w+)'::text AS kind", part)
        assert m, part
        blocks[m.group(1)] = part
    return blocks


def _route():
    for r in router.routes:
        if getattr(r, "path", "") == "/ops/ai-response-errors":
            return r
    raise AssertionError("route not registered")


# ─── 마이그레이션 ───────────────────────────────────────────────────────────

def test_view_has_five_kinds_in_order():
    assert list(_blocks()) == [
        "fallback_exhausted", "llm_outage", "interrupted", "low_quality", "error_book_chat",
    ]


def test_migration_is_view_only_no_writes():
    sql = _strip_comments(MIGRATION.read_text(encoding="utf-8")).upper()
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE", "CREATE TABLE"):
        assert word not in sql


def test_rollback_drops_only_the_view():
    assert ROLLBACK.read_text(encoding="utf-8").strip() == "DROP VIEW IF EXISTS ai_response_errors;"


def test_llm_outage_uses_prefix_match_only():
    block = _blocks()["llm_outage"]
    assert "role = 'assistant'" in block
    assert f"starts_with(m.content, {OUTAGE_PREFIX})" in block
    assert "NOT starts_with" not in block
    assert "LIKE" not in block.upper()


def test_llm_outage_block_has_no_interrupted_string():
    assert "interrupted" not in _blocks()["llm_outage"]


def test_interrupted_block_excludes_outage_rows():
    block = _blocks()["interrupted"]
    assert "role = 'assistant'" in block
    assert "m.model_used = 'interrupted'" in block
    assert f"NOT starts_with(m.content, {OUTAGE_PREFIX})" in block


def test_outage_and_interrupted_are_disjoint_by_construction():
    b = _blocks()
    # llm_outage 는 접두 일치, interrupted 는 그 부정 — 같은 접두 문자열이어야 겹치지 않는다.
    assert OUTAGE_PREFIX in b["llm_outage"] and OUTAGE_PREFIX in b["interrupted"]


def test_low_quality_block():
    block = _blocks()["low_quality"]
    assert "role = 'assistant'" in block
    assert "quality_score < 0.4" in block


def test_error_book_chat_block():
    block = _blocks()["error_book_chat"]
    assert "ohvis_wiki_error_book" in block
    assert "error_key LIKE 'chat.%'" in block


def test_fallback_exhausted_block():
    block = _blocks()["fallback_exhausted"]
    assert "FROM error_log" in block
    assert "COALESCE(e.last_seen, e.created_at)" in block
    assert "'occurrence_count', e.occurrence_count" in block
    assert "b.metadata ->> 'error_hash' = e.error_hash" in block
    assert "b.error_key LIKE 'chat.%'" in block
    assert "COUNT(DISTINCT b.error_key) = 1" in block


def test_migration_header_documents_interrupted():
    assert "interrupted" in MIGRATION.read_text(encoding="utf-8").split("CREATE OR REPLACE VIEW")[0]


# ─── 라우트 / 파라미터 ──────────────────────────────────────────────────────

def test_kind_allow_list():
    assert AI_RESPONSE_ERROR_KINDS == (
        "fallback_exhausted", "llm_outage", "interrupted", "low_quality", "error_book_chat",
    )
    assert "interrupted" in AI_RESPONSE_ERROR_KINDS


def test_kind_allow_list_matches_view_kinds():
    assert set(AI_RESPONSE_ERROR_KINDS) == set(_blocks())


def test_route_is_get_and_requires_internal_admin():
    route = _route()
    assert route.methods == {"GET"}
    deps = [d.call for d in route.dependant.dependencies]
    assert ops.require_internal_admin in deps


def test_kind_query_description_mentions_interrupted():
    desc = {p.name: p.field_info.description for p in _route().dependant.query_params}["kind"]
    assert "interrupted" in desc


def test_limit_bounds_are_1_to_500():
    limit = {p.name: p for p in _route().dependant.query_params}["limit"]
    metas = {type(m).__name__: m for m in limit.field_info.metadata}
    assert metas["Ge"].ge == 1
    assert metas["Le"].le == 500
    assert limit.field_info.default == 100


def test_invalid_kind_is_422_before_any_db_access(monkeypatch):
    async def boom():
        raise AssertionError("DB must not be touched")

    monkeypatch.setattr(ops, "_get_conn", boom)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ops.ops_ai_response_errors(since=None, kind="llm_outage' OR 1=1", limit=10))
    assert ei.value.status_code == 422


class _FakeConn:
    def __init__(self, exc=None, summary=None, rows=None):
        self.exc, self.summary, self.rows = exc, summary or [], rows or []
        self.sql, self.closed = [], False

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if self.exc:
            raise self.exc
        return self.summary if "GROUP BY" in sql else self.rows

    async def close(self):
        self.closed = True


def test_missing_view_returns_503(monkeypatch):
    conn = _FakeConn(exc=asyncpg.UndefinedTableError("relation does not exist"))

    async def get_conn():
        return conn

    monkeypatch.setattr(ops, "_get_conn", get_conn)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ops.ops_ai_response_errors(since=None, kind=None, limit=10))
    assert ei.value.status_code == 503
    assert conn.closed


def test_response_shape_and_read_only_queries(monkeypatch):
    from datetime import datetime, timezone

    ts = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
    conn = _FakeConn(
        summary=[{"kind": "interrupted", "cnt": 2, "first_at": ts, "last_at": ts}],
        rows=[{
            "kind": "interrupted", "occurred_at": ts, "source_table": "chat_messages",
            "source_id": "1", "session_id": None, "model_used": "interrupted",
            "summary": "x", "detail": '{"intent": null}', "error_book_key": None,
        }],
    )

    async def get_conn():
        return conn

    monkeypatch.setattr(ops, "_get_conn", get_conn)
    out = asyncio.run(ops.ops_ai_response_errors(since=None, kind="interrupted", limit=5))
    assert out["summary"]["total"] == 2
    assert out["summary"]["by_kind"]["interrupted"]["count"] == 2
    assert out["items"][0]["detail"] == {"intent": None}
    assert out["count"] == 1 and out["limit"] == 5
    assert out["filters"]["kind"] == "interrupted"
    for sql in conn.sql:
        assert sql.lstrip().upper().startswith("SELECT")
        assert "FROM ai_response_errors" in sql


def test_endpoint_source_does_not_touch_other_ops_routes():
    src = inspect.getsource(ops.ops_ai_response_errors)
    assert "INSERT" not in src.upper() and "UPDATE " not in src.upper() and "DELETE" not in src.upper()
