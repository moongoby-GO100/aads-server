"""오비스 알림 모아보기 API(/api/v1/inbox) 단위 테스트.

정본 PRD ohvis-notification-inbox-prd 의 완료 기준 1번: 탭 분류, 러너 job 묶기, 경보 24시간 묶기,
확인 처리 멱등, 출처 하나 실패 시 degraded 응답, 비관리자 403.

DB 를 태우지 않는다. 풀 대신 `FakePool` 이 SQL 의 종류만 구분해 미리 넣어 둔 행을 돌려주고,
UPDATE/INSERT 는 메모리 상태에 반영한다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import inbox
from app.auth import get_current_user

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
NOW = datetime.now(timezone.utc)
APPROVAL_ID = "11111111-1111-4111-8111-111111111111"
NOTIFY_ID = "22222222-2222-4222-8222-222222222222"
NOTIFY_ID_2 = "33333333-3333-4333-8333-333333333333"


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


def alert(id_, *, sev="WARNING", title="디스크 사용량 초과", category="disk_full", project=None, at=None, acked=False):
    return {"id": id_, "severity": sev, "category": category, "title": title, "message": f"메시지 {id_}",
            "project": project, "acknowledged": acked, "created_at": at or ago(minutes=id_)}


def event(id_, job_id, event_type, status, *, at, project="AADS", phase="x", verdict=None):
    return {"id": id_, "job_id": job_id, "project": project, "event_type": event_type, "status": status,
            "phase": phase, "verdict": verdict, "observed_at": at, "session_id": "sess-1", "job_status": None,
            "instruction_hash": f"hash-{job_id}", "job_created_at": at - timedelta(minutes=5),
            "task_title": f"작업 {job_id}"}


class FakePool:
    def __init__(self):
        self.alerts: list[dict] = []
        self.approvals: list[dict] = []
        self.notifies: list[dict] = []
        self.ohvis: list[dict] = []
        self.events: list[dict] = []
        self.successes: list[dict] = []
        self.marks: dict[tuple, datetime] = {}
        self.sql: list[str] = []

    # ── 읽기 ──
    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "FROM alert_history" in sql:
            return [dict(r) for r in self.alerts]
        if "FROM agent_permission_requests r" in sql:
            rows = self.approvals if "tier='approve'" in sql else self.notifies
            return [dict(r) for r in rows]
        if "FROM ohvis_notifications" in sql:
            return [dict(r) for r in self.ohvis]
        if "FROM pipeline_runner_events" in sql:
            assert "model_attempt_started" not in args[1] and "cli_process_started" not in args[1]
            return [dict(r) for r in self.events]
        if "FROM pipeline_jobs" in sql:
            return [dict(r) for r in self.successes]
        if "FROM ohvis_inbox_read_marks" in sql:
            _tenant, user, source, ids = args
            return [{"source_id": i, "read_at": self.marks[(user, source, i)]}
                    for i in ids if (user, source, i) in self.marks]
        raise AssertionError(f"예상 못 한 fetch: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        if "FROM alert_history" in sql:
            return {"total": len(self.alerts), "unread": sum(1 for a in self.alerts if not a["acknowledged"])}
        if "FROM agent_permission_requests" in sql:
            return {"total": len(self.notifies), "unread": sum(1 for n in self.notifies if n["decision"] == "notified")}
        raise AssertionError(f"예상 못 한 fetchrow: {sql[:80]}")

    # ── 쓰기 ──
    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO ohvis_inbox_read_marks"):
            _tenant, user, source, ids = args
            for i in ids:
                self.marks[(user, source, i)] = datetime.now(timezone.utc)
            return f"INSERT 0 {len(ids)}"
        if sql.startswith("UPDATE alert_history a"):
            changed = 0
            for rep in [a for a in self.alerts if a["id"] in args[0]]:
                for a in self.alerts:
                    same = (a["category"], a["title"], a["project"]) == (rep["category"], rep["title"], rep["project"])
                    if same and not a["acknowledged"] and rep["created_at"] - timedelta(hours=24) < a["created_at"] <= rep["created_at"]:
                        a["acknowledged"] = True
                        changed += 1
            return f"UPDATE {changed}"
        if sql.startswith("UPDATE alert_history SET"):
            before, critical_only = args
            hit = [a for a in self.alerts if not a["acknowledged"] and a["created_at"] <= before
                   and (not critical_only or a["severity"].lower() == "critical")]
            for a in hit:
                a["acknowledged"] = True
            return f"UPDATE {len(hit)}"
        if sql.startswith("UPDATE agent_permission_requests"):
            if "id = ANY" in sql:
                hit = [n for n in self.notifies if n["id"] in args[0] and n["decision"] == "notified"]
            else:
                hit = [n for n in self.notifies if n["decision"] == "notified" and n["created_at"] <= args[2]]
            for n in hit:
                n["decision"] = "acknowledged"
            return f"UPDATE {len(hit)}"
        if sql.startswith("UPDATE ohvis_notifications"):
            if "id = ANY" in sql:
                hit = [o for o in self.ohvis if o["id"] in args[0] and o["read_at"] is None]
            else:
                hit = [o for o in self.ohvis if o["read_at"] is None and o["created_at"] <= args[2]]
            for o in hit:
                o["read_at"] = NOW
            return f"UPDATE {len(hit)}"
        raise AssertionError(f"예상 못 한 execute: {sql[:80]}")


def approval(id_=APPROVAL_ID, at=None):
    return {"id": id_, "action_type": "run_remote_command", "action_summary": "운영 서버 재시작 요청\n상세",
            "risk_level": "high", "gate_source": "gate", "decision": "pending",
            "created_at": at or ago(hours=3), "project": "GO100"}


def notify(id_=NOTIFY_ID, decision="notified", at=None):
    return {"id": id_, "action_type": "patch_remote_file", "action_summary": "프롬프트 변경", "risk_level": "medium",
            "gate_source": "goal", "decision": decision, "created_at": at or ago(hours=1), "project": "AADS"}


def make_client(pool: FakePool, monkeypatch, role="admin") -> TestClient:
    monkeypatch.setattr(inbox, "get_pool", lambda: pool)
    app = FastAPI()
    app.include_router(inbox.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "u-admin", "current_tenant": {"id": TENANT}, "current_membership": {"role": role},
    }
    return TestClient(app)


@pytest.fixture
def pool() -> FakePool:
    p = FakePool()
    p.alerts = [
        alert(1, sev="CRITICAL", title="디스크 사용량 초과", at=ago(minutes=10)),
        alert(2, sev="critical", title="디스크 사용량 초과", at=ago(hours=2)),
        alert(3, sev="warning", title="비용 초과", category="cost_exceed", at=ago(minutes=30)),
        alert(4, sev="info", title="완료 확인", category="goal", project="GO100", at=ago(hours=5), acked=True),
    ]
    p.approvals = [approval()]
    p.notifies = [notify(), notify(NOTIFY_ID_2, "acknowledged", ago(days=2))]
    p.ohvis = [{"id": 7, "project_key": "AADS", "kind": "review", "title": "시안 검토 요청", "body": "본문",
                "link": "/projects/AADS/mockups", "created_at": ago(hours=4), "read_at": None}]
    p.events = [
        event(1, "runner-aaa", "approval_requested", "awaiting_approval", at=ago(minutes=20)),
        event(2, "runner-bbb", "job_terminal", "error", at=ago(minutes=30), project="NTV2"),
        event(3, "runner-ccc", "job_terminal", "error", at=ago(minutes=40), project="GO100"),
        event(4, "runner-ddd", "job_terminal", "done", at=ago(minutes=50)),
        event(5, "runner-eee", "job_started", "running", at=ago(minutes=2)),
        event(6, "runner-eee", "model_attempt_started", "running", at=ago(minutes=1)),
        event(7, "runner-ddd", "job_started", "running", at=ago(minutes=60)),
    ]
    # runner-ccc 는 같은 지시문 해시로 뒤에 성공한 job 이 있다 → 처리 필요에서 빠져야 한다.
    p.successes = [{"job_id": "runner-zzz", "project": "GO100", "instruction_hash": "hash-runner-ccc",
                    "created_at": ago(minutes=10), "rework_of": None}]
    return p


def keys(items):
    return {(i["source"], i["source_id"]) for i in items}


# ── 순수 함수 ────────────────────────────────────────────────────────


def test_alerts_group_within_24h_and_split_beyond():
    t = NOW
    rows = [
        alert(10, title="A", at=t),
        alert(11, title="A", at=t - timedelta(hours=23, minutes=59)),
        alert(12, title="A", at=t - timedelta(hours=30)),
        alert(13, title="B", at=t),
        alert(14, title="A", project="GO100", at=t),
    ]
    items = inbox.group_alerts(rows)
    by_id = {i.source_id: i for i in items}
    assert set(by_id) == {"10", "12", "13", "14"}
    assert by_id["10"].count == 2 and by_id["12"].count == 1
    assert by_id["10"].link == "/decisions?alert=10"


def test_alert_group_severity_normalized_lowercase_and_highest_wins():
    items = inbox.group_alerts([alert(1, sev="WARNING", at=NOW), alert(2, sev="CRITICAL", at=NOW - timedelta(hours=1))])
    assert len(items) == 1 and items[0].severity == "critical"
    assert inbox.normalize_severity("CRITICAL") == inbox.normalize_severity("critical") == "critical"


def test_alert_group_unread_if_any_member_unacked():
    items = inbox.group_alerts([alert(1, at=NOW, acked=True), alert(2, at=NOW - timedelta(hours=1), acked=False)])
    assert items[0].read is False
    items = inbox.group_alerts([alert(1, at=NOW, acked=True), alert(2, at=NOW - timedelta(hours=1), acked=True)])
    assert items[0].read is True


def test_runner_collapses_events_to_one_card_per_job():
    evs = [
        event(1, "runner-x", "job_started", "running", at=ago(minutes=30)),
        event(2, "runner-x", "ai_review_result", "running", at=ago(minutes=20), verdict="APPROVE"),
        event(3, "runner-x", "job_terminal", "done", at=ago(minutes=10)),
        event(4, "runner-x", "model_attempt_completed", "done", at=ago(minutes=1)),
        event(5, "runner-x", "cli_first_stdout", "running", at=ago(seconds=30)),
    ]
    cards = inbox.build_runner_cards(evs, [], {})
    assert len(cards) == 1
    assert cards[0].summary.startswith("완료")
    assert cards[0].link == "/chat?session=sess-1&job=runner-x"


def test_failed_job_is_superseded_by_later_success_of_same_task():
    failed = event(1, "runner-x", "job_terminal", "error", at=ago(minutes=30))
    failed["task_id"] = "TASK-1"
    later_ok = {"job_id": "runner-y", "project": "AADS", "instruction_hash": "other", "task_id": "TASK-1",
                "created_at": ago(minutes=5), "rework_of": None}
    earlier_ok = dict(later_ok, job_id="runner-w", created_at=ago(hours=2))
    other_project = dict(later_ok, job_id="runner-v", project="GO100")
    assert inbox.build_runner_cards([failed], [later_ok], {})[0].in_action is False
    assert inbox.build_runner_cards([failed], [earlier_ok, other_project], {})[0].in_action is True


def test_runner_read_mark_is_dropped_by_newer_state():
    ev_old = event(1, "runner-x", "job_started", "running", at=ago(minutes=30))
    marks = {"runner-x": ago(minutes=20)}
    assert inbox.build_runner_cards([ev_old], [], marks)[0].read is True
    ev_new = event(2, "runner-x", "job_terminal", "error", at=ago(minutes=5))
    assert inbox.build_runner_cards([ev_old, ev_new], [], marks)[0].read is False


# ── 탭 분류 ──────────────────────────────────────────────────────────


def test_action_tab_contents(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    body = c.get("/api/v1/inbox", params={"tab": "action"}).json()
    got = keys(body["items"])
    assert got == {
        ("approval", APPROVAL_ID),
        ("runner", "runner-aaa"),      # 결정 안 된 approval_requested
        ("runner", "runner-bbb"),      # 후속 성공 없는 실패
        ("alert", "1"),                # 미확인 critical 경보 묶음
    }
    assert body["degraded_sources"] == []
    assert all(i["tab"] == "action" for i in body["items"])
    # 오래된 것 먼저
    times = [i["occurred_at_kst"] for i in body["items"]]
    assert times == sorted(times)


def test_other_tabs(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    alerts = c.get("/api/v1/inbox", params={"tab": "alert"}).json()["items"]
    assert keys(alerts) == {("alert", "1"), ("alert", "3"), ("alert", "4")}
    assert {i["severity"] for i in alerts} <= {"critical", "warning", "info"}
    change = c.get("/api/v1/inbox", params={"tab": "change"}).json()["items"]
    assert keys(change) == {("notify", NOTIFY_ID), ("notify", NOTIFY_ID_2), ("ohvis_notification", "7")}
    runner = c.get("/api/v1/inbox", params={"tab": "runner"}).json()["items"]
    ids = [i["source_id"] for i in runner]
    assert sorted(ids) == ["runner-aaa", "runner-bbb", "runner-ccc", "runner-ddd", "runner-eee"]
    assert len(ids) == len(set(ids))


def test_item_schema_and_links(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    items = {(i["source"], i["source_id"]): i for i in c.get("/api/v1/inbox", params={"tab": "action"}).json()["items"]}
    appr = items[("approval", APPROVAL_ID)]
    assert set(appr) == {"source", "source_id", "tab", "project", "severity", "title", "summary",
                         "occurred_at_kst", "count", "read", "link", "actions"}
    assert appr["link"] == f"/approvals?focus={APPROVAL_ID}"
    assert appr["title"] == "운영 서버 재시작 요청" and appr["project"] == "GO100"
    assert items[("runner", "runner-aaa")]["link"] == "/chat?session=sess-1&job=runner-aaa"
    assert items[("alert", "1")]["link"] == "/decisions?alert=1"
    assert items[("alert", "1")]["count"] == 2
    assert appr["occurred_at_kst"].endswith("+09:00")


def test_filters_and_pagination(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    only = c.get("/api/v1/inbox", params={"tab": "alert", "severity": "CRITICAL"}).json()["items"]
    assert keys(only) == {("alert", "1")}
    only = c.get("/api/v1/inbox", params={"tab": "runner", "project": "ntv2"}).json()["items"]
    assert keys(only) == {("runner", "runner-bbb")}
    seen, cursor = [], ""
    for _ in range(10):
        page = c.get("/api/v1/inbox", params={"tab": "runner", "limit": 2, "cursor": cursor}).json()
        seen += [i["source_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == ["runner-aaa", "runner-bbb", "runner-ccc", "runner-ddd", "runner-eee"]
    assert c.get("/api/v1/inbox", params={"tab": "runner", "cursor": "!!"}).status_code == 422
    assert c.get("/api/v1/inbox", params={"tab": "nope"}).status_code == 422


def test_summary_alert_unread_matches_raw_count(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    body = c.get("/api/v1/inbox/summary").json()
    raw_unread = sum(1 for a in pool.alerts if not a["acknowledged"])
    assert body["tabs"]["alert"] == {"total": len(pool.alerts), "unread": raw_unread}
    assert body["tabs"]["action"]["unread"] == 4
    assert body["tabs"]["change"] == {"total": 3, "unread": 2}
    assert body["degraded_sources"] == []


# ── 확인 처리 ────────────────────────────────────────────────────────


def test_pending_approval_stays_in_action_but_badge_drops(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    before = c.get("/api/v1/inbox/summary").json()["tabs"]["action"]["unread"]
    r = c.post("/api/v1/inbox/read", json={"items": [{"source": "approval", "source_id": APPROVAL_ID}]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get("/api/v1/inbox/summary").json()["tabs"]["action"]["unread"] == before - 1
    items = c.get("/api/v1/inbox", params={"tab": "action"}).json()["items"]
    appr = next(i for i in items if i["source"] == "approval")
    assert appr["read"] is True and appr["actions"] == ["open"]
    assert c.get("/api/v1/inbox", params={"tab": "action", "unread_only": "true"}).json()["items"]


def test_read_is_idempotent_for_every_source(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    payload = {"items": [
        {"source": "alert", "source_id": "1"},
        {"source": "alert", "source_id": "1"},
        {"source": "notify", "source_id": NOTIFY_ID},
        {"source": "ohvis_notification", "source_id": "7"},
        {"source": "runner", "source_id": "runner-bbb"},
    ]}
    first = c.post("/api/v1/inbox/read", json=payload)
    snapshot = ({a["id"]: a["acknowledged"] for a in pool.alerts}, [n["decision"] for n in pool.notifies],
                [o["read_at"] is None for o in pool.ohvis], set(pool.marks))
    second = c.post("/api/v1/inbox/read", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {
        "ok": True, "marked": {"alert": 1, "notify": 1, "ohvis_notification": 1, "runner": 1}, "failed_sources": []}
    assert snapshot == ({a["id"]: a["acknowledged"] for a in pool.alerts}, [n["decision"] for n in pool.notifies],
                        [o["read_at"] is None for o in pool.ohvis], set(pool.marks))
    # 경보 묶음(1,2)은 통째로 확인되고, 다른 경보는 그대로다.
    assert {a["id"]: a["acknowledged"] for a in pool.alerts} == {1: True, 2: True, 3: False, 4: True}
    assert pool.notifies[0]["decision"] == "acknowledged"
    assert pool.ohvis[0]["read_at"] is not None
    # 확인한 critical 경보는 처리 필요에서 빠진다.
    action = c.get("/api/v1/inbox", params={"tab": "action"}).json()["items"]
    assert ("alert", "1") not in keys(action)


def test_read_rejects_unknown_source_and_bad_ids(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    assert c.post("/api/v1/inbox/read", json={"items": [{"source": "x", "source_id": "1"}]}).status_code == 422
    assert c.post("/api/v1/inbox/read", json={"items": [{"source": "alert", "source_id": "1; DROP"}]}).status_code == 422
    assert c.post("/api/v1/inbox/read", json={"items": [{"source": "approval", "source_id": "nope"}]}).status_code == 422
    assert c.post("/api/v1/inbox/read", json={"items": []}).status_code == 422


def test_read_all_change_tab_is_bounded_and_repeatable(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    body = {"tab": "change", "before": NOW.isoformat()}
    first = c.post("/api/v1/inbox/read-all", json=body).json()
    again = c.post("/api/v1/inbox/read-all", json=body).json()
    assert first["marked"] == {"notify": 1, "ohvis_notification": 1}
    assert again["marked"] == {"notify": 0, "ohvis_notification": 0}
    assert c.post("/api/v1/inbox/read-all", json={"tab": "zzz", "before": NOW.isoformat()}).status_code == 422


def test_read_all_respects_before_cutoff(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    cutoff = (NOW - timedelta(hours=1, minutes=30)).isoformat()
    out = c.post("/api/v1/inbox/read-all", json={"tab": "alert", "before": cutoff}).json()
    # 3시간 전 이전 미확인 경보는 없고(id=2 는 2시간 전), 1시간30분 이전은 id=2 만이다.
    assert out["marked"] == {"alert": 1}
    assert [a["acknowledged"] for a in pool.alerts] == [False, True, False, True]


def test_read_all_action_tab_keeps_pending_approval_visible(pool, monkeypatch):
    c = make_client(pool, monkeypatch)
    out = c.post("/api/v1/inbox/read-all", json={"tab": "action", "before": NOW.isoformat()}).json()
    assert out["ok"] is True
    assert out["marked"]["approval"] == 1 and out["marked"]["runner"] == 2 and out["marked"]["alert"] == 2
    assert pool.notifies[0]["decision"] == "notified"
    items = c.get("/api/v1/inbox", params={"tab": "action"}).json()["items"]
    assert ("approval", APPROVAL_ID) in keys(items)


# ── degraded / 권한 ─────────────────────────────────────────────────


def test_one_source_failure_returns_others_with_degraded(pool, monkeypatch):
    async def boom(_pool, _ctx):
        raise RuntimeError("db down")

    monkeypatch.setattr(inbox, "load_runner", boom)
    c = make_client(pool, monkeypatch)
    body = c.get("/api/v1/inbox", params={"tab": "action"}).json()
    assert body["degraded_sources"] == ["pipeline_runner_events"]
    assert keys(body["items"]) == {("approval", APPROVAL_ID), ("alert", "1")}
    summary = c.get("/api/v1/inbox/summary").json()
    assert summary["degraded_sources"] == ["pipeline_runner_events"]
    assert summary["tabs"]["alert"]["unread"] == 3


def test_slow_source_times_out_into_degraded(pool, monkeypatch):
    async def slow(_pool, _ctx):
        await asyncio.sleep(5)

    monkeypatch.setattr(inbox, "load_alerts", slow)
    monkeypatch.setattr(inbox, "SOURCE_TIMEOUT_SEC", 0.05)
    c = make_client(pool, monkeypatch)
    body = c.get("/api/v1/inbox", params={"tab": "change"}).json()
    assert body["degraded_sources"] == ["alert_history"]
    assert len(body["items"]) == 3


def test_all_sources_down_still_answers_200(pool, monkeypatch):
    async def boom(_pool, _ctx):
        raise RuntimeError("down")

    for name in ("load_alerts", "load_approvals", "load_notifies", "load_ohvis_notifications", "load_runner"):
        monkeypatch.setattr(inbox, name, boom)
    c = make_client(pool, monkeypatch)
    body = c.get("/api/v1/inbox/summary").json()
    assert len(body["degraded_sources"]) == 5
    assert all(t == {"total": 0, "unread": 0} for t in body["tabs"].values())


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/api/v1/inbox/summary", {}),
    ("get", "/api/v1/inbox", {}),
    ("post", "/api/v1/inbox/read", {"json": {"items": [{"source": "alert", "source_id": "1"}]}}),
    ("post", "/api/v1/inbox/read-all", {"json": {"tab": "alert", "before": "2026-10-09T00:00:00+09:00"}}),
])
@pytest.mark.parametrize("role", ["member", "viewer"])
def test_non_admin_gets_403(pool, monkeypatch, method, path, kwargs, role):
    c = make_client(pool, monkeypatch, role=role)
    assert getattr(c, method)(path, **kwargs).status_code == 403
    assert pool.sql == [] and not pool.marks


def test_owner_is_allowed(pool, monkeypatch):
    c = make_client(pool, monkeypatch, role="owner")
    assert c.get("/api/v1/inbox/summary").status_code == 200
