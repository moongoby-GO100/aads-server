"""단방향 알림 인입구(POST /api/v1/session-notify) 회귀 테스트.

2026-09-29. 알림을 왕복 경로(`ask`)에 태우면 답이 완성되지 않아 영구히
failed/pending 으로 쌓인다(실측 530건 중 성공 11.5%). 알림 전용 길을 낸다.

DB 는 메모리 가짜 풀로 대신한다 — 실제 DB 를 건드리지 않으므로 정리할
데이터가 남지 않는다. 대상 인박스 전달(`_deliver_answer`)도 가짜로 바꿔
LLM 턴이 돌지 않게 한다.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import session_notify as notify_router
from app.services import session_relay

TENANT = "00000000-0000-0000-0000-0000000000aa"
WS = "11111111-1111-1111-1111-111111111111"
OPS = "22222222-2222-2222-2222-222222222222"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakePool:
    """이 모듈이 던지는 SQL 만 흉내 낸다."""

    def __init__(self):
        self.sessions = [
            {"id": OPS, "workspace_id": WS, "tenant_id": TENANT,
             "title": "운영인프라담당", "role_key": "OpsInfraOwner"},
        ]
        self.relays: list[dict] = []

    # pool.acquire() / conn.transaction()
    def acquire(self):
        pool = self

        class _Acq:
            async def __aenter__(self_inner):
                return pool

            async def __aexit__(self_inner, *a):
                return False

        return _Acq()

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if sql.startswith("INSERT INTO session_relay"):
            relay_id, origin, target, question, status = args
            self.relays.append({"id": relay_id, "origin": origin, "target": target,
                                "hop": 0, "question": question, "status": status})
            return "INSERT 0 1"
        if sql.startswith("UPDATE session_relay SET status='failed'"):
            for r in self.relays:
                if r["id"] == args[0]:
                    r["status"] = "failed"
                    r["error"] = args[1]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql}")

    async def fetch(self, sql, *args):
        if "SELECT DISTINCT s.workspace_id" in sql:
            target, tenant, sender_role = args
            ws = {s["workspace_id"] for s in self.sessions
                  if s["role_key"] == target and s["role_key"] != sender_role
                  and (tenant is None or s["tenant_id"] == tenant)}
            return [{"workspace_id": w} for w in ws]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchrow(self, sql, *args):
        # _resolve_target
        if "FROM chat_sessions s" in sql and "s.role_key = $1" in sql:
            target, origin = args
            ows = next(s["workspace_id"] for s in self.sessions if s["id"] == origin)
            for s in self.sessions:
                if s["role_key"] == target and s["workspace_id"] == ows:
                    return {"id": s["id"], "title": s["title"], "role_key": s["role_key"],
                            "workspace_id": s["workspace_id"]}
            return None
        if "FROM chat_sessions s" in sql:
            return None  # 별칭·제목 단계는 이 가짜에서 매칭 없음
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "FROM chat_sessions WHERE workspace_id = $1::uuid AND role_key = $2" in sql:
            ws, role = args
            for s in self.sessions:
                if s["workspace_id"] == ws and s["role_key"] == role:
                    return s["id"]
            return None
        if sql.startswith("INSERT INTO chat_sessions"):
            ws, title, role = args
            sid = str(uuid.uuid4())
            self.sessions.append({"id": sid, "workspace_id": ws, "tenant_id": TENANT,
                                  "title": title, "role_key": role})
            return sid
        if "FROM session_relay" in sql and "dedup" not in sql:
            origin, target, _minutes, marker = args
            for r in reversed(self.relays):
                if (r["origin"] == origin and r["target"] == target and r["hop"] == 0
                        and r["status"] != "failed" and r["question"].endswith(marker)):
                    return r["id"]
            return None
        raise AssertionError(f"unexpected fetchval: {sql}")


@pytest.fixture
def fake(monkeypatch):
    pool = FakePool()
    delivered: list[tuple[str, str]] = []

    async def _fake_deliver(session_id, content):
        delivered.append((session_id, content))

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    monkeypatch.setattr(session_relay, "_deliver_answer", _fake_deliver)
    pool.delivered = delivered
    return pool


def _app():
    app = FastAPI()
    app.include_router(notify_router.router, prefix="/api/v1")
    app.dependency_overrides[notify_router.require_tenant_member] = lambda: {
        "tenant": {"id": TENANT}, "membership": {"role": "member"}, "user": {},
    }
    return app


async def _post(payload):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.post("/api/v1/session-notify", json=payload)
    # 백그라운드 전달이 끝나게 한 박자 준다.
    await asyncio.sleep(0)
    for t in list(session_relay._running):
        await t
    return resp


_BASE = {
    "target": "OpsInfraOwner",
    "title": "분봉 수집 지연",
    "body": "MINUTE_COLLECT_HEALTH lag=412s (임계 300s)\n확인: docker logs go100-collector --tail 50",
    "severity": "warn",
    "source": "go100-cron:MINUTE_COLLECT_HEALTH",
}


@pytest.mark.asyncio
async def test_role_key_delivers_and_records_one_row(fake):
    resp = await _post(_BASE)

    assert resp.status_code == 202
    data = resp.json()
    assert data["delivered"] is True
    assert data["target_session_id"] == OPS
    assert data["reason"] is None
    assert len(fake.relays) == 1
    row = fake.relays[0]
    assert row["id"] == data["relay_id"]
    assert row["hop"] == 0
    assert row["status"] == session_relay.NOTIFY_STATUS_DELIVERED
    assert row["question"].startswith("[알림:warn] 분봉 수집 지연\n")
    assert row["question"].endswith("(source=go100-cron:MINUTE_COLLECT_HEALTH)")
    # 발신자는 같은 워크스페이스의 시스템 세션이다.
    sender = next(s for s in fake.sessions if s["id"] == row["origin"])
    assert sender["role_key"] == session_relay.NOTIFY_SENDER_ROLE_KEY
    assert sender["workspace_id"] == WS
    # 대상 인박스에 들어갔다.
    assert [sid for sid, _ in fake.delivered] == [OPS]

    # 재호출 시 시스템 발신 세션을 새로 만들지 않는다(멱등).
    await _post({**_BASE, "title": "두 번째"})
    senders = [s for s in fake.sessions if s["role_key"] == session_relay.NOTIFY_SENDER_ROLE_KEY]
    assert len(senders) == 1


@pytest.mark.asyncio
async def test_unknown_target_returns_202_not_delivered(fake):
    resp = await _post({**_BASE, "target": "NoSuchOwner"})

    assert resp.status_code == 202
    assert resp.json() == {"delivered": False, "target_session_id": None,
                           "relay_id": None, "reason": "target_not_found"}
    assert fake.relays == []
    assert fake.delivered == []


@pytest.mark.asyncio
async def test_dedup_key_suppresses_second_row(fake):
    first = await _post({**_BASE, "dedup_key": "minute-lag:2026-09-29T18"})
    second = await _post({**_BASE, "dedup_key": "minute-lag:2026-09-29T18"})

    assert first.json()["delivered"] is True
    assert second.status_code == 202
    body = second.json()
    assert body["delivered"] is False
    assert body["reason"] == "deduplicated"
    assert body["relay_id"] == first.json()["relay_id"]
    assert len(fake.relays) == 1
    assert len(fake.delivered) == 1

    # 다른 키는 막지 않는다.
    third = await _post({**_BASE, "dedup_key": "minute-lag:2026-09-29T19"})
    assert third.json()["delivered"] is True
    assert len(fake.relays) == 2


@pytest.mark.asyncio
async def test_invalid_severity_is_422(fake):
    resp = await _post({**_BASE, "severity": "fatal"})

    assert resp.status_code == 422
    assert fake.relays == []


@pytest.mark.asyncio
async def test_delivery_failure_marks_row_failed(fake, monkeypatch):
    async def _boom(session_id, content):
        raise RuntimeError("stream broke")

    monkeypatch.setattr(session_relay, "_deliver_answer", _boom)
    resp = await _post(_BASE)

    assert resp.status_code == 202
    assert fake.relays[0]["status"] == "failed"
    assert "stream broke" in fake.relays[0]["error"]


def test_notify_does_not_take_round_trip_path():
    """답 대기·hop 증가 경로를 타면 알림이 다시 failed/pending 으로 쌓인다."""
    src = inspect.getsource(session_relay.notify) + inspect.getsource(session_relay._run_notify)
    assert "_run_relay" not in src
    assert "_current_hop" not in src
    assert "MAX_HOP" not in src
    assert "_deliver_answer" in src, "대상이 응답 중이면 기다리는 기존 전달 방식을 재사용해야 한다"


def test_status_value_passes_db_check_constraint():
    """session_relay_status_chk 가 허용하지 않는 값은 INSERT 가 거부된다."""
    assert session_relay.NOTIFY_STATUS_DELIVERED in {
        "queued", "pending", "answered", "failed", "blocked",
    }


def test_endpoint_requires_tenant_member():
    """무인증 엔드포인트 금지."""
    sig = inspect.signature(notify_router.post_session_notify)
    assert sig.parameters["context"].default.dependency is notify_router.require_tenant_member
