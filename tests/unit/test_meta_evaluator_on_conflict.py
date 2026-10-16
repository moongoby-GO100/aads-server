"""auto_tune_memory 의 ai_meta_memory INSERT 가 실제 유일 제약 (project, category, key) 을 대상으로 하는지 고정."""

import asyncio
from unittest.mock import patch

import pytest

from app.memory import meta_evaluator


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.executed = []

    def transaction(self):
        return _Tx()

    async def fetch(self, *a, **k):
        return []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 0"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


_EVAL = {
    "utilization_rate": 0.9,
    "re_correction_count": 0,
    "total_memories": 10,
    "active_memories": 9,
}


def _run(project):
    conn = _FakeConn()

    async def _eval(_project=None):
        return dict(_EVAL)

    with patch.object(meta_evaluator, "_get_pool", return_value=_FakePool(conn)), \
         patch.object(meta_evaluator, "evaluate_memory_effectiveness", _eval):
        asyncio.run(meta_evaluator.auto_tune_memory(project))
    inserts = [e for e in conn.executed if "INSERT INTO ai_meta_memory" in e[0]]
    assert len(inserts) == 1
    return inserts[0]


@pytest.mark.parametrize("project,expected", [(None, ""), ("AADS", "AADS")])
def test_insert_targets_project_category_key_constraint(project, expected):
    sql, args = _run(project)
    assert "ON CONFLICT (project, category, key)" in sql
    assert "ON CONFLICT (key)" not in sql
    assert args[0] == expected
    assert args[1] == f"meta_eval_{project.lower() if project else 'global'}"
