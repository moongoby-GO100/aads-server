import json

import pytest


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _MetaPool:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.store = {}

    def acquire(self):
        return _Acquire(self)

    async def fetchrow(self, *_args):
        if len(_args) >= 3:
            key = (_args[1], "correction_directive", _args[2])
            value = self.store.get(key)
            if value is not None:
                return {"value": value}
        return None

    async def fetch(self, *_args, **_kwargs):
        return self.rows

    async def execute(self, _sql, project, key, value):
        self.store[(project, "correction_directive", key)] = json.loads(value)
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_self_eval_reflexion_records_fail_then_success_counts():
    from app.services import self_evaluator

    pool = _MetaPool()

    failed = await self_evaluator._record_self_refine_outcome(
        pool=pool,
        project="AADS",
        query="구체적 수치와 근거를 알려줘",
        response="정보가 없습니다.",
        score=0.32,
    )

    assert failed["failure_type"] == "정보_부족"
    assert failed["fail_count"] == 1
    assert failed["success_count"] == 0
    assert failed["last_outcome"] == "fail"
    assert failed["improvement_hint"]

    recovered = await self_evaluator._record_self_refine_outcome(
        pool=pool,
        project="AADS",
        query="구체적 수치와 근거를 알려줘",
        response="관련 데이터를 확인했고 수치와 근거를 함께 정리했습니다.",
        score=0.82,
    )

    assert recovered["fail_count"] == 1
    assert recovered["success_count"] == 1
    assert recovered["last_outcome"] == "success"


@pytest.mark.asyncio
async def test_memory_recall_correction_directives_include_counts_hint_and_recent_success(monkeypatch):
    from app.core import memory_recall

    rows = [
        {
            "key": "reflexion:AADS:정보_부족",
            "value": {
                "directive": "응답 전 반드시 관련 도구/DB를 조회하여 구체적 정보를 확보하라.",
                "failure_type": "정보_부족",
                "fail_count": 12,
                "success_count": 3,
                "last_outcome": "fail",
                "directive_strength": "critical",
                "improvement_hint": "사실 주장 전 도구/DB 근거를 확인하라.",
            },
        },
        {
            "key": "reflexion:AADS:형식_부적합",
            "value": {
                "directive": "CEO 요청 형식을 정확히 파악하고 그 형식으로만 응답하라.",
                "failure_type": "형식_부적합",
                "fail_count": 2,
                "success_count": 2,
                "last_outcome": "success",
                "directive_strength": "relaxed",
                "improvement_hint": "요청 형식을 먼저 확인하라.",
            },
        },
    ]
    monkeypatch.setattr(memory_recall, "_get_pool", lambda: _MetaPool(rows))

    text = await memory_recall._build_correction_directives("AADS")

    assert "[정보_부족 실패 12회/성공 3회]" in text
    assert "개선힌트: 사실 주장 전 도구/DB 근거를 확인하라." in text
    assert "[형식_부적합 실패 2회/성공 2회, 최근 성공]" in text
    assert "유지점검:" in text
