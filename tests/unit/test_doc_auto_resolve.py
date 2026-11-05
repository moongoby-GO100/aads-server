"""정본 문서 자동 보완 2회 실패 후속 자동화: 모드·상한·경로 5종·독립 심판·조사/분할 제출·결정 카드·요약 필드."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.services import canonical_review_digest as digest
from app.services import doc_auto_approval as auto
from app.services import doc_auto_fix as fix
from app.services import doc_auto_resolve as res
from app.services import doc_review_rubric as rubric
from app.services import next_step_proposals
from app.services import runner_job_submit as submit
from app.services.doc_auto_approval import Decision

TENANT = "00000000-0000-0000-0000-000000000001"
REV = "22222222-2222-2222-2222-222222222222"
SESSION = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=fix.KST)


def issue(sev="major", criterion="completeness", problem="완료기준이 없습니다", location="완료기준", fixt="수치를 적으세요"):
    return {"severity": sev, "criterion": criterion, "location": location, "problem": problem, "fix": fixt}


def rrow(issues=(), model="claude-x", provider="anthropic", verdict="reject", score=0.9, created=NOW):
    data = {"verdict": verdict, "summary": "요약입니다", "ceo_brief": "쉬운 설명입니다.",
            "scores": {k: score for k in rubric.SCORE_KEYS}, "score": score, "issues": list(issues)}
    return {"reviewer_model": model, "reviewer_provider": provider, "verdict": verdict, "cross_company": True,
            "reason": "요약입니다\n" + rubric.REVIEW_MARKER + json.dumps(data, ensure_ascii=False), "created_at": created}


HEAD = {"id": 1, "project_key": "AADS", "document_key": "doc-a", "kind": "prd", "generation": 2,
        "latest_revision_id": REV, "approved_revision_id": None, "goal_linked": True}
REVISION = {"id": REV, "title": "일반 문서", "version": "1.0.0", "content": "# 본문", "doc_tier": "D2",
            "author_model": "claude-author", "source_session_id": SESSION}


def hold(reasons=("major_present",), risk="normal"):
    return Decision("hold", risk, list(reasons), 0.9)


def two_rounds(status="succeeded"):
    return {"rounds": [{"revision_id": f"old{i}", "fingerprint": f"{i}" * 16, "round": i, "status": status,
                        "active": False, "job_id": f"runner-old{i}"} for i in (1, 2)], "daily": 0}


def adjudication_row(excluded=None, model="gpt-judge"):
    data = {"adjudication": {"model": model, "excluded_issue_ids": excluded or {}, "judgments": []}}
    return {"reviewer_model": model, "reviewer_provider": "openai", "verdict": "attempt_failed", "cross_company": True,
            "reason": res.ADJUDICATION_TAG + " 독립 심판\n" + rubric.REVIEW_MARKER + json.dumps(data), "created_at": NOW}


def classify(rows, decision=None, history=None, head=HEAD, revision=REVISION, jobs=()):
    return res.classify_block(decision or hold(), rows, history or two_rounds(), head, revision,
                              resolve_history={"jobs": list(jobs)})


# ---------------------------------------------------------------- 모드·상수
def test_mode_defaults_and_constants(monkeypatch):
    monkeypatch.delenv("DOC_AUTO_RESOLVE", raising=False)
    assert res.auto_resolve_mode() == "on"
    monkeypatch.setenv("DOC_AUTO_RESOLVE", "garbage")
    assert res.auto_resolve_mode() == "on"
    monkeypatch.setenv("DOC_AUTO_RESOLVE", " OFF ")
    assert res.auto_resolve_mode() == "off"
    assert res.DOC_AUTO_RESOLVE_TOTAL_LIMIT == 4
    assert set(res.PATHS) == {"adjudicate", "investigate", "split", "ceo_card", "none"}


def test_off_mode_keeps_existing_escalate(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_RESOLVE", "off")
    assert classify([rrow([issue()])]).path == "none"
    assert classify([rrow([issue()])]).reasons == ["mode_off"]
    assert res.schedule(TENANT, "AADS", 1, REV) is False
    conn = AsyncMock()
    out = asyncio.run(res.maybe_auto_resolve(conn, TENANT, "AADS", 1, REV))
    assert out["path"] == "none" and out["reasons"] == ["mode_off"]
    conn.fetch.assert_not_called()


def test_escalate_hook_calls_schedule_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(res, "schedule", lambda *a: calls.append(a) or True)
    fix._kick_auto_resolve(TENANT, "AADS", HEAD, REVISION, False)
    assert calls == []
    fix._kick_auto_resolve(TENANT, "AADS", HEAD, REVISION, True)
    assert calls == [(TENANT, "AADS", 1, REV)]
    monkeypatch.setattr(res, "schedule", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    fix._kick_auto_resolve(TENANT, "AADS", HEAD, REVISION, True)   # 예외는 로그만


# ---------------------------------------------------------------- 분류·상한
def test_not_escalated_is_none():
    out = res.classify_block(hold(), [rrow([issue()])], {"rounds": [], "daily": 0}, HEAD, REVISION, mode="on")
    assert out.path == "none" and out.reasons == ["not_escalated"]


def test_total_limit_four_counts_fix_rounds_and_resolve_jobs():
    jobs = [{"revision_id": "x", "path": "investigate", "status": "succeeded", "active": False, "job_id": "j1"},
            {"revision_id": "y", "path": "split", "status": "succeeded", "active": False, "job_id": "j2"}]
    out = classify([rrow([issue()])], jobs=jobs)
    assert out.path == "none" and out.reasons == ["total_limit"] and out.total_used == 4
    assert classify([rrow([issue()])], jobs=jobs[:1]).path == "adjudicate"


def test_active_resolve_job_blocks():
    jobs = [{"revision_id": "x", "path": "investigate", "status": "running", "active": True, "job_id": "j1"}]
    assert classify([rrow([issue()])], jobs=jobs).reasons == ["resolve_in_progress"]


def test_path_adjudicate_when_remaining_findings_and_not_judged():
    out = classify([rrow([issue("blocker", problem="롤백 없음")])], hold(["blocker_present"]))
    assert out.path == "adjudicate" and out.findings[0]["issue_id"] == "claude-x:0"


def test_path_investigate_for_factual_or_paths():
    rows = [rrow([issue(criterion="factual_consistency", problem="없는 파일을 인용")]), adjudication_row()]
    assert classify(rows).path == "investigate"
    rows = [rrow([issue(problem="경로 오류")]), adjudication_row()]
    assert classify(rows, hold(["checks_failed:paths_exist"])).path == "investigate"


def test_path_split_when_scores_high_and_two_sections():
    rows = [rrow([issue(location="3절 일정", problem="일정 근거 없음"), issue(location="5절 수치", problem="수치 없음")]),
            adjudication_row()]
    assert classify(rows).path == "split"
    spread = [rrow([issue(location=f"{n}절") for n in (1, 2, 3)]), adjudication_row()]
    assert classify(spread).path == "ceo_card"
    low = [rrow([issue(location="3절")], score=0.5), adjudication_row()]
    assert classify(low).path == "ceo_card"


def test_path_ceo_card_for_policy_failed_job_and_daily_limit():
    policy = classify([rrow([issue(problem="비용 우선순위를 정해야 합니다")])])
    assert policy.path == "ceo_card" and policy.reasons == ["policy_decision"]
    failed = {"rounds": [{"revision_id": REV, "fingerprint": "a" * 16, "round": 1, "status": "error",
                          "active": False, "job_id": "runner-f"}], "daily": 0}
    assert classify([rrow([issue()])], history=failed).reasons == ["fix_job_failed"]
    daily = {**two_rounds(), "daily": fix.DOC_AUTO_FIX_DAILY_LIMIT}
    assert classify([rrow([issue()])], history=daily).path == "ceo_card"
    assert classify([rrow([issue(problem="모호함", location="")]), adjudication_row()]).path == "ceo_card"   # 앞 경로 뒤에도 남음


def test_already_tried_revision_goes_to_card():
    jobs = [{"revision_id": REV, "path": "investigate", "status": "error", "active": False, "job_id": "j1"}]
    rows = [rrow([issue(criterion="factual_consistency")]), adjudication_row()]
    assert classify(rows, jobs=jobs).path == "ceo_card"


@pytest.mark.parametrize("decision,head,revision", [
    (hold(risk="high"), HEAD, REVISION),
    (Decision("skip", reasons=["migration_document"]), HEAD, REVISION),
    (hold(), {**HEAD, "document_key": "migration-apply-001"}, REVISION),
    (hold(), {**HEAD, "kind": "contract"}, {**REVISION, "title": "마이그레이션 계약"}),
    (hold(["checks_failed:secret_patterns"]), HEAD, REVISION),
])
def test_protected_documents_only_reach_card(decision, head, revision):
    rows = [rrow([issue(criterion="factual_consistency", location="3절")]), adjudication_row()]
    out = classify(rows, decision, head=head, revision=revision)
    assert out.path == "ceo_card" and out.protected is True


def test_protected_unjudged_runs_advisory_adjudication_first():
    out = classify([rrow([issue()])], hold(risk="high"))
    assert out.path == "adjudicate" and out.protected is True


# ---------------------------------------------------------------- 독립 심판
def test_validate_adjudication_strict():
    ok = {"judgments": [{"issue_id": "a:0", "verdict": "invalid", "evidence": "본문 2절에 있음"}]}
    assert res.validate_adjudication(ok, ["a:0"])[0]["verdict"] == "invalid"
    bad = [None, {}, {"judgments": "x"}, {"judgments": [], "extra": 1},
           {"judgments": [{"issue_id": "a:0", "verdict": "maybe", "evidence": "x"}]},
           {"judgments": [{"issue_id": "a:0", "verdict": "valid", "evidence": " "}]},
           {"judgments": [{"issue_id": "z:9", "verdict": "valid", "evidence": "x"}]},
           {"judgments": [{"issue_id": "a:0", "verdict": "valid", "evidence": "x", "k": 1}]},
           {"judgments": [{"issue_id": "a:0", "verdict": "valid", "evidence": "x"}] * 2}]
    for obj in bad:
        with pytest.raises(ValueError):
            res.validate_adjudication(obj, ["a:0"])
    with pytest.raises(ValueError):   # 일부만 판정
        res.validate_adjudication(ok, ["a:0", "a:1"])


def test_pick_adjudicator_prefers_unreviewed_other_company():
    rows = [{"reviewer_model": "claude-x", "reviewer_provider": "anthropic"}]
    models = ["claude-x", "claude-author", "claude-y", "codex:gpt-5"]
    assert res.pick_adjudicator(models, "claude-author", rows) == "codex:gpt-5"
    assert res.pick_adjudicator(["claude-x", "claude-author"], "claude-author", rows) is None


class RecConn:
    def __init__(self):
        self.recorded = []

    @asynccontextmanager
    async def transaction(self):
        yield


@pytest.fixture
def adjudicator(monkeypatch):
    rec = []

    async def fake_record(conn, tenant, project, head, revision, pick, verdict, reason):
        rec.append((pick.model, verdict, reason))
    monkeypatch.setattr(res.cross, "_record", fake_record)

    async def fake_models(conn):
        return ["claude-x", "codex:gpt-5"]
    monkeypatch.setattr(res, "_candidate_models", fake_models)
    return rec


def _run_adjudication(monkeypatch, response_text, rec_findings=None):
    from app.services import code_reviewer as cli

    async def fake_call(**kw):
        return response_text
    monkeypatch.setattr(cli, "_call_review_model", fake_call)
    monkeypatch.setattr(cli, "_extract_review_text", lambda r: (r, {}))
    rows = [rrow([issue(), issue("minor", problem="사소")])]
    findings = res.remaining_findings(rows)
    ctx = {"head": HEAD, "revision": REVISION, "rows": rows}
    return findings, asyncio.run(res.run_adjudication(None, TENANT, "AADS", ctx, findings, protected=False))


def test_adjudication_records_invalid_with_evidence(monkeypatch, adjudicator):
    findings, out = _run_adjudication(monkeypatch, json.dumps(
        {"judgments": [{"issue_id": "claude-x:0", "verdict": "invalid", "evidence": "4절에 이미 수치가 있음"}]}))
    assert out["status"] == "recorded" and out["invalid"] == ["claude-x:0"]
    model, verdict, reason = adjudicator[0]
    assert model == "codex:gpt-5" and verdict == "attempt_failed"   # 승인 집계에 끼지 않는 기록
    _, data = rubric.parse_review_reason(reason)
    assert data["adjudication"]["excluded_issue_ids"] == {"claude-x:0": findings[0]["digest"]}
    assert "4절에 이미 수치가 있음" in reason


@pytest.mark.parametrize("text", ["not json", json.dumps({"judgments": [{"issue_id": "claude-x:0", "verdict": "ok"}]}),
                                  json.dumps({"verdict": "pass"})])
def test_adjudication_format_error_is_attempt_failed(monkeypatch, adjudicator, text):
    _, out = _run_adjudication(monkeypatch, text)
    assert out["status"] == "attempt_failed" and out["invalid"] == []
    assert adjudicator[0][1] == "attempt_failed" and adjudicator[0][2].startswith(res.ADJUDICATION_TAG)
    assert res.is_adjudication_row({"reason": adjudicator[0][2]})   # 실패도 '심판 실시'로 센다


def test_adjudication_without_model_is_attempt_failed(monkeypatch, adjudicator):
    async def none_models(conn):
        return []
    monkeypatch.setattr(res, "_candidate_models", none_models)
    ctx = {"head": HEAD, "revision": REVISION, "rows": [rrow([issue()])]}
    out = asyncio.run(res.run_adjudication(None, TENANT, "AADS", ctx, res.remaining_findings(ctx["rows"]), protected=False))
    assert out["status"] == "attempt_failed" and adjudicator[0][0] == "unconfigured"


# ---------------------------------------------------------------- invalid 제외 후 재판정(기준값 불변)
def _approvable_rows(extra=()):
    return [rrow([issue("major", problem="가짜 지적")], model="claude-x", provider="anthropic", verdict="pass"),
            rrow([], model="codex:gpt", provider="openai", verdict="pass"), *extra]


def _evaluate(rows, excluded=None, head=HEAD, revision=REVISION):
    return auto.evaluate(head, revision, rows, {"review_state": "passed"}, [{"id": "c", "ok": True}],
                         excluded_issue_ids=excluded)


def test_invalid_judgment_excluded_then_approve_and_thresholds_unchanged():
    rows = _approvable_rows()
    assert _evaluate(rows).action == "hold" and "major_present" in _evaluate(rows).reasons
    digest_ = auto.indexed_issues(rows)[0]["digest"]
    judged = rows + [adjudication_row({"claude-x:0": digest_})]
    excluded = auto.adjudication_excluded_ids(judged)
    assert excluded == frozenset({"claude-x:0"})
    assert _evaluate(judged, excluded).action == "approve"
    assert (auto.DEFAULT_MIN_SCORE, auto.DEFAULT_MIN_CRITERION, auto.DEFAULT_HIGH_RISK_MIN_SCORE) == (0.85, 0.70, 0.90)
    low = [rrow([issue("major")], model="claude-x", provider="anthropic", verdict="pass", score=0.5),
           rrow([], model="codex:gpt", provider="openai", verdict="pass")]
    low = low + [adjudication_row({"claude-x:0": auto.indexed_issues(low)[0]["digest"]})]
    assert "score_below_min" in _evaluate(low, auto.adjudication_excluded_ids(low)).reasons   # 점수 기준은 그대로


def test_exclusion_requires_same_issue_content():
    rows = _approvable_rows()
    forged = rows + [adjudication_row({"claude-x:0": "0" * 12})]
    assert auto.adjudication_excluded_ids(forged) == frozenset()
    assert _evaluate(forged, auto.adjudication_excluded_ids(forged)).action == "hold"


def test_assess_applies_exclusion_but_not_for_high_risk(monkeypatch):
    monkeypatch.setattr(rubric, "run_checks", lambda *a, **k: [{"id": "c", "ok": True}])
    rows = _approvable_rows()
    rows = rows + [adjudication_row({"claude-x:0": auto.indexed_issues(rows)[0]["digest"]})]

    class Conn:
        def __init__(self, title):
            self.title = title
            self.n = 0

        async def fetchrow(self, sql, *a):
            self.n += 1
            return dict(HEAD) if self.n == 1 else {**REVISION, "title": self.title}

        async def fetch(self, sql, *a):
            return [{**r, "id": i} for i, r in enumerate(rows)]
    ok = asyncio.run(auto.assess(Conn("일반 문서"), TENANT, "AADS", 1, REV))
    assert ok["decision"].action == "approve"
    high = asyncio.run(auto.assess(Conn("보안 인증 문서"), TENANT, "AADS", 1, REV))
    assert high["decision"].action == "hold" and "major_present" in high["decision"].reasons


# ---------------------------------------------------------------- 실행(모킹)
class Db:
    """head 잠금을 asyncio.Lock 으로 흉내 내는 가짜 연결 + 제출된 작업 저장소."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.jobs = []
        self.executed = []

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield

    async def execute(self, sql, *args):
        self.executed.append(args)

    async def fetchval(self, sql, *args):
        return 1

    async def fetchrow(self, sql, *args):
        return None


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_RESOLVE", "on")
    monkeypatch.setenv("DOC_AUTO_APPROVE", "on")
    db = Db()
    state = {"rows": [], "decision": hold(), "head": HEAD, "revision": REVISION, "history": two_rounds()}
    approve = AsyncMock(return_value=Decision("hold", "normal", ["major_present"]))
    monkeypatch.setattr(auto, "maybe_auto_approve", approve)

    async def fake_assess(conn, tenant, project, head_id, revision_id=None, *, lock=False):
        await asyncio.sleep(0)
        return {"head": state["head"], "revision": state["revision"], "rows": list(state["rows"]),
                "decision": state["decision"], "summary": {}, "checks": []}
    monkeypatch.setattr(auto, "assess", fake_assess)

    async def fake_fix_history(conn, tenant, project, key, *, now=None):
        return state["history"]
    monkeypatch.setattr(fix, "load_history", fake_fix_history)

    async def fake_resolve_history(conn, tenant, project, key, *, now=None):
        jobs = [{**m, "job_id": j["job_id"], "status": j["status"], "active": j["status"] == "queued"}
                for j in db.jobs for m in res.parse_markers(j["instruction"])]
        return {"jobs": jobs, "daily": state.get("resolve_daily", len(jobs))}
    monkeypatch.setattr(res, "load_resolve_history", fake_resolve_history)

    async def fake_submit(conn, **kw):
        await asyncio.sleep(0)
        job = {"job_id": f"runner-{len(db.jobs)}", "status": "queued", "instruction": kw["instruction"], **kw}
        db.jobs.append(job)
        return {"job_id": job["job_id"], "status": "queued", "idempotent": False}
    monkeypatch.setattr(submit, "submit_runner_job_internal", fake_submit)

    cards = []

    async def fake_propose(session_id, steps, context="", tenant_id=""):
        cards.append({"session_id": session_id, "steps": steps, "tenant_id": tenant_id})
        return {"cards": [{"id": f"card-{len(cards)}-{i}", "title": s["title"]} for i, s in enumerate(steps)]}
    monkeypatch.setattr(next_step_proposals, "propose", fake_propose)
    found = {"card": None}

    async def fake_find_card(conn, tenant, project, key, rev):
        return found["card"]
    monkeypatch.setattr(res, "find_card", fake_find_card)
    return type("W", (), {"db": db, "state": state, "approve": approve, "cards": cards, "found": found})


def resolve(w):
    return asyncio.run(res.maybe_auto_resolve(w.db, TENANT, "AADS", 1, REV))


def test_investigate_submits_one_job_with_marker_and_is_idempotent(world):
    world.state["rows"] = [rrow([issue(criterion="factual_consistency", problem="없는 함수 인용")]), adjudication_row()]
    out = resolve(world)
    assert out["path"] == "investigate" and out["job_id"] == "runner-0" and len(world.db.jobs) == 1
    text = world.db.jobs[0]["instruction"]
    assert f"DOC_AUTO_RESOLVE_OF: doc-a rev={REV} path=investigate" in text
    assert "코드·DB 수정 금지" in text and "읽기 전용" in text and "없는 함수 인용" in text
    assert world.db.jobs[0]["origin"] == "doc_auto_resolve"
    again = resolve(world)
    assert again["path"] == "none" and again["reasons"] == ["resolve_in_progress"] and len(world.db.jobs) == 1


def test_split_submits_open_items_instruction(world):
    world.state["rows"] = [rrow([issue(location="3절 일정", problem="근거 없음")]), adjudication_row()]
    out = resolve(world)
    text = world.db.jobs[0]["instruction"]
    assert out["path"] == "split" and f"rev={REV} path=split" in text
    assert "doc-a-open-items" in text and "한글 제목" in text and "코드는 수정하지 않는다" in text


def test_concurrent_calls_submit_once(world):
    world.state["rows"] = [rrow([issue(criterion="factual_consistency")]), adjudication_row()]

    async def both():
        return await asyncio.gather(res.maybe_auto_resolve(world.db, TENANT, "AADS", 1, REV),
                                    res.maybe_auto_resolve(world.db, TENANT, "AADS", 1, REV))
    a, b = asyncio.run(both())
    assert len(world.db.jobs) == 1
    assert {a["job_id"], b["job_id"]} == {"runner-0"} and sorted([a["idempotent"], b["idempotent"]]) == [False, True]


def test_daily_limit_inside_lock_downgrades_to_card(world, monkeypatch):
    world.state["rows"] = [rrow([issue(criterion="factual_consistency")]), adjudication_row()]
    # 하루 상한은 fix.load_history 의 daily(자동 보완+후속 처리 합계) 하나로 센다.
    # 잠금 밖 판정에서는 여유가 있었지만 잠금 안에서 다시 읽으니 상한에 도달한 경쟁 상황.
    calls = {"n": 0}

    async def racing_history(conn, tenant, project, key, *, now=None):
        calls["n"] += 1
        return {**two_rounds(), "daily": 0 if calls["n"] == 1 else fix.DOC_AUTO_FIX_DAILY_LIMIT}
    monkeypatch.setattr(fix, "load_history", racing_history)
    out = resolve(world)
    assert out["path"] == "ceo_card" and world.db.jobs == [] and len(world.cards) == 1
    assert any("doc-auto-fix-daily:" + TENANT in str(a) for a in world.db.executed)


def test_adjudication_invalid_then_approve_goes_through_maybe_auto_approve(world, monkeypatch):
    world.state["rows"] = [rrow([issue()])]
    world.approve.return_value = Decision("approve", "normal", [], 0.9)

    async def fake_adj(conn, tenant, project, ctx, findings, *, protected):
        world.state["rows"].append(adjudication_row({findings[0]["issue_id"]: findings[0]["digest"]}))
        return {"status": "recorded", "invalid": [findings[0]["issue_id"]], "model": "m"}
    monkeypatch.setattr(res, "run_adjudication", fake_adj)
    out = resolve(world)
    assert out["outcome"] == "approved" and world.approve.await_count == 1 and world.db.jobs == [] and world.cards == []


def test_adjudication_valid_moves_to_next_path(world, monkeypatch):
    world.state["rows"] = [rrow([issue(criterion="factual_consistency", problem="수치 불일치")])]

    async def fake_adj(conn, tenant, project, ctx, findings, *, protected):
        world.state["rows"].append(adjudication_row())
        return {"status": "recorded", "invalid": [], "model": "m"}
    monkeypatch.setattr(res, "run_adjudication", fake_adj)
    out = resolve(world)
    assert out["path"] == "investigate" and len(world.db.jobs) == 1 and world.approve.await_count == 0


def test_adjudication_failure_falls_through_without_repeat(world, monkeypatch):
    world.state["rows"] = [rrow([issue(problem="모호함", location="")])]
    calls = []

    async def fake_adj(conn, tenant, project, ctx, findings, *, protected):
        calls.append(1)
        world.state["rows"].append({**adjudication_row(), "reason": res.ADJUDICATION_TAG + " 실패: x"})
        return {"status": "attempt_failed", "invalid": [], "model": "m"}
    monkeypatch.setattr(res, "run_adjudication", fake_adj)
    out = resolve(world)
    assert calls == [1] and out["path"] == "ceo_card" and world.approve.await_count == 0


def test_protected_document_never_auto_approved_and_only_card(world, monkeypatch):
    world.state["decision"] = hold(risk="high")
    world.state["rows"] = [rrow([issue(criterion="factual_consistency")])]
    world.approve.return_value = Decision("approve", "high", [], 0.95)

    async def fake_adj(conn, tenant, project, ctx, findings, *, protected):
        assert protected is True
        world.state["rows"].append(adjudication_row({findings[0]["issue_id"]: findings[0]["digest"]}))
        return {"status": "recorded", "invalid": [findings[0]["issue_id"]], "model": "m"}
    monkeypatch.setattr(res, "run_adjudication", fake_adj)
    out = resolve(world)
    assert out["path"] == "ceo_card" and world.approve.await_count == 0 and world.db.jobs == []
    assert len(world.cards) == 1


# ---------------------------------------------------------------- 결정 카드
def test_card_one_card_set_with_recommendation_and_idempotent(world):
    world.state["rows"] = [rrow([issue(problem="비용 우선순위를 정해야 합니다")])]
    out = resolve(world)
    assert out["path"] == "ceo_card" and out["card_id"] == "card-1-0" and len(world.cards) == 1
    steps = world.cards[0]["steps"]
    # 문서·판본당 카드 1장: 승인 = 권장안, 대안 2개는 본문에 비권장 사유와 함께(runner-5718da52 반려 반영)
    assert len(steps) == 1 and "권장" in steps[0]["title"] and "일반 문서" in steps[0]["title"]
    detail = steps[0]["detail"]
    assert "권장 이유" in detail and detail.count("비권장 이유") == 2 and "대안 1" in detail and "대안 2" in detail
    assert steps[0]["rollback"] and steps[0]["risk"] in ("medium", "high")
    assert res.parse_card_marker(detail)["option"] == "revise"
    assert world.cards[0]["session_id"] == SESSION and world.cards[0]["tenant_id"] == TENANT
    world.found["card"] = {"id": "card-1-0", "decision": "pending", "created_at": NOW}
    again = resolve(world)
    assert len(world.cards) == 1 and again["idempotent"] is True and again["card_id"] == "card-1-0"


def test_card_without_session_does_not_crash(world):
    world.state["revision"] = {**REVISION, "source_session_id": None}
    world.state["rows"] = [rrow([issue(problem="정책 결정 필요")])]
    out = resolve(world)
    assert out["path"] == "ceo_card" and out["card_id"] is None and world.cards == []


def test_no_answer_means_no_approval_and_no_submit(world):
    world.state["rows"] = [rrow([issue(problem="예산 선택")])]
    resolve(world)
    assert world.approve.await_count == 0 and world.db.jobs == []


def _card_summary(world, option):
    s = world.cards[0]["steps"][0]
    detail = s["detail"]
    if option != "revise":
        # 새 카드는 revise 1장뿐이다. split/keep 은 이전 형식으로 이미 발행된 카드의 승인 처리 호환을 검증한다.
        detail = detail.replace(" opt=revise", f" opt={option}")
    return f"[다음단계 1] {s['title']}\n{detail}\n· 되돌리는 법: {s['rollback']}"


def test_card_approval_submits_chosen_option_once(world):
    world.state["rows"] = [rrow([issue(problem="정책 결정 필요")])]
    resolve(world)
    summary = _card_summary(world, "revise")
    out = asyncio.run(res.on_card_approved(world.db, TENANT, summary))
    assert out["job_id"] == "runner-0" and len(world.db.jobs) == 1
    text = world.db.jobs[0]["instruction"]
    assert "지적 반영 새 판본 작성" in text and f"DOC_AUTO_RESOLVE_OF: doc-a rev={REV} path=card" in text
    assert world.db.jobs[0]["origin"] == "doc_auto_resolve"
    again = asyncio.run(res.on_card_approved(world.db, TENANT, summary))
    assert again["idempotent"] is True and len(world.db.jobs) == 1


def test_card_split_option_builds_split_instruction_and_keep_does_nothing(world):
    world.state["rows"] = [rrow([issue(problem="정책 결정 필요")])]
    resolve(world)
    out = asyncio.run(res.on_card_approved(world.db, TENANT, _card_summary(world, "keep")))
    assert out["reasons"] == ["keep"] and world.db.jobs == []
    asyncio.run(res.on_card_approved(world.db, TENANT, _card_summary(world, "split")))
    assert "doc-a-open-items" in world.db.jobs[0]["instruction"]


def test_card_approval_on_stale_revision_or_unknown_marker(world):
    world.state["rows"] = [rrow([issue(problem="정책 결정 필요")])]
    resolve(world)
    summary = _card_summary(world, "revise")
    world.state["head"] = {**HEAD, "latest_revision_id": "99999999-9999-9999-9999-999999999999"}
    out = asyncio.run(res.on_card_approved(world.db, TENANT, summary))
    assert out["reasons"] == ["stale_revision"] and world.db.jobs == []
    assert asyncio.run(res.on_card_approved(world.db, TENANT, "마커 없는 카드")) is None


# ---------------------------------------------------------------- 요약(needs_ceo)
def _item():
    return {"project_key": "AADS", "document_key": "doc-a", "title": "T", "risk": "normal", "ceo_brief": "b"}


def test_digest_needs_ceo_new_fields_and_auto_fixing_split(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_FIX", "on")
    status = {"resolve_path": "ceo_card", "card_id": "card-1", "pending_days": 3, "active": None}
    monkeypatch.setattr(digest, "_resolve_status", AsyncMock(return_value=status))
    history = two_rounds()
    monkeypatch.setattr(fix, "load_history", AsyncMock(return_value=history))
    result = {"needs_ceo": [], "auto_fixing": []}
    asyncio.run(digest._classify_held(None, result, _item(), TENANT, hold(), HEAD, REVISION, [rrow([issue()])]))
    entry = result["needs_ceo"][0]
    assert (entry["resolve_path"], entry["card_id"], entry["pending_days"]) == ("ceo_card", "card-1", 3)
    for old in ("project_key", "document_key", "title", "risk", "round", "max_rounds", "reason", "ceo_brief",
                "remaining_issues", "decision_needed"):
        assert old in entry
    text = digest.format_digest([], {"mode": "on", "auto_approved": [], "held": [], "would_approve": [],
                                     "needs_ceo": [entry], "auto_fixing": []})
    assert "버튼으로 결정" in text and "미결 3일" in text
    # 후속 자동 처리가 돌고 있으면 대표님 판단 필요가 아니라 자동 보완 중이다.
    active = {"resolve_path": "investigate", "card_id": None, "pending_days": 0,
              "active": {"job_id": "runner-9", "path": "investigate"}}
    monkeypatch.setattr(digest, "_resolve_status", AsyncMock(return_value=active))
    result = {"needs_ceo": [], "auto_fixing": []}
    asyncio.run(digest._classify_held(None, result, _item(), TENANT, hold(), HEAD, REVISION, [rrow([issue()])]))
    assert result["needs_ceo"] == [] and result["auto_fixing"][0]["resolve_path"] == "investigate"


def test_digest_text_stays_within_3900_with_cards(monkeypatch):
    items = [{"project_key": "AADS", "document_key": f"doc-{i}", "title": "긴 제목 " * 20, "risk": "normal", "round": 2,
              "max_rounds": 2, "reason": "사유 " * 80, "ceo_brief": "요약 " * 80,
              "remaining_issues": [{"severity": "major", "location": "x", "problem": "문제 " * 80}] * 3,
              "decision_needed": fix.DECISION_REQUEST_KO, "resolve_path": "ceo_card", "card_id": f"c{i}",
              "pending_days": i} for i in range(30)]
    text = digest.format_digest([], {"mode": "on", "auto_approved": [], "held": [], "would_approve": [],
                                     "needs_ceo": items, "auto_fixing": []})
    assert len(text) <= 3900 and "버튼으로 결정" in text


def test_resolve_status_reads_card_and_pending_days(monkeypatch):
    created = datetime.now(timezone.utc) - timedelta(days=2, hours=1)

    async def hist(conn, tenant, project, key, *, now=None):
        return {"jobs": [], "daily": 0}

    async def card(conn, tenant, project, key, rev):
        return {"id": "card-7", "decision": "pending", "created_at": created}
    monkeypatch.setattr(res, "load_resolve_history", hist)
    monkeypatch.setattr(res, "find_card", card)
    out = asyncio.run(res.resolve_status(None, TENANT, "AADS", "doc-a", REV))
    assert out["resolve_path"] == "ceo_card" and out["card_id"] == "card-7" and out["pending_days"] == 2


def test_source_guards():
    """코드 약속: 승인 경로·기준값을 건드리지 않고 기존 카드 경로를 재사용한다."""
    import inspect
    src = inspect.getsource(res)
    assert "DOC_AUTO_RESOLVE" in src and "DOC_AUTO_RESOLVE_TOTAL_LIMIT = 4" in src
    assert "next_step_proposals" in src and "_require_human_approver(" not in src
    assert "approve_revision_in_tx" not in src and "review_revision_in_tx" not in src


# ---------------------------------------------------------------- runner-5718da52 반려 지적 회귀

def test_single_card_per_document_revision_title_has_key_and_revision(world):
    # [P1] 선택지마다 카드가 생기지 않는다. 제목에 document_key·판본이 있어 판본이 바뀌면 다른 카드가 된다.
    world.state["rows"] = [rrow([issue(problem="정책 결정 필요")])]
    resolve(world)
    steps = world.cards[0]["steps"]
    assert len(steps) == 1
    assert "doc-a" in steps[0]["title"] and REV[:8] in steps[0]["title"]
    other = res.build_card_steps("AADS", HEAD, {**REVISION, "id": "55555555-5555-5555-5555-555555555555"},
                                 res.Resolution("ceo_card", ["policy_decision"]))
    assert other[0]["title"] != steps[0]["title"]


def test_protected_document_card_is_high_risk():
    steps = res.build_card_steps("AADS", HEAD, REVISION, res.Resolution("ceo_card", ["protected_document"], protected=True))
    assert steps[0]["risk"] == "high" and "사람이 승인해야" in steps[0]["detail"]


def test_card_routed_to_auto_is_reported_not_treated_as_card(world, monkeypatch):
    # [P1] 기존 grant·목표 정책이 덮어 카드 대신 auto 로 돌아오면 카드가 있다고 보지 않는다.
    from app.services import next_step_proposals

    async def auto_only(session_id, steps, context="", tenant_id=""):
        return {"cards": [], "auto": [{"title": steps[0]["title"], "grant_id": "g1"}]}
    monkeypatch.setattr(next_step_proposals, "propose", auto_only)
    world.state["rows"] = [rrow([issue(problem="정책 결정 필요")])]
    out = resolve(world)
    assert out["card_id"] is None and out["outcome"] == "card_routed_to_auto" and world.db.jobs == []


def test_daily_limit_counts_fix_and_resolve_markers_together():
    # [P1] 자동 보완 경로의 하루 상한 SQL 도 후속 처리 마커를 함께 센다.
    sql = fix._DAILY_SQL
    assert "strpos(instruction,$2)>0 OR strpos(instruction,$4)>0" in sql
    assert fix.RESOLVE_MARKER_PREFIX == res.MARKER_PREFIX


def test_load_history_daily_query_passes_both_markers():
    seen = {}

    class C:
        async def fetch(self, sql, *a):
            return []

        async def fetchval(self, sql, *a):
            seen["args"] = a
            return 7
    out = asyncio.run(fix.load_history(C(), TENANT, "AADS", "doc-a", now=NOW))
    assert out["daily"] == 7 and seen["args"][1] == fix.MARKER_PREFIX and seen["args"][3] == res.MARKER_PREFIX


def test_secret_document_never_goes_to_adjudication():
    # [P1] 시크릿이 탐지되면 blocker·major 가 남아 있어도 외부 심판으로 보내지 않고 카드로만.
    secret_rev = {**REVISION, "content": "# 본문\nOPENAI_API_KEY=sk-proj-" + "A" * 40}
    out = classify([rrow([issue("blocker")])], revision=secret_rev)
    assert out.path == "ceo_card" and out.reasons == ["secret_detected"]
    out2 = classify([rrow([issue("blocker")])], decision=hold(["blocker_present", "checks_failed:secret_patterns"]))
    assert out2.path == "ceo_card" and out2.reasons == ["secret_detected"]


def test_adjudication_request_masks_secret_like_text():
    rev = {**REVISION, "content": "토큰 Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"}
    findings = [{"issue_id": "m:0", "severity": "major", "criterion": "c", "location": "L",
                 "problem": "키 sk-proj-" + "B" * 40 + " 노출", "fix": "f"}]
    _, payload = res.build_adjudication_request(HEAD, rev, findings)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in payload and "B" * 40 not in payload
