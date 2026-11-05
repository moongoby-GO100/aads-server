"""approval_required 필터/필드: SQL 조건 자체를 sqlite 로 평가해 검증한다."""
import asyncio
import sqlite3

import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs

TENANT = "00000000-0000-0000-0000-000000000001"
CTX = {"tenant": {"id": TENANT}, "user": {"id": "u", "auth_type": "human_session"},
       "membership": {"role": "admin", "status": "active"}}


def _db(rows):
    """rows: (head, latest_tier, approved_is, linked) ; approved_is in None|'latest'|'old'."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE h(id, latest_revision_id, approved_revision_id);"
        "CREATE TABLE latest(id, doc_tier);"
        "CREATE TABLE project_document_goal_links(head_id);")
    for name, tier, appr, linked in rows:
        db.execute("INSERT INTO h VALUES(?,?,?)", (name, name + "-2",
                   None if appr is None else name + ("-2" if appr == "latest" else "-1")))
        db.execute("INSERT INTO latest VALUES(?,?)", (name + "-2", tier))
        if linked:
            db.execute("INSERT INTO project_document_goal_links VALUES(?)", (name,))
    return db


def _eval(rows):
    cond = docs.APPROVAL_REQUIRED_SQL.replace("IS DISTINCT FROM", "IS NOT")
    db = _db(rows)
    q = ("SELECT h.id, " + cond + " FROM h LEFT JOIN latest ON latest.id=h.latest_revision_id")
    return {r[0]: bool(r[1]) for r in db.execute(q)}


def test_definition_cases():
    got = _eval([
        ("a", "D2", None, True),       # (a) 승인 이력 없음
        ("b", "D3", "latest", True),   # (b) 최신 승인됨
        ("c", "D2", "old", True),      # (c) 과거 판본만 승인
        ("d1", "D1", None, True),      # (d) D1
        ("d2", None, None, True),      # 무등급
        ("d3", "D2", None, False),     # 목표 미연결
    ])
    assert got == {"a": True, "b": False, "c": True, "d1": False, "d2": False, "d3": False}


class Conn:
    def __init__(self):
        self.sql = self.args = None

    async def fetch(self, sql, *args):
        if "FROM project_document_heads" in sql:
            self.sql, self.args = sql, args
        return []

    async def fetchval(self, sql, *args):
        return True


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


def test_filter_sql_and_binding_before_limit(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(docs, "get_pool", lambda: Pool(conn))
    asyncio.run(docs.list_documents("aads", q=None, kind=None, approved_only=False,
                                    approval_required=True, limit=5, context=CTX))
    sql = conn.sql
    assert "NOT $7::bool OR " + docs.APPROVAL_REQUIRED_SQL in sql
    assert "IS DISTINCT FROM h.latest_revision_id" in sql
    assert "EXISTS(SELECT 1 FROM project_document_goal_links" in docs.APPROVAL_REQUIRED_SQL
    assert sql.index("$7::bool OR") < sql.index("LIMIT $6")  # WHERE 가 LIMIT 보다 앞
    assert conn.args == (TENANT, "AADS", None, None, False, 5, True)
    assert "AS approval_required" in sql
    assert "COALESCE(" + docs.APPROVAL_REQUIRED_SQL + ", false) AS approval_required" in sql


def test_conflict_with_approved_only():
    with pytest.raises(HTTPException) as e:
        asyncio.run(docs.list_documents("aads", q=None, kind=None, approved_only=True,
                                        approval_required=True, limit=5, context=CTX))
    assert e.value.status_code == 400
    assert e.value.detail == "approval_required_conflicts_approved_only"
