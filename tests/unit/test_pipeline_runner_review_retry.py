"""P0: review_hold 무한 대기 방지 — AI 검수 DELEGATED(파서/인프라 실패) 재시도 로직 검증."""
import asyncio

import pytest

from app.services.pipeline_runner_service import PipelineCJob, _MAX_REVIEW_PARSE_RETRIES


def _make_job() -> PipelineCJob:
    return PipelineCJob(
        project="AADS",
        instruction="테스트 지시",
        chat_session_id="",
    )


def test_max_review_parse_retries_is_three():
    assert _MAX_REVIEW_PARSE_RETRIES == 3


def test_ai_review_with_retry_retries_on_delegated_then_recovers():
    job = _make_job()
    calls = {"n": 0}

    async def _fake_ai_review():
        calls["n"] += 1
        if calls["n"] < 3:
            return {"verdict": "DELEGATED", "summary": "LLM 호출 실패", "feedback": "", "parse_error": "boom"}
        return {"verdict": "PASS", "summary": "정상 통과", "feedback": ""}

    job._ai_review = _fake_ai_review

    async def _run():
        # 재시도 사이 sleep으로 테스트가 느려지지 않도록 패치
        orig_sleep = asyncio.sleep
        asyncio.sleep = lambda *_a, **_k: orig_sleep(0)
        try:
            return await job._ai_review_with_retry()
        finally:
            asyncio.sleep = orig_sleep

    review = asyncio.run(_run())

    assert calls["n"] == 3
    assert review["verdict"] == "PASS"


def test_ai_review_with_retry_gives_up_after_max_attempts():
    job = _make_job()
    calls = {"n": 0}

    async def _fake_ai_review():
        calls["n"] += 1
        return {"verdict": "DELEGATED", "summary": "LLM 검수 호출 실패", "feedback": "", "parse_error": "boom"}

    job._ai_review = _fake_ai_review

    async def _run():
        orig_sleep = asyncio.sleep
        asyncio.sleep = lambda *_a, **_k: orig_sleep(0)
        try:
            return await job._ai_review_with_retry()
        finally:
            asyncio.sleep = orig_sleep

    review = asyncio.run(_run())

    # 첫 실패에 즉시 포기하지 않고 정확히 _MAX_REVIEW_PARSE_RETRIES회 시도해야 한다.
    assert calls["n"] == _MAX_REVIEW_PARSE_RETRIES
    assert review["verdict"] == "DELEGATED"


def test_ai_review_with_retry_returns_immediately_on_first_pass():
    job = _make_job()
    calls = {"n": 0}

    async def _fake_ai_review():
        calls["n"] += 1
        return {"verdict": "FAIL", "summary": "수정 필요", "feedback": "버그 있음"}

    job._ai_review = _fake_ai_review

    review = asyncio.run(job._ai_review_with_retry())

    assert calls["n"] == 1
    assert review["verdict"] == "FAIL"


def test_flag_review_hold_sets_parser_failure_category(monkeypatch):
    job = _make_job()
    executed = {}

    class _FakeConn:
        async def execute(self, query, *args):
            executed["query"] = query
            executed["args"] = args

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakePool:
        def acquire(self):
            return _FakeConn()

    monkeypatch.setattr(
        "app.core.db_pool.get_pool", lambda: _FakePool()
    )

    asyncio.run(job._flag_review_hold({"summary": "REVIEW_PARSER_FAILURE"}))

    assert "review_flag_category = 'REVIEW_PARSER_FAILURE'" in executed["query"]
    assert "review_needs_retry = TRUE" in executed["query"]
    assert executed["args"][0] == job.job_id
