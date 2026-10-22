"""클로브 권한 오판 방지: 일반 tool_error 는 permission_ok 를 바꾸지 않고, FALSE 회사는 간격마다 재확인한다.

실 DB·실 클로브 호출은 없다. DB 연결과 세션은 가짜로 대체한다.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_scheduler as sched
from app.services import obys_collection_service as svc

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CID = "cid-danharu"
TENANT = "00000000-0000-0000-0000-000000000001"


def run(coro):
    return asyncio.run(coro)


def _err(text):
    return {"isError": True, "content": [{"type": "text", "text": text}]}


class FakeConn:
    def __init__(self, link=None):
        self.link = link
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchrow(self, sql, *args):
        return self.link

    async def close(self):
        pass


class FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, tool, args=None):
        self.calls.append((tool, args))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {"assets": []}


def _ctx():
    return svc.RunCtx(CID, TENANT, "biz-1", "inst", 1, 300, "live")


def _sets_false(conn):
    return [e for e in conn.executed if "permission_ok=FALSE" in e[0]]


# ── 1. 클라이언트: 분류와 마스킹 ──────────────────────────

def test_generic_is_error_is_tool_error_not_permission_and_keeps_text():
    with pytest.raises(clobe.ClobeToolError) as e:
        clobe.parse_tool_json(_err("Internal error while loading assets"))
    assert not isinstance(e.value, clobe.ClobePermissionDenied)
    assert str(e.value) == "tool_error"
    assert e.value.detail == "Internal error while loading assets"


@pytest.mark.parametrize("text", [
    "Permission denied for company", "403 Forbidden", "User is not a member of this company",
    "해당 회사에 대한 권한이 없습니다", "Access denied", "접근 권한 없음",
])
def test_explicit_permission_denial_is_classified(text):
    with pytest.raises(clobe.ClobePermissionDenied) as e:
        clobe.parse_tool_json(_err(text))
    assert isinstance(e.value, clobe.ClobeError)
    assert e.value.detail == text


def test_error_detail_is_cut_to_200_chars_and_masks_secrets():
    secret = "sk-ant-oat01-" + "A1b2C3d4" * 6
    text = f"upstream failed Bearer abc.def.ghi token={secret} code=XYZ123 " + "x " * 300
    with pytest.raises(clobe.ClobeToolError) as e:
        clobe.parse_tool_json(_err(text))
    d = e.value.detail
    assert len(d) <= 200
    assert secret not in d and "abc.def.ghi" not in d and "XYZ123" not in d
    assert "Bearer ***" in d


def test_is_error_without_text_still_raises_tool_error():
    with pytest.raises(clobe.ClobeToolError) as e:
        clobe.parse_tool_json({"isError": True, "content": []})
    assert e.value.detail == ""


# ── 2. _preflight: 권한 거부만 FALSE ───────────────────────

def test_a_generic_tool_error_keeps_permission_ok_and_fails_only_this_round():
    conn = FakeConn()
    session = FakeSession(clobe.ClobeToolError("Internal error"))
    with pytest.raises(svc.CollectionError) as e:
        run(svc._preflight(conn, session, _ctx()))
    assert e.value.code == "clobe_tool_error"
    assert conn.executed == []


def test_a_other_clobe_errors_do_not_flip_permission_either():
    conn = FakeConn()
    with pytest.raises(svc.CollectionError):
        run(svc._preflight(conn, FakeSession(clobe.ClobeError("tool_unexpected_shape")), _ctx()))
    assert conn.executed == []


def test_b_explicit_permission_denial_sets_false_with_reason():
    conn = FakeConn()
    session = FakeSession(clobe.ClobePermissionDenied("User is not a member of this company"))
    with pytest.raises(svc.CollectionError) as e:
        run(svc._preflight(conn, session, _ctx()))
    assert e.value.code == "permission_denied"
    (sql, args), = _sets_false(conn)
    assert args == (CID, "permission_denied:User is not a member of this company")


def test_preflight_success_sets_true():
    conn = FakeConn()
    run(svc._preflight(conn, FakeSession(None), _ctx()))
    assert len(conn.executed) == 1 and "permission_ok=TRUE" in conn.executed[0][0]


def test_reauth_and_transient_in_preflight_pass_through_untouched():
    for exc in (clobe.ClobeReauthRequired("reauth"), clobe.ClobeTransientError("net")):
        conn = FakeConn()
        with pytest.raises(type(exc)):
            run(svc._preflight(conn, FakeSession(exc), _ctx()))
        assert conn.executed == []


# ── 3. recheck_company_permission ─────────────────────────

def _link(**kw):
    base = {"clobe_company_id": CID, "link_status": "linked", "tenant_id": TENANT, "business_id": "biz-1",
            "permission_ok": False, "permission_error": "permission_denied:tool_error"}
    return {**base, **kw}


@pytest.fixture
def conn_for(monkeypatch):
    def make(link):
        conn = FakeConn(link)

        async def connect():
            return conn

        monkeypatch.setattr(svc, "_connect", connect)
        monkeypatch.setattr(svc.core, "tenant_in_scope", lambda t: True)
        return conn
    return make


def test_recheck_success_restores_true(conn_for):
    conn = conn_for(_link())
    session = FakeSession(None)
    res = run(svc.recheck_company_permission(CID, session_factory=lambda: session))
    assert res["status"] == "recovered"
    assert session.calls == [("get_scraping_status", {"companyId": CID})]
    (sql, args), = conn.executed
    assert "permission_ok=TRUE" in sql and "permission_error=NULL" in sql and args == (CID,)


def test_recheck_failure_stays_false_and_counts_consecutive_failures(conn_for):
    conn = conn_for(_link(permission_error="permission_denied:x;recheck_fail=2"))
    res = run(svc.recheck_company_permission(
        CID, session_factory=lambda: FakeSession(clobe.ClobePermissionDenied("forbidden"))))
    assert (res["status"], res["fails"]) == ("still_failing", 3)
    (sql, args), = conn.executed
    assert "permission_ok=TRUE" not in sql and "permission_checked_at=now()" in sql
    assert args == (CID, "permission_denied:forbidden;recheck_fail=3")


def test_recheck_general_tool_error_also_counts_but_never_sets_true(conn_for):
    conn = conn_for(_link())
    res = run(svc.recheck_company_permission(
        CID, session_factory=lambda: FakeSession(clobe.ClobeToolError("Internal error"))))
    assert (res["status"], res["fails"]) == ("still_failing", 1)
    assert conn.executed[0][1] == (CID, "tool_error:Internal error;recheck_fail=1")


@pytest.mark.parametrize("exc", [clobe.ClobeReauthRequired("r"), clobe.ClobeTransientError("t")])
def test_recheck_reauth_or_transient_changes_nothing(conn_for, exc):
    conn = conn_for(_link())
    with pytest.raises(type(exc)):
        run(svc.recheck_company_permission(CID, session_factory=lambda: FakeSession(exc)))
    assert conn.executed == []


@pytest.mark.parametrize("link", [
    _link(permission_ok=None), _link(permission_ok=True), _link(permission_error=svc.ABSENT_ERROR),
    _link(link_status="blocked"),
])
def test_recheck_skips_rows_that_are_not_false_denials(conn_for, link):
    conn = conn_for(link)
    session = FakeSession(None)
    assert run(svc.recheck_company_permission(CID, session_factory=lambda: session))["status"] == "skipped"
    assert session.calls == [] and conn.executed == []


def test_recheck_fail_count_tag_roundtrip():
    assert svc.recheck_fail_count(None) == 0
    assert svc.recheck_fail_count("permission_denied:tool_error") == 0
    tagged = svc._with_recheck_fail("permission_denied:x;recheck_fail=4", 5)
    assert tagged == "permission_denied:x;recheck_fail=5" and svc.recheck_fail_count(tagged) == 5


# ── 4. 스케줄러: 간격·복귀·백오프 ──────────────────────────

@pytest.fixture
def world(monkeypatch):
    for k in (sched.RECHECK_HOURS_ENV, sched.RECHECK_MAX_FAILS_ENV):
        monkeypatch.delenv(k, raising=False)
    w = type("W", (), {})()
    w.rows = {}          # company_id -> {"ok": bool|None, "checked_at", "error"}
    w.rechecked, w.collected = [], []
    w.recheck_outcome = {}  # company_id -> "ok" | Exception

    async def load_recheck_candidates():
        return [{"clobe_company_id": c, "permission_checked_at": r["checked_at"], "permission_error": r["error"]}
                for c, r in sorted(w.rows.items()) if r["ok"] is False]

    async def recheck(company_id, **kw):
        w.rechecked.append(company_id)
        out = w.recheck_outcome.get(company_id, "ok")
        if isinstance(out, Exception):
            raise out
        r = w.rows[company_id]
        if out == "ok":
            r.update(ok=True, error=None, checked_at=NOW)
            return {"status": "recovered", "fails": 0}
        fails = svc.recheck_fail_count(r["error"]) + 1
        r.update(error=svc._with_recheck_fail("permission_denied:x", fails), checked_at=NOW)
        return {"status": "still_failing", "fails": fails}

    async def load_state():
        return [{"clobe_company_id": c} for c, r in sorted(w.rows.items()) if r["ok"] is not False], {}

    async def run_collection(company_id, **kw):
        w.collected.append(company_id)
        return {"company_id": company_id, "status": "succeeded",
                "runs": [{"data_kind": k, "status": "succeeded", "error_code": None} for k in kw["kinds"]]}

    async def get_status():
        return {"reauth_required": False}

    monkeypatch.setattr(sched, "load_recheck_candidates", load_recheck_candidates)
    monkeypatch.setattr(sched, "load_state", load_state)
    monkeypatch.setattr(svc, "recheck_company_permission", recheck)
    monkeypatch.setattr(svc, "run_collection", run_collection)
    monkeypatch.setattr(clobe, "get_status", get_status)
    return w


def _false_row(hours_ago, error="permission_denied:tool_error"):
    return {"ok": False, "checked_at": NOW - timedelta(hours=hours_ago), "error": error}


def test_c_false_company_rechecked_after_interval_and_collected_next_round(world):
    world.rows[CID] = _false_row(100)  # 단하루: 4일 전 박힌 FALSE
    out = run(sched.run_due(NOW))
    assert world.rechecked == [CID]
    assert out["permission_rechecks"] == [{"company_id": CID, "status": "recovered", "fails": 0}]
    # 같은 회차에서 곧바로 수집 대상으로 복귀한다(load_state 가 재확인 뒤에 돈다)
    assert world.collected == [CID]
    out2 = run(sched.run_due(NOW + timedelta(hours=1)))
    assert world.collected == [CID, CID] and out2["permission_rechecks"] == []


def test_d_interval_not_elapsed_means_no_recheck(world):
    world.rows[CID] = _false_row(5.9)
    out = run(sched.run_due(NOW))
    assert world.rechecked == [] and world.collected == []
    assert out["permission_rechecks"] == []


def test_interval_boundary_and_env_override(world, monkeypatch):
    world.rows[CID] = _false_row(6)
    run(sched.run_due(NOW))
    assert world.rechecked == [CID]
    world.rechecked.clear()
    world.rows[CID] = _false_row(10)
    monkeypatch.setenv(sched.RECHECK_HOURS_ENV, "12")
    run(sched.run_due(NOW))
    assert world.rechecked == []


def test_interval_zero_or_negative_disables_recheck_and_garbage_falls_back(world, monkeypatch):
    world.rows[CID] = _false_row(1000)
    monkeypatch.setenv(sched.RECHECK_HOURS_ENV, "0")
    run(sched.run_due(NOW))
    assert world.rechecked == []
    monkeypatch.setenv(sched.RECHECK_HOURS_ENV, "abc")
    assert sched.recheck_interval() == timedelta(hours=6)


def test_still_failing_company_stays_out_of_collection_and_backs_off(world):
    world.rows[CID] = _false_row(7)
    world.recheck_outcome[CID] = "fail"
    out = run(sched.run_due(NOW))
    assert world.collected == []
    assert out["permission_rechecks"][0]["fails"] == 1
    # 방금 확인했으므로 다음 회차(1시간 뒤)에는 다시 부르지 않는다
    run(sched.run_due(NOW + timedelta(hours=1)))
    assert world.rechecked == [CID]
    # 1회 실패 뒤 간격은 12시간
    run(sched.run_due(NOW + timedelta(hours=11)))
    assert world.rechecked == [CID]
    run(sched.run_due(NOW + timedelta(hours=12)))
    assert world.rechecked == [CID, CID]


def test_backoff_doubles_and_stops_after_max_consecutive_failures():
    iv = timedelta(hours=6)
    t = NOW - timedelta(hours=1000)
    assert [sched.is_recheck_due(NOW - timedelta(hours=h), f, NOW, iv, 5)
            for h, f in ((6, 0), (11, 1), (12, 1), (23, 2), (24, 2), (47, 3), (48, 3), (95, 4), (96, 4))] == \
        [True, False, True, False, True, False, True, False, True]
    assert sched.is_recheck_due(t, 5, NOW, iv, 5) is False
    assert sched.is_recheck_due(None, 0, NOW, iv, 5) is True
    naive = (NOW - timedelta(hours=7)).replace(tzinfo=None)
    assert sched.is_recheck_due(naive, 0, NOW, iv, 5) is True


def test_failure_limit_env(world, monkeypatch):
    world.rows[CID] = _false_row(1000, error="permission_denied:x;recheck_fail=2")
    monkeypatch.setenv(sched.RECHECK_MAX_FAILS_ENV, "2")
    run(sched.run_due(NOW))
    assert world.rechecked == []
    monkeypatch.setenv(sched.RECHECK_MAX_FAILS_ENV, "3")
    run(sched.run_due(NOW))
    assert world.rechecked == [CID]


def test_connection_problem_stops_rechecks_without_blocking_collection(world):
    world.rows["a"] = _false_row(100)
    world.rows["b"] = _false_row(100)
    world.rows["ok"] = {"ok": None, "checked_at": None, "error": None}
    world.recheck_outcome["a"] = clobe.ClobeTransientError("net")
    out = run(sched.run_due(NOW))
    assert world.rechecked == ["a"]
    assert out["permission_rechecks"][0]["status"] == "recheck_unavailable"
    assert world.collected == ["ok"] and out["ok"] is True


def test_one_company_recheck_exception_does_not_block_the_next(world):
    world.rows["a"] = _false_row(100)
    world.rows["b"] = _false_row(100)
    world.recheck_outcome["a"] = RuntimeError("boom secret")
    out = run(sched.run_due(NOW))
    assert world.rechecked == ["a", "b"]
    assert [r["status"] for r in out["permission_rechecks"]] == ["recheck_error", "recovered"]
    assert world.collected == ["b"]


def test_candidate_load_failure_does_not_stop_collection(world, monkeypatch):
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(sched, "load_recheck_candidates", boom)
    world.rows["ok"] = {"ok": None, "checked_at": None, "error": None}
    out = run(sched.run_due(NOW))
    assert out["permission_rechecks"] == [] and world.collected == ["ok"]


def test_recheck_respects_the_run_budget(world):
    world.rows[CID] = _false_row(100)
    out = run(sched.recheck_permissions(NOW, time.monotonic() - 1))
    assert out == [] and world.rechecked == []
