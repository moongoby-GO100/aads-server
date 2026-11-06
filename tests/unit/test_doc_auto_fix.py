"""정본 문서 보류 자동 보완 순환: 모드, 계획(none/fix/escalate), 멱등, 마커, 피드백, 후크, 재검수 프롬프트, needs_ceo."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import canonical_review_digest as digest
from app.services import doc_auto_approval as auto
from app.services import doc_auto_fix as fix
from app.services import doc_cross_review as cross
from app.services import doc_review_rubric as rubric
from app.services import runner_job_submit as submit
from app.services.doc_auto_approval import Decision

TENANT = "00000000-0000-0000-0000-000000000001"
REV = "22222222-2222-2222-2222-222222222222"
PREV = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=fix.KST)


def issue(sev="major", criterion="completeness", problem="완료기준이 없습니다", location="완료기준", fixt="수치를 적으세요"):
    return {"severity": sev, "criterion": criterion, "location": location, "problem": problem, "fix": fixt}


def review(issues=(), model="claude-x", provider="anthropic", verdict="reject"):
    data = {"verdict": verdict, "summary": "s", "ceo_brief": "쉬운 설명입니다.", "score": 0.6, "scores": {"a": 0.6},
            "issues": list(issues)}
    return {"reviewer_model": model, "reviewer_provider": provider, "verdict": verdict,
            "reason": "요약\n" + rubric.REVIEW_MARKER + json.dumps(data), "cross_company": True, "created_at": NOW}


HEAD = {"id": 1, "project_key": "AADS", "document_key": "doc-a", "kind": "prd", "generation": 2}
REVISION = {"id": REV, "title": "일반 문서", "version": "1.0.0", "content": "# 본문", "doc_tier": "D2",
            "source_session_id": TENANT}


def hold(reasons=("major_present",), risk="normal"):
    return Decision("hold", risk, list(reasons), 0.6)


def plan(decision=None, reviews=None, history=None, head=HEAD, revision=REVISION):
    return fix.plan_auto_fix(decision or hold(), head, revision,
                             [review([issue()])] if reviews is None else reviews, history)


# ---------------------------------------------------------------- 모드·상수
def test_mode_defaults_and_constants(monkeypatch):
    monkeypatch.delenv("DOC_AUTO_FIX", raising=False)
    assert fix.auto_fix_mode() == "on"
    monkeypatch.setenv("DOC_AUTO_FIX", "garbage")
    assert fix.auto_fix_mode() == "on"
    monkeypatch.setenv("DOC_AUTO_FIX", " OFF ")
    assert fix.auto_fix_mode() == "off"
    assert fix.DOC_AUTO_FIX_MAX_ROUNDS == 2 and fix.DOC_AUTO_FIX_DAILY_LIMIT == 10


# ---------------------------------------------------------------- 계획
def test_none_cases():
    assert plan(Decision("approve")).action == "none"
    assert plan(Decision("skip", reasons=["kind_report"])).action == "none"
    assert plan(hold(["needs_second_company", "legacy_review_no_score"])).action == "none"
    assert plan(hold(["review_state_pending", "needs_second_company"])).action == "none"


def test_fix_for_ordinary_hold_and_rejected():
    p = plan(hold(["review_state_rejected", "reject_present", "blocker_present"]),
             [review([issue("blocker", problem="롤백 절차 없음")])])
    assert p.action == "fix" and p.round == 1 and len(p.fingerprint) == 16
    assert "롤백 절차 없음" in p.feedback and "blocker" in p.feedback


@pytest.mark.parametrize("decision,head,revision,expect", [
    (hold(risk="high"), HEAD, REVISION, "risk_high"),
    (Decision("skip", reasons=["migration_document"]), HEAD, REVISION, "migration_document"),
    (hold(), {**HEAD, "document_key": "migration-apply-001"}, REVISION, "migration_document"),
    (hold(), {**HEAD, "kind": "contract"}, {**REVISION, "title": "마이그레이션 계약"}, "migration_document"),
    (hold(["checks_failed:secret_patterns"]), HEAD, REVISION, "secret_detected"),
])
def test_escalate_cases(decision, head, revision, expect):
    p = plan(decision, head=head, revision=revision)
    assert p.action == "escalate" and expect in p.reasons


def test_escalate_on_rounds_exhausted_same_fp_and_daily():
    fp = plan().fingerprint
    done = lambda n, f: {"round": n, "fingerprint": f, "revision_id": f"r{n}", "active": False}  # noqa: E731
    assert plan(history={"rounds": [done(1, "a" * 16)], "daily": 0}).action == "fix"
    two = plan(history={"rounds": [done(1, "a" * 16), done(2, "b" * 16)], "daily": 0})
    assert two.action == "escalate" and "rounds_exhausted" in two.reasons
    same = plan(history={"rounds": [done(1, fp)], "daily": 0})
    assert same.action == "escalate" and "same_findings" in same.reasons
    daily = plan(history={"rounds": [], "daily": 10})
    assert daily.action == "escalate" and "daily_limit" in daily.reasons
    assert plan(history={"rounds": [], "daily": 9}).action == "fix"


def test_fingerprint_stable_and_ignores_minor_and_non_dict():
    a = [review([issue(), issue("minor", problem="사소")])]
    b = [review([issue(problem="  완료기준이   없습니다!! "), "문자열", None])]
    assert fix.fingerprint_of(fix.extract_findings(a)) == fix.fingerprint_of(fix.extract_findings(b))
    c = [review([issue(problem="다른 문제")])]
    assert fix.fingerprint_of(fix.extract_findings(a)) != fix.fingerprint_of(fix.extract_findings(c))


def test_feedback_sanitizes_secret_limits_and_keeps_blockers():
    secret = "sk-ant-oat01-" + "A" * 40
    many = [issue("minor", problem=f"사소 {i} " + "가" * 140) for i in range(40)]
    many += [issue("major", problem=f"중요 {i} " + "나" * 240) for i in range(20)]
    blockers = [issue("blocker", problem=f"차단 {i} {secret}") for i in range(3)]
    text = fix.build_feedback(fix.extract_findings([review(many + blockers)]), ["checks_failed:a_check,b_check"])
    assert len(text) <= 2000
    assert secret not in text and "A" * 40 not in text
    assert text.startswith("실패한 자동 검사: a_check, b_check")
    for i in range(3):
        assert f"차단 {i}" in text
    assert text.index("[blocker]") < text.index("[major]")
    assert "위치" not in text and " / " in text


def test_feedback_many_blockers_all_kept_within_limit():
    findings = fix.extract_findings([review([issue("blocker", problem=f"차단{i} " + "x" * 500) for i in range(15)])])
    text = fix.build_feedback(findings, [])
    assert len(text) <= 2000 and all(f"차단{i} " in text for i in range(15))


# ---------------------------------------------------------------- 마커·제출
def test_marker_format_roundtrip():
    line = fix.marker_line("doc-a", REV, 1, "0123456789abcdef")
    assert line == f"DOC_AUTO_FIX_OF: doc-a rev={REV} round=1/2 fp=0123456789abcdef"
    assert fix.parse_markers("x\n" + line + "\n")[0]["revision_id"] == REV


def test_instruction_contents():
    p = plan()
    text = fix.build_instruction("AADS", HEAD, REVISION, p)
    assert text.startswith("PUSH_ONLY:true") and "<feedback>" in text and "source_task_id" in text
    assert "document_key: doc-a" in text and f"revision_id: {REV}" in text and "version: 1.0.0" in text
    assert "삭제" in text and "축소" in text and "완화" in text and "archive" in text
    assert fix.marker_line("doc-a", REV, 1, p.fingerprint) in text


def test_daily_pattern_counts_real_instruction_not_dev_examples():
    import re
    fix_pat = re.compile(fix.DAILY_FIX_MARKER_PATTERN)
    resolve_pat = re.compile(fix.DAILY_RESOLVE_MARKER_PATTERN)
    p = plan()
    assert fix_pat.search(fix.build_instruction("AADS", HEAD, REVISION, p))
    assert resolve_pat.search("x\n" + f"{fix.RESOLVE_MARKER_PREFIX}doc-a rev={REV} path=investigate" + "\n")
    dev = ("TASK_ID: dev\n마커 예시 DOC_AUTO_FIX_OF: <key> rev=<id> round=1/2 fp=0123456789abcdef 와 "
           "DOC_AUTO_RESOLVE_OF: <key> rev=<id> path=investigate. 를 본문에서 설명한다.\n"
           "  - DOC_AUTO_FIX_OF: doc-a rev=abc round=1/2 fp=0123456789abcdef\n")
    assert fix_pat.search(dev) is None and resolve_pat.search(dev) is None


def test_load_history_daily_query_passes_regex_patterns():
    seen = {}

    class C:
        async def fetch(self, sql, *a):
            return []

        async def fetchval(self, sql, *a):
            seen["sql"], seen["args"] = sql, a
            return 4
    out = asyncio.run(fix.load_history(C(), TENANT, "AADS", "doc-a", now=NOW))
    assert out["daily"] == 4 and seen["sql"] == fix._DAILY_SQL and "strpos" not in seen["sql"]
    assert seen["args"][1] == fix.DAILY_FIX_MARKER_PATTERN and seen["args"][3] == fix.DAILY_RESOLVE_MARKER_PATTERN


class FakeConn:
    def __init__(self, jobs=(), daily=0):
        self.jobs, self.daily, self.writes = list(jobs), daily, []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        return self.jobs

    async def fetchval(self, sql, *args):
        return self.daily

    async def execute(self, sql, *args):
        self.writes.append((sql, args))


def job(rev, rnd=1, fp="a" * 16, status="succeeded", job_id="runner-old"):
    return {"job_id": job_id, "status": status, "created_at": NOW,
            "instruction": "x\n" + fix.marker_line("doc-a", rev, rnd, fp)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_APPROVE", "on")
    monkeypatch.setenv("DOC_AUTO_FIX", "on")
    calls = []

    async def fake_submit(conn, **kw):
        calls.append(kw)
        return {"job_id": "runner-new", "status": "queued", "idempotent": False}
    monkeypatch.setattr(submit, "submit_runner_job_internal", fake_submit)

    def set_ctx(decision=None, rows=None):
        async def fake_assess(*a, **k):
            return {"head": HEAD, "revision": REVISION, "rows": [review([issue()])] if rows is None else rows,
                    "decision": decision or hold(), "summary": {}, "checks": []}
        monkeypatch.setattr(auto, "assess", fake_assess)
    set_ctx()
    return SimpleNamespace(calls=calls, set_ctx=set_ctx)


def run(conn, decision=None):
    return asyncio.run(fix.maybe_schedule_auto_fix(conn, TENANT, "AADS", 1, REV, decision or hold(), now=NOW))


def test_fix_submits_once_internal_origin(env):
    out = run(FakeConn())
    assert out["action"] == "fix" and out["job_id"] == "runner-new"
    assert len(env.calls) == 1 and env.calls[0]["origin"] == "doc_auto_fix" and env.calls[0]["tenant_id"] == TENANT
    assert "DOC_AUTO_FIX_OF: doc-a rev=" + REV + " round=1/2 fp=" in env.calls[0]["instruction"]


def test_same_revision_is_idempotent(env):
    out = run(FakeConn([job(REV)]))
    assert out["job_id"] == "runner-old" and out["idempotent"] is True and env.calls == []


def test_active_job_for_same_key_blocks_submission(env):
    out = run(FakeConn([job(PREV, status="running", job_id="runner-busy")]))
    assert out["action"] == "none" and out["job_id"] == "runner-busy" and env.calls == []


def test_escalate_writes_nothing_and_logs(env, caplog):
    env.set_ctx(hold(risk="high"))
    with caplog.at_level(logging.WARNING):
        out = run(FakeConn())
    assert out["action"] == "escalate" and env.calls == []
    assert "doc_auto_fix_escalated" in caplog.text and "doc_auto_fix_decision" in caplog.text
    assert "risk_high" in caplog.text


def test_exhausted_two_rounds_escalates_without_submit(env):
    out = run(FakeConn([job(PREV, 1, "a" * 16), job("r2", 2, "b" * 16)]))
    assert out["action"] == "escalate" and "rounds_exhausted" in out["reasons"] and env.calls == []


def test_daily_limit_escalates(env):
    out = run(FakeConn(daily=10))
    assert out["action"] == "escalate" and "daily_limit" in out["reasons"] and env.calls == []


@pytest.mark.parametrize("fix_mode,approve_mode", [("off", "on"), ("on", "shadow"), ("on", "off")])
def test_shadow_and_off_never_submit(env, monkeypatch, fix_mode, approve_mode):
    monkeypatch.setenv("DOC_AUTO_FIX", fix_mode)
    monkeypatch.setenv("DOC_AUTO_APPROVE", approve_mode)
    run(FakeConn())
    assert env.calls == []


def test_non_hold_decision_is_ignored(env):
    assert run(FakeConn(), Decision("approve")) is None and env.calls == []


# ---------------------------------------------------------------- 후크: 반려 상태도 plan 경로
def test_rejected_review_state_still_reaches_plan(monkeypatch):
    seen = []
    maybe = AsyncMock(return_value=hold(["review_state_rejected", "blocker_present"]))
    monkeypatch.setattr(auto, "maybe_auto_approve", maybe)
    followup = AsyncMock()
    monkeypatch.setattr(auto, "request_follow_up_review", followup)

    async def fake_schedule(*a, **k):
        seen.append(a)
    monkeypatch.setattr(fix, "maybe_schedule_auto_fix", fake_schedule)

    @asynccontextmanager
    async def acquire():
        yield object()
    monkeypatch.setattr(cross, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    asyncio.run(cross._auto_approve_after_review(
        TENANT, "AADS", {"id": 1}, {"id": REV}, {"review_state": "rejected"}, allow_follow_up=True))
    assert maybe.await_count == 1 and len(seen) == 1 and followup.await_count == 0


def test_auto_fix_exception_does_not_propagate(monkeypatch):
    monkeypatch.setattr(auto, "maybe_auto_approve", AsyncMock(return_value=hold()))
    monkeypatch.setattr(fix, "maybe_schedule_auto_fix", AsyncMock(side_effect=RuntimeError("boom")))

    @asynccontextmanager
    async def acquire():
        yield object()
    monkeypatch.setattr(cross, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    asyncio.run(cross._auto_approve_after_review(
        TENANT, "AADS", {"id": 1}, {"id": REV}, {"review_state": "passed"}, allow_follow_up=False))


# ---------------------------------------------------------------- 재검수 프롬프트
def test_review_prompt_includes_previous_findings():
    prior = [{"severity": "major", "criterion": "completeness", "location": "L1", "problem": "롤백 없음", "fix": "x"}]
    system, prompt = rubric.build_review_request("prd", "D2", "t", "c", [], [], prior_findings=prior)
    data = json.loads(prompt)
    assert data["previous_findings"][0]["problem"] == "롤백 없음"
    assert "하나씩" in system and "미해결" in system or "해결되지 않은" in system
    assert "지워서" in system and "blocker" in system
    s0, p0 = rubric.build_review_request("prd", "D2", "t", "c", [], [])
    assert "previous_findings" not in p0 and "previous_findings" not in s0


def test_previous_findings_from_marker(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_FIX", "on")

    class C:
        async def fetchval(self, sql, *a):
            return "x\n" + fix.marker_line("doc-a", PREV, 1, "a" * 16)

        async def fetch(self, sql, *a):
            assert a[0].hex == PREV.replace("-", "")
            return [review([issue("blocker", problem="B"), issue("major", problem="M"), issue("minor", problem="m")])]
    out = asyncio.run(fix.previous_findings(C(), TENANT, "AADS", HEAD, {**REVISION, "source_task_id": "runner-x"}))
    assert [f["severity"] for f in out] == ["blocker", "major"]
    assert asyncio.run(fix.previous_findings(C(), TENANT, "AADS", HEAD, REVISION)) == []
    monkeypatch.setenv("DOC_AUTO_FIX", "off")
    assert asyncio.run(fix.previous_findings(C(), TENANT, "AADS", HEAD, {**REVISION, "source_task_id": "r"})) == []


# ---------------------------------------------------------------- needs_ceo
def _needs(n, long=False):
    return [{"project_key": "AADS", "document_key": f"doc-{i}", "title": "제목" * (40 if long else 3), "risk": "normal",
             "round": 2, "max_rounds": 2, "reason": fix.ESCALATE_REASON_KO["rounds_exhausted"],
             "ceo_brief": "설명" * (200 if long else 5),
             "remaining_issues": [{"severity": "major", "location": "L", "problem": "문제" * (100 if long else 3)}] * 3,
             "decision_needed": fix.DECISION_REQUEST_KO} for i in range(n)]


def test_format_digest_puts_ceo_first_and_fits_limit():
    auto_data = {"mode": "on", "auto_approved": [], "held": [], "would_approve": [],
                 "needs_ceo": _needs(2), "auto_fixing": [{"document_key": "k"}] * 3, "truncated": {}}
    text = digest.format_digest([], auto_data)
    assert text.startswith("🚨 <b>대표님 판단 필요 2건</b>") and "자동 보완 중 3건" in text
    assert "결정하실 것" in text
    big = {**auto_data, "needs_ceo": _needs(30, long=True)}
    assert len(digest.format_digest([], big)) <= 3900
    assert digest.format_digest([], big).startswith("🚨 <b>대표님 판단 필요 30건</b>")
    assert not digest.auto_digest_is_empty(auto_data)


def test_digest_classifies_held_into_needs_ceo(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_FIX", "on")
    item = {"project_key": "AADS", "document_key": "doc-a", "title": "T", "risk": "high", "ceo_brief": "b"}
    result = {"needs_ceo": [], "auto_fixing": []}
    conn = FakeConn()
    asyncio.run(digest._classify_held(conn, result, item, TENANT, hold(risk="high"), HEAD, REVISION, [review([issue()])]))
    assert len(result["needs_ceo"]) == 1
    entry = result["needs_ceo"][0]
    assert entry["document_key"] == "doc-a" and "고위험" in entry["reason"] and entry["decision_needed"]
    assert len(entry["remaining_issues"]) <= 3
    asyncio.run(digest._classify_held(conn, result, item, TENANT, hold(), HEAD, REVISION, [review([issue()])]))
    assert len(result["auto_fixing"]) == 1
    monkeypatch.setenv("DOC_AUTO_FIX", "off")
    asyncio.run(digest._classify_held(conn, result, item, TENANT, hold(), HEAD, REVISION, [review([issue()])]))
    assert len(result["auto_fixing"]) == 1


# ---------------------------------------------------------------- runner-204be745 반려 지적 6건 회귀

def test_daily_limit_is_serialized_per_tenant_before_head_lock(env):
    # [P1] 하루 한도 조회·제출을 테넌트 advisory lock 으로 직렬화한다(head 잠금·조회보다 먼저).
    conn = FakeConn()
    order = []
    orig_fetch = conn.fetch

    async def fetch(sql, *a):
        order.append("fetch")
        return await orig_fetch(sql, *a)
    conn.fetch = fetch
    orig_exec = conn.execute

    async def execute(sql, *a):
        order.append("lock" if "pg_advisory_xact_lock" in sql else "exec")
        return await orig_exec(sql, *a)
    conn.execute = execute
    run(conn)
    locks = [args for sql, args in conn.writes if "pg_advisory_xact_lock" in sql]
    assert locks == [("doc-auto-fix-daily:" + TENANT,)]
    assert order[0] == "lock"


@pytest.mark.parametrize("fix_mode,approve_mode", [("off", "on"), ("on", "shadow")])
def test_no_tenant_lock_when_not_submitting(env, monkeypatch, fix_mode, approve_mode):
    monkeypatch.setenv("DOC_AUTO_FIX", fix_mode)
    monkeypatch.setenv("DOC_AUTO_APPROVE", approve_mode)
    conn = FakeConn()
    run(conn)
    assert conn.writes == [] and env.calls == []


def test_revision_made_by_running_fix_job_is_not_blocked(env):
    # [P1] 러너가 새 판본을 등록한 뒤 아직 running 이어도, 그 판본의 다음 회차 판정은 막히지 않는다.
    env.set_ctx(hold(["major_present"]), rows=[review([issue(problem="다른 지적")])])
    rev = {**REVISION, "source_task_id": "runner-busy"}

    async def fake_assess(*a, **k):
        return {"head": HEAD, "revision": rev, "rows": [review([issue(problem="다른 지적")])],
                "decision": hold(), "summary": {}, "checks": []}
    auto.assess, orig = fake_assess, auto.assess
    try:
        out = run(FakeConn([job(PREV, 1, "a" * 16, status="running", job_id="runner-busy")]))
    finally:
        auto.assess = orig
    assert out["action"] == "fix" and len(env.calls) == 1
    assert "round=2/2" in env.calls[0]["instruction"]


def test_failed_fix_job_without_new_revision_escalates(env, caplog):
    # [P1] 현재 판본에 대한 보완 작업이 새 판본 없이 실패하면 멈추지 않고 상신한다(제출 0회).
    with caplog.at_level(logging.WARNING):
        out = run(FakeConn([job(REV, 1, status="error", job_id="runner-failed")]))
    assert out["action"] == "escalate" and out["reasons"] == ["fix_job_failed"] and env.calls == []
    assert "doc_auto_fix_escalated" in caplog.text


def test_round_state_rules():
    hist = {"rounds": [{"revision_id": PREV, "job_id": "j1", "active": True, "status": "running"}]}
    assert fix.round_state(hist, {"id": REV})[0] == "busy"
    assert fix.round_state(hist, {"id": REV, "source_task_id": "j1"})[0] is None
    done = {"rounds": [{"revision_id": REV, "job_id": "j2", "active": False, "status": "done"}]}
    assert fix.round_state(done, {"id": REV})[0] == "submitted"
    failed = {"rounds": [{"revision_id": REV, "job_id": "j3", "active": False, "status": "cancelled"}]}
    assert fix.round_state(failed, {"id": REV})[0] == "failed"


def test_retry_sql_keeps_failed_markers_and_skips_live_ones():
    sql = fix._RETRY_SQL
    assert "status NOT IN ('error','cancelled','rejected_done')" in sql and "LIMIT $2" in sql


def test_retry_stalled_reruns_scheduler(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_FIX", "on")
    seen = []

    async def fake(conn, tenant, project, head_id, revision_id, decision=None, **k):
        seen.append((tenant, head_id))
        return {"action": "fix", "job_id": "runner-n", "idempotent": False}
    monkeypatch.setattr(fix, "maybe_schedule_auto_fix", fake)

    class C:
        async def fetch(self, sql, *a):
            assert sql is fix._RETRY_SQL and a == (fix.MARKER_PREFIX, fix.RETRY_BATCH_LIMIT)
            return [{"tenant_id": TENANT, "project_key": "AADS", "head_id": 1, "revision_id": REV}]

    @asynccontextmanager
    async def acquire():
        yield C()
    out = asyncio.run(fix.retry_stalled_auto_fixes(SimpleNamespace(acquire=acquire)))
    assert out == {"checked": 1, "submitted": 1, "escalated": 0, "skipped": 0} and seen == [(TENANT, 1)]
    monkeypatch.setenv("DOC_AUTO_FIX", "off")
    assert asyncio.run(fix.retry_stalled_auto_fixes(SimpleNamespace(acquire=acquire)))["checked"] == 0


def test_retry_job_registered_in_scheduler():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2].joinpath("app/main.py").read_text()
    assert 'id="doc_auto_fix_retry"' in src and "retry_stalled_auto_fixes" in src


def test_digest_in_progress_is_auto_fixing_not_needs_ceo(monkeypatch):
    # [P1] 첫 보완 작업 제출 직후 요약은 같은 지문 때문에 '반복 실패'로 상신하지 않고 '자동 보완 중'으로 분류한다.
    monkeypatch.setenv("DOC_AUTO_FIX", "on")

    async def hist(*a, **k):
        return {"rounds": [{"revision_id": REV, "job_id": "runner-1", "active": True, "status": "running",
                            "round": 1, "fingerprint": "a" * 16}], "daily": 1}
    monkeypatch.setattr(fix, "load_history", hist)
    result = {"needs_ceo": [], "auto_fixing": []}
    item = {"project_key": "AADS", "document_key": "doc-a", "title": "t", "risk": "normal", "ceo_brief": None}
    asyncio.run(digest._classify_held(None, result, item, TENANT, hold(), HEAD, REVISION, [review([issue()])]))
    assert result["needs_ceo"] == [] and result["auto_fixing"][0]["job_id"] == "runner-1"


def test_digest_failed_fix_job_goes_to_needs_ceo(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_FIX", "on")

    async def hist(*a, **k):
        return {"rounds": [{"revision_id": REV, "job_id": "runner-1", "active": False, "status": "error",
                            "round": 1, "fingerprint": "a" * 16}], "daily": 1}
    monkeypatch.setattr(fix, "load_history", hist)
    result = {"needs_ceo": [], "auto_fixing": []}
    item = {"project_key": "AADS", "document_key": "doc-a", "title": "t", "risk": "normal", "ceo_brief": None}
    asyncio.run(digest._classify_held(None, result, item, TENANT, hold(), HEAD, REVISION, [review([issue()])]))
    assert result["auto_fixing"] == [] and "새 판본을 만들지 못하고" in result["needs_ceo"][0]["reason"]


def test_migration_skip_reaches_escalation_from_hook(monkeypatch):
    # [P1] 마이그레이션 문서는 evaluate 가 skip 을 돌려줘도 후크가 보완 계획(상신)으로 보낸다.
    skip = Decision("skip", "normal", ["migration_document"])
    monkeypatch.setattr(auto, "maybe_auto_approve", AsyncMock(return_value=skip))
    sched = AsyncMock(return_value=None)
    monkeypatch.setattr(fix, "maybe_schedule_auto_fix", sched)

    @asynccontextmanager
    async def acquire():
        yield object()
    monkeypatch.setattr(cross, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    asyncio.run(cross._auto_approve_after_review(TENANT, "AADS", {"id": 1}, {"id": REV}, {"review_state": "passed"},
                                                 allow_follow_up=True))
    assert sched.await_count == 1 and sched.await_args.args[5] is skip


def test_migration_skip_plans_escalation(env):
    env.set_ctx(Decision("skip", "normal", ["migration_document"]))
    out = run(FakeConn(), Decision("skip", "normal", ["migration_document"]))
    assert out["action"] == "escalate" and out["reasons"] == ["migration_document"] and env.calls == []


def test_digest_lists_migration_docs_as_needs_ceo(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_FIX", "on")
    monkeypatch.setenv("DOC_AUTO_APPROVE", "on")
    mhead = {**HEAD, "document_key": "migration-apply-m-1", "kind": "contract"}

    async def fake_assess(*a, **k):
        return {"head": mhead, "revision": REVISION, "rows": [review([issue()])],
                "decision": Decision("skip", "normal", ["migration_document"]), "summary": {}, "checks": []}
    monkeypatch.setattr(auto, "assess", fake_assess)

    async def hist(*a, **k):
        return {"rounds": [], "daily": 0}
    monkeypatch.setattr(fix, "load_history", hist)

    class C:
        async def fetch(self, sql, *a):
            if sql is digest._MIGRATION_SQL:
                return [{"head_id": 9, "tenant_id": TENANT, "project_key": "AADS", "revision_id": REV}]
            return []
    result = asyncio.run(digest.fetch_auto_approval_digest(C(), TENANT, "AADS"))
    assert [n["document_key"] for n in result["needs_ceo"]] == ["migration-apply-m-1"]
    assert "마이그레이션" in result["needs_ceo"][0]["reason"]


def test_previous_findings_not_truncated_to_ten(monkeypatch):
    # [P2] 직전 blocker·major 가 10건을 넘어도 모두(상한 90) 재검수에 넘긴다.
    monkeypatch.setenv("DOC_AUTO_FIX", "on")
    many = [issue("major", problem=f"문제 {i}") for i in range(25)]

    class C:
        async def fetchval(self, sql, *a):
            return "x\n" + fix.marker_line("doc-a", PREV, 1, "a" * 16)

        async def fetch(self, sql, *a):
            return [review(many)]
    out = asyncio.run(fix.previous_findings(C(), TENANT, "AADS", HEAD, {**REVISION, "source_task_id": "runner-x"}))
    assert len(out) == 25
    _, prompt = rubric.build_review_request("prd", "D2", "t", "c", [], [], prior_findings=out)
    assert len(json.loads(prompt)["previous_findings"]) == 25


@pytest.mark.parametrize("fix_mode,approve_mode", [("off", "on"), ("on", "off")])
def test_off_modes_log_plan(env, monkeypatch, caplog, fix_mode, approve_mode):
    # [P2] off 모드에서도 계획을 세워 doc_auto_fix_decision 로그를 남긴다(제출 0회).
    monkeypatch.setenv("DOC_AUTO_FIX", fix_mode)
    monkeypatch.setenv("DOC_AUTO_APPROVE", approve_mode)
    with caplog.at_level(logging.INFO):
        out = run(FakeConn())
    assert env.calls == [] and out["action"] == "fix"
    assert "doc_auto_fix_decision" in caplog.text and '"round": 1' in caplog.text


def test_approve_off_hook_still_logs_fix_plan(monkeypatch):
    monkeypatch.setattr(auto, "maybe_auto_approve", AsyncMock(return_value=Decision("skip", "normal", ["mode_off"])))
    sched = AsyncMock(return_value=None)
    monkeypatch.setattr(fix, "maybe_schedule_auto_fix", sched)

    @asynccontextmanager
    async def acquire():
        yield object()
    monkeypatch.setattr(cross, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    asyncio.run(cross._auto_approve_after_review(TENANT, "AADS", {"id": 1}, {"id": REV}, {"review_state": "passed"},
                                                 allow_follow_up=True))
    assert sched.await_count == 1 and sched.await_args.args[5] is None


@pytest.mark.parametrize("action,reasons,why,expected_round", [
    ("fix", ["major_present"], "ambiguous_session", 1),
    ("escalate", ["risk_high", "no_session"], "no_session", 2),
])
def test_without_session_preserves_reasons(action, reasons, why, expected_round):
    p = fix.without_session_plan(fix.FixPlan(action, reasons, 2, "fp", "feedback"), why)
    assert p.action == "escalate" and p.reasons == list(dict.fromkeys(reasons + [why]))
    assert p.round == expected_round and p.fingerprint == "fp" and p.feedback == "feedback"


@pytest.mark.parametrize("candidates,expected", [([], (None, "no_session")),
    ([TENANT], (TENANT, None)), ([TENANT, TENANT], (TENANT, None)),
    ([TENANT, REV], (None, "ambiguous_session"))])
def test_resolve_session_candidates(candidates, expected):
    from app.services.goal_report import GOAL_TARGET_SESSION_SQL
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[{"session_id": x} for x in candidates]))
    assert asyncio.run(fix.resolve_session_id(conn, TENANT, HEAD, {**REVISION, "source_session_id": "invalid"})) == expected
    sql = conn.fetch.call_args.args[0]
    assert GOAL_TARGET_SESSION_SQL in sql and "DISTINCT" in sql
    assert "gl.tenant_id=$2::uuid" in sql and "g.tenant_id=$2::uuid" in sql


def test_resolve_revision_session_first():
    conn = SimpleNamespace(fetch=AsyncMock())
    assert asyncio.run(fix.resolve_session_id(conn, TENANT, HEAD, REVISION)) == (TENANT, None)
    conn.fetch.assert_not_called()


@pytest.mark.parametrize("why", ["no_session", "ambiguous_session"])
def test_auto_fix_without_session_escalates(env, monkeypatch, why):
    monkeypatch.setattr(fix, "resolve_session_id", AsyncMock(return_value=(None, why)))
    monkeypatch.setattr(fix, "_kick_auto_resolve", lambda *a: None)
    out = run(FakeConn())
    assert out["action"] == "escalate" and out["reasons"] == ["major_present", why]
    assert env.calls == []


@pytest.mark.parametrize("why,word", [("no_session", "세션 없음"), ("ambiguous_session", "세션 모호")])
def test_digest_preserves_hold_reason_without_session(monkeypatch, why, word):
    monkeypatch.setattr(fix, "load_history", AsyncMock(return_value={"rounds": [], "daily": 0}))
    monkeypatch.setattr(fix, "resolve_session_id", AsyncMock(return_value=(None, why)))
    monkeypatch.setattr(digest, "_resolve_status", AsyncMock(return_value=None))
    result = {"auto_fixing": [], "needs_ceo": []}
    item = {"project_key": "AADS", "document_key": "doc-a", "title": "문서", "risk": "normal"}
    asyncio.run(digest._classify_held(None, result, item, TENANT, hold(["checks_failed:paths_exist"]), HEAD, REVISION, []))
    reason = result["needs_ceo"][0]["reason"]
    assert "자동 점검에서 문제가 발견됐습니다" in reason and word in reason and "checks_failed" not in reason


def test_goal_session_sql_retains_owner_and_lead_priority():
    import inspect
    from app.services import goal_report
    sql = " ".join(goal_report.GOAL_TARGET_SESSION_SQL.split())
    assert sql.startswith("COALESCE( g.owner_session_id,")
    assert "JOIN chat_sessions s ON s.id = l.task_id::uuid" in sql
    assert "l.task_type = 'chat_session'" in sql and "l.link_state = 'active'" in sql
    assert "ORDER BY (s.role_key LIKE '%Lead') DESC, l.created_at LIMIT 1" in sql
    assert "{GOAL_TARGET_SESSION_SQL}" in inspect.getsource(goal_report.report_weekly_goal_progress)


@pytest.mark.parametrize("owner,expected", [(TENANT, TENANT), (None, PREV)])
def test_resolve_goal_owner_or_active_lead(owner, expected):
    """CTE fixture로 실제 공유 선택식을 실행한다(외부 DB·쓰기 없음)."""
    import re
    import sqlite3

    class ReadOnlyConn:
        async def fetch(self, sql, head_id, tenant):
            fixture = """
                WITH goals(id, tenant_id, owner_session_id) AS (VALUES (:goal, :tenant, :owner)),
                project_document_goal_links(head_id, tenant_id, goal_id) AS (VALUES (:head, :tenant, :goal)),
                chat_sessions(id, role_key) AS (VALUES (:member, 'Member'), (:lead, 'TechLead')),
                goal_task_links(goal_id, task_id, task_type, link_state, created_at) AS (
                    VALUES (:goal, :member, 'chat_session', 'active', 1),
                           (:goal, :lead, 'chat_session', 'active', 2))
            """
            # SQLite에서는 UUID/text 캐스트만 생략하고 선택식·정렬·필터는 그대로 실행한다.
            query = re.sub(r"::(?:uuid|text)", "", sql).replace("$1", ":head").replace("$2", ":tenant")
            with sqlite3.connect(":memory:") as db:
                db.row_factory = sqlite3.Row
                return db.execute(fixture + query, {"goal": REV, "tenant": tenant, "owner": owner,
                    "head": head_id, "member": TENANT, "lead": PREV}).fetchall()

    assert asyncio.run(fix.resolve_session_id(ReadOnlyConn(), TENANT, HEAD,
        {**REVISION, "source_session_id": None})) == (expected, None)
