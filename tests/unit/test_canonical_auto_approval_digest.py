"""자동승인 참고검토: 일일 요약 확장, 조회 API, 수정 요청(change-request) API."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import canonical_documents as docs
from app.services import canonical_review_digest as digest
from app.services import doc_auto_approval as auto
from app.services import doc_review_rubric as rubric
from app.services import runner_job_submit as submit
from app.services.doc_auto_approval import Decision

TENANT = "00000000-0000-0000-0000-000000000001"
REV = UUID("22222222-2222-2222-2222-222222222222")
SESSION = "11111111-1111-1111-1111-111111111111"
SECRET_BODY = "본문-절대-노출-금지-XYZ"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=digest.KST)


def review_row(model="claude-x", provider="anthropic", score=0.95, minor=("작은 문제 하나",)):
    reason = "요약\n" + rubric.REVIEW_MARKER + json.dumps({"verdict": "pass", "summary": "요약", "ceo_brief": "쉬운 설명입니다.", "score": score,
                         "scores": {"a": score},
                         "issues": [{"severity": "minor", "criterion": "a", "location": "L1", "problem": p, "fix": "f"} for p in minor]})
    return {"reviewer_model": model, "reviewer_provider": provider, "verdict": "pass", "reason": reason,
            "cross_company": True, "created_at": NOW}


def approved_row(key="doc-a", project="AADS", title="일반 문서", content="# 본문\n" + SECRET_BODY):
    return {"tenant_id": TENANT, "project_key": project, "document_key": key, "kind": "prd", "generation": 5,
            "title": title, "revision_id": REV, "version": "1.0.0", "doc_tier": "D2", "content": content,
            "approved_at": NOW, "review_rows": [review_row(), review_row("gpt-x", "openai", 0.9)]}


class AutoConn:
    def __init__(self, approved=(), candidates=()):
        self.approved, self.candidates = list(approved), list(candidates)
        self.writes = []

    async def fetch(self, sql, *args):
        if sql.startswith("SELECT h.tenant_id::text AS tenant_id, h.project_key, h.document_key, h.kind, h.generation"):
            return self.approved
        if "migration-apply-%" in sql and "contract" in sql:
            return getattr(self, "migrations", [])
        if "AS head_id" in sql:
            return self.candidates
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append(sql)


def ctx_for(action, reasons=(), risk="normal", score=0.8, key="held-doc"):
    head = {"project_key": "AADS", "document_key": key, "kind": "prd", "generation": 3}
    rev = {"id": REV, "title": "보류 문서", "version": "2.0.0", "doc_tier": "D2"}
    return {"head": head, "revision": rev, "rows": [review_row()], "decision": Decision(action, risk, list(reasons), score)}


def fetch(conn, monkeypatch, mode="on", ctxs=None):
    monkeypatch.setenv("DOC_AUTO_APPROVE", mode)
    queue = list(ctxs or [])

    async def fake_assess(*a, **k):
        return queue.pop(0)
    monkeypatch.setattr(auto, "assess", fake_assess)
    return asyncio.run(digest.fetch_auto_approval_digest(conn, TENANT, "AADS", since_hours=24))


def test_auto_approved_and_held_are_separated_without_db_writes(monkeypatch):
    conn = AutoConn([approved_row()], [{"head_id": 1, "tenant_id": TENANT, "project_key": "AADS", "revision_id": REV}] * 3)
    result = fetch(conn, monkeypatch, ctxs=[
        ctx_for("hold", ["needs_second_company", "score_below_min"]), ctx_for("approve"), ctx_for("skip")])
    assert [i["document_key"] for i in result["auto_approved"]] == ["doc-a"]
    assert len(result["held"]) == 1 and result["would_approve"] == []
    held = result["held"][0]
    assert held["hold_reasons"][0] == "서로 다른 두 회사의 통과 검토가 더 필요합니다"
    assert "needs_second" not in " ".join(held["hold_reasons"])
    item = result["auto_approved"][0]
    for field in ("project_key", "document_key", "title", "kind", "doc_tier", "revision_id", "version", "generation",
                  "mode", "risk", "score", "ceo_brief", "minor_issues", "hold_reasons", "reviewer_models"):
        assert field in item
    assert item["minor_issues"] == ["작은 문제 하나"] and item["reviewer_models"] == ["claude-x", "gpt-x"]
    assert conn.writes == []
    assert SECRET_BODY not in json.dumps(result, default=str)


def test_shadow_mode_only_reports_would_approve(monkeypatch):
    cand = [{"head_id": 1, "tenant_id": TENANT, "project_key": "AADS", "revision_id": REV}]
    on = fetch(AutoConn([], cand), monkeypatch, "on", [ctx_for("approve")])
    assert on["would_approve"] == [] and on["held"] == []
    shadow = fetch(AutoConn([], cand), monkeypatch, "shadow", [ctx_for("approve")])
    assert len(shadow["would_approve"]) == 1 and shadow["mode"] == "shadow"


def test_minor_issues_capped_at_three(monkeypatch):
    row = approved_row()
    row["review_rows"] = [review_row(minor=("a1", "a2", "a3", "a4", "a5"))]
    assert len(fetch(AutoConn([row]), monkeypatch)["auto_approved"][0]["minor_issues"]) == 3


def test_high_risk_marked(monkeypatch):
    result = fetch(AutoConn([approved_row(title="보안 인증 정책")]), monkeypatch)
    assert result["auto_approved"][0]["risk"] == "high"
    text = digest.format_digest([], result)
    assert "자동승인 1건" in text and "고위험" in text


def make_auto(n_approved, n_held=0):
    def item(i, risk="normal"):
        return {"tenant_id": TENANT, "project_key": "AADS", "document_key": f"auto-{i}", "title": f"자동 문서 {i}" * 4,
                "risk": risk, "score": 0.9, "hold_reasons": ["검토 점수가 기준에 못 미칩니다"]}
    return {"mode": "on", "auto_approved": [item(i) for i in range(n_approved)],
            "held": [item(i) for i in range(n_held)], "would_approve": []}


def test_format_sections_cap_and_link():
    text = digest.format_digest([], make_auto(2, 1))
    assert "자동승인 2건" in text and "보류 1건(수정 필요)" in text and "/docs" in text
    big = digest.format_digest([{"project_key": "AADS", "document_key": "w", "title": "대기", "waiting_days": 1}],
                               make_auto(150, 10))
    assert len(big) <= digest.MAX_MESSAGE_CHARS
    assert "검토 대기 정본 문서 1건" in big and "자동승인 150건" in big


def test_existing_section_unchanged_without_auto():
    items = [{"project_key": "AADS", "document_key": "d", "title": "t", "waiting_days": 2}]
    assert digest.format_digest(items) == digest.format_digest(items, None)
    assert digest.format_digest(items).count("자동승인") == 0


def test_zero_everything_not_sent(monkeypatch):
    monkeypatch.setenv("DOC_AUTO_APPROVE", "on")
    sent = []

    class Conn:
        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def fetch(self, sql, *a):
            return []

        async def fetchval(self, *a):
            return None

        async def execute(self, *a):
            pass

    class Pool:
        def acquire(self):
            return Conn()

    async def sender(text):
        sent.append(text)
        return True
    result = asyncio.run(digest.send_daily_digest(Pool(), now=NOW, sender=sender))
    assert sent == [] and result["sent"] == 0


# ── API ──
class Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, *_):
        if exc_type:
            self.conn.rolled_back = True
            self.conn.state = dict(self.conn.snapshot)
        return False


class CRConn:
    def __init__(self, *, approved=True, generation=4, latest=REV, previous_job=None, project="AADS"):
        self.head = {"id": 7, "latest_revision_id": latest, "approved_revision_id": REV if approved else None,
                     "generation": generation}
        self.snapshot = {"approved": self.head["approved_revision_id"]}
        self.state = dict(self.snapshot)
        self.previous_job, self.rolled_back, self.jobs, self.sqls = previous_job, False, [], []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def transaction(self):
        return Tx(self)

    async def fetchval(self, sql, *args):
        if "FROM pipeline_jobs" in sql:
            return self.previous_job
        return True

    async def fetchrow(self, sql, *args):
        if "FROM project_document_heads" in sql:
            return self.head
        if "FROM project_document_revisions" in sql:
            return {"id": REV, "version": "1.2.0", "source_session_id": SESSION}
        if "FROM pipeline_jobs" in sql:
            return None
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.sqls.append(sql)
        if "SET approved_revision_id=NULL" in sql:
            self.state["approved"] = None
        if sql.startswith("INSERT INTO pipeline_jobs"):
            self.jobs.append(args)


def human(role="member"):
    return {"tenant": {"id": TENANT}, "user": {"id": "user-1", "auth_type": "human_session"},
            "membership": {"role": role, "status": "active"}}


FEEDBACK = "세 번째 절의 기한을 명확히 고쳐주세요."


def call(conn, monkeypatch, context=None, *, feedback=FEEDBACK, generation=4, revision=REV, project="AADS"):
    monkeypatch.setattr(docs, "get_pool", lambda: conn)
    body = docs.ChangeRequestInput(revision_id=revision, expected_generation=generation, feedback=feedback)
    return asyncio.run(docs.change_request(project, "doc-a", body, context or human("admin")))


def code(excinfo):
    return excinfo.value.status_code, excinfo.value.detail


def test_change_request_requires_human_first(monkeypatch):
    conn = CRConn()
    for ctx in ({**human("admin"), "user": {"id": "system:bot", "auth_type": "human_session"}},
                {**human("admin"), "user": {"id": "u", "auth_type": "api_key"}}):
        with pytest.raises(HTTPException) as e:
            call(conn, monkeypatch, ctx)
        assert code(e) == (403, "human_login_required")
    assert conn.sqls == []


def test_change_request_archives_approved_and_submits_job(monkeypatch):
    conn = CRConn()
    result = call(conn, monkeypatch)
    assert result["archived"] is True and result["idempotent"] is False and result["job_id"].startswith("runner-")
    assert conn.state["approved"] is None
    instruction = conn.jobs[0][2]
    for needle in ("PUSH_ONLY", "doc-a", str(REV), "1.2.0", FEEDBACK, "<feedback>", "코드 수정 금지",
                   "source_task_id", "change_summary", "CHANGE_REQUEST_KEY"):
        assert needle in instruction
    assert conn.jobs[0][4] == SESSION


def test_change_request_unapproved_does_not_archive(monkeypatch):
    conn = CRConn(approved=False)
    assert call(conn, monkeypatch)["archived"] is False
    assert not any("approved_revision_id=NULL" in s for s in conn.sqls)


def test_change_request_conflicts(monkeypatch):
    with pytest.raises(HTTPException) as e:
        call(CRConn(latest=UUID("33333333-3333-3333-3333-333333333333")), monkeypatch)
    assert code(e) == (409, "revision_not_latest")
    with pytest.raises(HTTPException) as e:
        call(CRConn(generation=9), monkeypatch)
    assert code(e) == (409, "generation_conflict")


def test_change_request_unsupported_project_rolls_back_archive(monkeypatch):
    monkeypatch.setattr(submit, "SUPPORTED_PROJECTS", frozenset({"KIS"}))
    conn = CRConn()
    with pytest.raises(HTTPException) as e:
        call(conn, monkeypatch)
    assert code(e) == (422, "change_request_project_unsupported")
    assert conn.rolled_back and conn.state["approved"] == REV and conn.jobs == []


def test_change_request_idempotent_within_ten_minutes(monkeypatch):
    conn = CRConn(previous_job="runner-abc12345", generation=5)  # 첫 요청이 generation 을 올렸어도 기존 job 반환
    result = call(conn, monkeypatch, generation=4)
    assert result == {"job_id": "runner-abc12345", "archived": False, "idempotent": True}
    assert conn.jobs == [] and not any("approved_revision_id=NULL" in s for s in conn.sqls)
    key = docs.change_request_key(REV, FEEDBACK)
    assert key != docs.change_request_key(REV, FEEDBACK + "!") and len(key) == 64


def test_change_request_feedback_length_and_secret():
    for bad in ("짧음", "x" * 2001):
        with pytest.raises(ValueError):
            docs.ChangeRequestInput(revision_id=REV, expected_generation=1, feedback=bad)


def test_change_request_calls_human_check_and_routes_precede_key_route():
    import inspect
    assert "_require_human_approver(context)" in inspect.getsource(docs.change_request)
    paths = [r.path for r in docs.router.routes if "GET" in getattr(r, "methods", ())]
    assert paths.index(docs.router.prefix + "/auto-approval-digest") < paths.index(docs.router.prefix + "/{document_key}")
    assert any(r.path.endswith("/{document_key}/change-request") and "POST" in r.methods for r in docs.router.routes)


def test_since_hours_out_of_range_is_422():
    app = FastAPI()
    app.include_router(docs.router, prefix="/api/v1")
    app.dependency_overrides[docs.VIEW.dependency] = lambda: human()
    client = TestClient(app)
    base = "/api/v1/projects/AADS/documents/auto-approval-digest"
    for value in (0, 169, -1):
        assert client.get(base, params={"since_hours": value}).status_code == 422


def test_digest_api_returns_list_and_mode_without_tenant(monkeypatch):
    conn = AutoConn([approved_row()])
    monkeypatch.setenv("DOC_AUTO_APPROVE", "off")

    class Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return conn

        async def __aexit__(self, *_):
            return False
    monkeypatch.setattr(docs, "get_pool", lambda: Pool())

    async def allow(*a, **k):
        return TENANT, "user-1"
    monkeypatch.setattr(docs, "_authorize", allow)
    out = asyncio.run(docs.auto_approval_digest("AADS", 24, human()))
    assert out["mode"] == "off" and out["counts"]["auto_approved"] == 1
    assert "tenant_id" not in out["auto_approved"][0] and SECRET_BODY not in json.dumps(out, default=str)


# ── runner-b7804677 반려 지적 4건 회귀 ──

def test_tool_registration_path_schedules_cross_review():
    # [P1] 도구 등록 경로(canonical_document_tools)도 HTTP 와 같은 커밋 후 예약 함수를 부른다.
    import inspect
    from app.services import canonical_document_tools as tools
    from app.services import spec_drift
    src = inspect.getsource(tools.register_document)
    assert "_schedule_cross_review_after_commit(tenant_id, project, result)" in src
    assert src.index("create_revision_in_tx") < src.index("_schedule_cross_review_after_commit")
    assert "_schedule_cross_review_after_commit" in inspect.getsource(spec_drift.register_spec_drift)


def test_schedule_after_commit_skips_d1_idempotent_and_off(monkeypatch):
    from app.services import doc_cross_review as cross
    called = []
    monkeypatch.setattr(cross, "schedule", lambda *a, **k: called.append(a))
    monkeypatch.setenv("DOC_AUTO_APPROVE", "on")
    asyncio.run(docs._schedule_cross_review_after_commit(TENANT, "AADS", {"idempotent": True, "doc_tier": "D2"}))
    asyncio.run(docs._schedule_cross_review_after_commit(TENANT, "AADS", {"idempotent": False, "doc_tier": "D1"}))
    monkeypatch.setenv("DOC_AUTO_APPROVE", "off")
    asyncio.run(docs._schedule_cross_review_after_commit(TENANT, "AADS", {"idempotent": False, "doc_tier": "D2"}))
    assert called == []


def test_held_sql_excludes_skip_targets_before_limit():
    # [P1] skip 대상(목표 미연결·report·migration-apply)이 상한을 차지하지 않는다.
    sql = digest._HELD_SQL
    assert "project_document_goal_links" in sql and "'report'" in sql and "migration-apply-%" in sql
    assert sql.index("project_document_goal_links") < sql.index("LIMIT")


def test_truncation_is_reported_not_silent(monkeypatch):
    monkeypatch.setattr(digest, "_MAX_AUTO_ROWS", 2)
    monkeypatch.setattr(digest, "_MAX_HELD_CANDIDATES", 1)
    cand = [{"head_id": i, "tenant_id": TENANT, "project_key": "AADS", "revision_id": REV} for i in range(2)]
    conn = AutoConn([approved_row(key=f"d{i}") for i in range(3)], cand)
    result = fetch(conn, monkeypatch, ctxs=[ctx_for("hold", ["major_present"])])
    assert len(result["auto_approved"]) == 2 and result["truncated"] == {"auto_approved": True, "held": True}
    assert "목록 상한" in digest.format_digest([], result)


def test_daily_digest_queries_each_tenant_separately(monkeypatch):
    # [P1] 일일 요약은 tenant_id=None 전체 조회가 아니라 테넌트마다 따로 조회한다.
    calls = []

    async def fake_fetch(conn, tenant_id, project_key=None, *, since_hours=24):
        calls.append(tenant_id)
        return {"mode": "on", "auto_approved": [], "held": [{"title": "t", "project_key": "AADS", "document_key": "k",
                                                            "risk": "normal", "hold_reasons": ["x"]}],
                "would_approve": [], "truncated": {}}
    monkeypatch.setattr(digest, "fetch_auto_approval_digest", fake_fetch)
    t2 = "00000000-0000-0000-0000-000000000002"

    class Conn:
        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def fetch(self, sql, *a):
            if sql is digest._AUTO_TENANTS_SQL:
                return [{"tenant_id": TENANT}, {"tenant_id": t2}]
            return []

        async def fetchval(self, *a):
            return None

        async def execute(self, *a):
            pass

    class Pool:
        def acquire(self):
            return Conn()
    sent = []

    async def sender(text):
        sent.append(text)
        return True
    asyncio.run(digest.send_daily_digest(Pool(), now=NOW, sender=sender))
    assert calls == [TENANT, t2] and None not in calls
    assert len(sent) == 2


def test_off_mode_still_reports_held_but_never_would_approve(monkeypatch):
    # [P2] off 여도 보류 문서·사유는 보인다. would_approve 는 shadow 전용.
    cand = [{"head_id": 1, "tenant_id": TENANT, "project_key": "AADS", "revision_id": REV}] * 2
    result = fetch(AutoConn([], cand), monkeypatch, "off", [ctx_for("hold", ["blocker_present"]), ctx_for("approve")])
    assert result["mode"] == "off" and len(result["held"]) == 1 and result["would_approve"] == []


def test_auto_approved_history_kept_after_cancel(monkeypatch):
    # [P2] 자동승인 뒤 취소·포인터 변경된 판본도 이력에 남고 currently_approved 로 구분한다.
    assert "IS NOT DISTINCT FROM e.revision_id) AS currently_approved" in digest._AUTO_SQL
    assert "AND h.approved_revision_id IS NOT DISTINCT FROM e.revision_id " not in digest._AUTO_SQL
    cancelled = approved_row(key="cancelled")
    cancelled["currently_approved"] = False
    result = fetch(AutoConn([approved_row(), cancelled]), monkeypatch)
    assert [(i["document_key"], i["currently_approved"]) for i in result["auto_approved"]] == [("doc-a", True), ("cancelled", False)]
