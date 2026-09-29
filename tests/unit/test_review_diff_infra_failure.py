"""git diff 캡쳐 실패가 score 0.000 품질반려로 집계되지 않는지 검증.

2026-09-29 실측: GO100 chat-direct 리뷰 8건이 GIT_DIFF_FAILURE/score=0.0 FLAG 로
code_reviews 에 쌓였다. 비-AADS 분기가 실패 텍스트를 `[ERROR]` 없이 돌려준 탓이다.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from app.services import tool_executor as te_module  # noqa: E402
from app.services.tool_executor import ToolExecutor  # noqa: E402

_GOOD_DIFF = (
    "diff --git a/app/x.py b/app/x.py\n"
    "--- a/app/x.py\n"
    "+++ b/app/x.py\n"
    "@@ -1 +1 @@\n"
    "-a = 1\n"
    "+a = 2"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(te_module.asyncio, "sleep", sleep)
    return sleep


def _patch_remote(side_effect):
    mock = AsyncMock(side_effect=side_effect)
    return mock, patch("app.api.ceo_chat_tools.tool_run_remote_command", mock)


# ─── 1) 비-AADS 실패는 반드시 [ERROR] ────────────────────────────────────────

@pytest.mark.parametrize("failure", [
    "fatal: not a git repository (or any of the parent directories): .git",
    "[stderr] warning: could not open directory 'x/': Permission denied",
    "command timed out after 30s",
])
async def test_go100_capture_failure_returns_error_prefix(failure):
    mock, ctx = _patch_remote([failure, failure])
    with ctx:
        result = await ToolExecutor()._capture_review_diff("GO100", "kis-autotrade-v4", "app/x.py")
    assert result.startswith("[ERROR] AI review git diff capture failed:")
    assert failure[:50] in result
    assert mock.await_count == 2


async def test_go100_capture_failure_detail_is_bounded():
    failure = "fatal: " + "x" * 5000
    _, ctx = _patch_remote([failure, failure])
    with ctx:
        result = await ToolExecutor()._capture_review_diff("GO100", "kis-autotrade-v4", "app/x.py")
    prefix = "[ERROR] AI review git diff capture failed: "
    assert result.startswith(prefix)
    assert len(result) - len(prefix) <= 1000


# ─── 2) 재시도는 1회만 ──────────────────────────────────────────────────────

async def test_capture_retry_once_then_success(_no_sleep):
    mock, ctx = _patch_remote(["fatal: index.lock exists", _GOOD_DIFF])
    with ctx:
        result = await ToolExecutor()._capture_review_diff("GO100", "kis-autotrade-v4", "app/x.py")
    assert result == ToolExecutor._strip_remote_command_wrapper(_GOOD_DIFF)
    assert result.startswith("diff --git")
    assert mock.await_count == 2
    _no_sleep.assert_awaited_once()
    assert _no_sleep.await_args.args[0] <= 2


async def test_capture_two_failures_returns_error_and_no_third_call():
    mock, ctx = _patch_remote([
        RuntimeError("ssh reset"),
        "fatal: not a git repository",
        _GOOD_DIFF,  # 3번째 호출이 일어나면 성공으로 보여 테스트가 실패한다
    ])
    with ctx:
        result = await ToolExecutor()._capture_review_diff("GO100", "kis-autotrade-v4", "app/x.py")
    assert result.startswith("[ERROR]")
    assert mock.await_count == 2


async def test_capture_success_first_try_no_retry(_no_sleep):
    mock, ctx = _patch_remote([_GOOD_DIFF])
    with ctx:
        result = await ToolExecutor()._capture_review_diff("GO100", "kis-autotrade-v4", "app/x.py")
    assert result.startswith("diff --git")
    assert mock.await_count == 1
    _no_sleep.assert_not_awaited()


async def test_capture_clean_tree_is_not_retried(_no_sleep):
    """변경 없음(헤더만 있는 정상 결과)은 실패가 아니다 — 재시도 없이 빈 diff."""
    mock, ctx = _patch_remote(["[GO100] $ git diff -- app/x.py"])
    with ctx:
        result = await ToolExecutor()._capture_review_diff("GO100", "kis-autotrade-v4", "app/x.py")
    assert result == ""
    assert mock.await_count == 1
    _no_sleep.assert_not_awaited()


# ─── 3) 호출자: 캡쳐 실패면 리뷰·채팅 경고 없음 ───────────────────────────────

async def test_go100_review_skips_on_capture_failure(caplog):
    executor = ToolExecutor()
    review = AsyncMock()
    get_pool = AsyncMock()
    executor._capture_review_diff = AsyncMock(
        return_value="[ERROR] AI review git diff capture failed: fatal: not a git repository"
    )
    with patch("app.services.code_reviewer.review_code_diff", review), \
            patch.dict(sys.modules, {"app.services.db": MagicMock(get_db_pool=get_pool)}), \
            caplog.at_level("WARNING", logger=te_module.logger.name):
        await executor._run_ai_code_review_go100("app/x.py", "summary", "2026-09-29 16:30")
    review.assert_not_awaited()
    get_pool.assert_not_awaited()
    assert any("ai_review_git_diff_infra_failure" in r.getMessage() for r in caplog.records)


async def test_aads_review_skips_on_capture_failure(caplog):
    executor = ToolExecutor()
    review = AsyncMock()
    get_pool = MagicMock()
    executor._capture_review_diff = AsyncMock(
        return_value="[ERROR] AI review git diff capture failed: timed out"
    )
    with patch("app.services.code_reviewer.review_code_diff", review), \
            patch("app.core.db_pool.get_pool", get_pool, create=True), \
            caplog.at_level("WARNING", logger=te_module.logger.name):
        await executor._run_ai_code_review_after_commit("AADS", "app/x.py", "summary", "now")
    review.assert_not_awaited()
    get_pool.assert_not_called()
    assert any("ai_review_git_diff_infra_failure" in r.getMessage() for r in caplog.records)


# ─── 2차 방어선: precheck 가 infra_failure 표시 ─────────────────────────────

def test_precheck_marks_git_diff_failure_as_infra():
    from app.services.code_reviewer import _precheck_review_input
    v = _precheck_review_input("fatal: not a git repository")
    assert v is not None
    # 기존 계약 유지
    assert v.verdict == "FLAG" and v.score == 0.0
    assert v.flag_category == "GIT_DIFF_FAILURE"
    assert v.failure_stage == "git_diff_capture"
    assert v.needs_retry is True
    # 추가 표시
    assert v.feedback.get("infra_failure") is True


def test_precheck_invalid_input_is_not_infra():
    from app.services.code_reviewer import _precheck_review_input
    v = _precheck_review_input("just some prose, not a diff")
    assert v is not None
    assert v.flag_category == "INVALID_REVIEW_INPUT"
    assert "infra_failure" not in v.feedback


# ─── 4) ops QA 패널 분류 ────────────────────────────────────────────────────

def test_qa_result_items_infra_vs_quality():
    from app.api.ops import _qa_result_items
    rows = [
        {"job_id": "infra", "project": "GO100", "verdict": "FLAG", "score": 0.0,
         "review_cycle": 1, "needs_retry": True, "flag_category": "GIT_DIFF_FAILURE",
         "feedback": {"summary": "git diff 수집 실패"}},
        {"job_id": "marked", "project": "GO100", "verdict": "FLAG", "score": 0.0,
         "review_cycle": 1, "needs_retry": False, "flag_category": None,
         "feedback": '{"infra_failure": true}'},
        {"job_id": "rc", "project": "AADS", "verdict": "REQUEST_CHANGES", "score": 0.4,
         "review_cycle": 1, "needs_retry": False, "flag_category": None,
         "feedback": {"issues": ["로직 오류"]}},
        {"job_id": "ok", "project": "AADS", "verdict": "APPROVE", "score": 0.9,
         "review_cycle": 1, "needs_retry": False, "flag_category": None,
         "feedback": {"issues": []}},
        {"job_id": "no-retry", "project": "GO100", "verdict": "FLAG", "score": 0.0,
         "review_cycle": 1, "needs_retry": False, "flag_category": "GIT_DIFF_FAILURE",
         "feedback": {}},
    ]
    infra, marked, rc, ok, no_retry = _qa_result_items(rows)
    assert infra["verdict"] == "INFRA" and infra["infra_failure"] is True
    assert infra["raw_verdict"] == "FLAG"            # 원본 판정은 그대로 노출
    assert marked["verdict"] == "INFRA" and marked["infra_failure"] is True
    assert rc["verdict"] == "FAIL" and rc["infra_failure"] is False
    assert ok["verdict"] == "PASS" and ok["infra_failure"] is False
    assert no_retry["verdict"] == "FAIL"             # needs_retry 없으면 기존 분류
