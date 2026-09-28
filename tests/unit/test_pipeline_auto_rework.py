"""AADS-RUNNER-AUTO-REWORK — 리뷰 반려 뒤 자동 재작업 제출."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.services import pipeline_auto_rework as ar


ORIGINAL = (
    "TASK_ID: AADS-X-20260929\nTITLE: t\nPRIORITY: P1-HIGH\nSIZE: S\n"
    "TARGET: /root/aads/aads-server\nGOAL_ID: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240\n\n본문\n"
)


def test_only_ai_request_changes_triggers():
    assert ar.is_request_changes_failure("error", "review_failed", "review_failed: verdict=REQUEST_CHANGES score=0.68")
    # 사람(원 세션) 판정 반려, 인프라 보류, 다른 실패는 재작업하지 않는다
    assert not ar.is_request_changes_failure("error", "review_failed", "review_failed: source=origin_session_adjudicator")
    assert not ar.is_request_changes_failure("review_hold", "review_hold", "review_infra_failed: verdict=FLAG")
    assert not ar.is_request_changes_failure("error", "error", "invalid_aads_target")
    assert not ar.is_request_changes_failure("error", "review_failed", "review_failed: verdict=FLAG score=0.1")


def test_max_rounds_follows_max_cycles_with_cap():
    assert ar.max_rounds(3) == 2
    assert ar.max_rounds(None) == 2
    assert ar.max_rounds(1) == 0
    assert ar.max_rounds(10) == 3
    assert ar.max_rounds("x") == 2


def test_extract_issues_from_json_and_fallback():
    fb = json.dumps({"issues": ["a", " ", "b"], "summary": "s"})
    assert ar.extract_issues(fb) == ["a", "b"]
    assert ar.extract_issues({"issues": [], "summary": "요약"}) == ["요약"]
    assert ar.extract_issues("평문 피드백") == ["평문 피드백"]
    assert ar.extract_issues(None) == []
    assert len(ar.extract_issues({"issues": ["x" * 2000] * 20})) == 8


def test_build_keeps_original_and_target_single_row():
    out = ar.build_rework_instruction(
        original=ORIGINAL, parent_job_id="runner-aaaaaaaa", round_no=1, rounds_max=2,
        issues=["지적1", "지적2"], commit_hash="0123456789abcdef", score=0.68,
    )
    assert out.startswith("ALLOW_DUP_JOB\nAUTO_REWORK_OF: runner-aaaaaaaa\nAUTO_REWORK_ROUND: 1/2\n")
    assert ORIGINAL.rstrip() in out
    assert "1. 지적1\n2. 지적2" in out
    assert "/tmp/aads-wt-runner-aaaaaaaa" in out and "0123456789ab" in out
    # 러너 TARGET 검증은 TARGET 행이 2개 이상이면 fail-closed — 1개로 유지돼야 한다
    assert sum(1 for line in out.split("\n") if line.strip().startswith("TARGET")) == 1
    assert ar.current_round(out) == 1


def test_second_round_does_not_stack_previous_section():
    r1 = ar.build_rework_instruction(
        original="ALLOW_DUP_JOB SUPERSEDES: runner-bbbbbbbb\n" + ORIGINAL,
        parent_job_id="runner-aaaaaaaa", round_no=1, rounds_max=2, issues=["옛 지적"],
    )
    r2 = ar.build_rework_instruction(
        original=r1, parent_job_id="runner-cccccccc", round_no=2, rounds_max=2, issues=["새 지적"],
    )
    assert "옛 지적" not in r2
    assert r2.count("AUTO_REWORK_OF:") == 1 and "AUTO_REWORK_OF: runner-cccccccc" in r2
    assert r2.count(ar._SECTION_MARK) == 1
    assert ar.current_round(r2) == 2
    assert r2.split("\n")[3].startswith("TASK_ID:")


class FakeConn:
    def __init__(self, row, existing=None, feedback=None):
        self.row = row
        self.existing = existing
        self.feedback = feedback
        self.executed: list[tuple] = []

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetchval(self, sql, *args):
        if "AUTO_REWORK_OF" in (args[0] if args else "") or "instruction LIKE" in sql:
            return self.existing
        if "code_reviews" in sql:
            return self.feedback
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    @asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()


def _row(**kw):
    base = dict(
        job_id="runner-aaaaaaaa", project="AADS", instruction=ORIGINAL,
        chat_session_id="1fa84036-b12d-4497-97f5-076a32645a20", status="error",
        phase="review_failed", error_detail="review_failed: verdict=REQUEST_CHANGES score=0.68",
        max_cycles=3, model="m", size="S", worker_model=None, model_override_reason=None,
        parallel_group=None, tenant_id="00000000-0000-0000-0000-000000000001",
        commit_hash="abc", review_score=0.68, goal_id=None, milestone_id=None,
    )
    base.update(kw)
    return base


def _inserts(conn):
    return [a for s, a in conn.executed if "INSERT INTO pipeline_jobs" in s]


def test_submits_one_rework_with_issues(monkeypatch):
    monkeypatch.delenv("PIPELINE_AUTO_REWORK", raising=False)
    conn = FakeConn(_row(), feedback=json.dumps({"issues": ["결함A"]}))
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res and res["round"] == 1 and res["rounds_max"] == 2
    ins = _inserts(conn)
    assert len(ins) == 1
    new_instruction = ins[0][2]
    assert "결함A" in new_instruction and "AUTO_REWORK_OF: runner-aaaaaaaa" in new_instruction
    assert any("pg_notify" in s for s, _ in conn.executed)
    assert any("INSERT INTO chat_messages" in s for s, _ in conn.executed)


def test_duplicate_notify_does_not_submit_twice():
    conn = FakeConn(_row(), existing="runner-dddddddd")
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res["skipped"] == "already_submitted"
    assert _inserts(conn) == []


def test_cap_reached_stops():
    r2 = ar.build_rework_instruction(
        original=ORIGINAL, parent_job_id="runner-bbbbbbbb", round_no=2, rounds_max=2, issues=["x"],
    )
    conn = FakeConn(_row(instruction=r2))
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res["skipped"] == "cap_reached"
    assert _inserts(conn) == []


@pytest.mark.parametrize("row_kw,env", [
    ({"instruction": ORIGINAL + "NO_AUTO_REWORK\n"}, None),
    ({"error_detail": "review_failed: source=origin_session_adjudicator"}, None),
    ({}, "0"),
])
def test_opt_out_paths(monkeypatch, row_kw, env):
    if env is not None:
        monkeypatch.setenv("PIPELINE_AUTO_REWORK", env)
    else:
        monkeypatch.delenv("PIPELINE_AUTO_REWORK", raising=False)
    conn = FakeConn(_row(**row_kw))
    asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert _inserts(conn) == []


def test_notify_hook_is_wired_before_terminal_suppression():
    src = open("app/api/pipeline_runner.py", encoding="utf-8").read()
    hook = src.index("maybe_submit_auto_rework(conn, job_id)")
    suppress = src.index('logger.info("pipeline_runner.notify_terminal_suppressed"')
    assert hook < suppress
