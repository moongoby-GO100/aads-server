"""자동화 승인 대기 알림·48h 재알림·14일 만료·홈 배지 카운트 (FR-18, AC-05, AC-07)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.work_recipe import approval_notifier as notifier

T0 = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
TENANT_A = "2d701a8c-9596-4757-8588-faa4f7837112"
TENANT_B = "7a1c3f0e-1111-4222-8333-944455556666"


class FakeDB:
    """work_recipe_registration_requests + work_recipe_approval_notices 를 흉내 낸다."""

    def __init__(self) -> None:
        self.regs: dict[str, dict] = {}
        self.notices: dict[str, dict] = {}
        self.alerts: list[dict] = []

    def add(self, tenant: str, requested_at: datetime, *, name="orders", status="pending") -> str:
        rid = str(uuid.uuid4())
        self.regs[rid] = {
            "id": rid, "tenant_id": tenant, "name": name, "domain": "shop.example.com",
            "spec": {"description": "주문 조회"}, "requested_at": requested_at, "status": status,
        }
        return rid

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self, query, *args):
        rows = []
        for reg in self.regs.values():
            n = self.notices.get(reg["id"], {})
            if reg["status"] != "pending" or n.get("expired_at"):
                continue
            rows.append({**reg, "clock_started_at": n.get("clock_started_at"),
                         "notified_at": n.get("notified_at"), "reminded_at": n.get("reminded_at")})
        return sorted(rows, key=lambda r: r["requested_at"])[: args[0]]

    async def fetchrow(self, query, *args):
        if "SELECT COUNT(*)" in query:
            tenant, cutoff = str(args[0]), args[1]
            clocks = []
            for reg in self.regs.values():
                n = self.notices.get(reg["id"], {})
                clock = n.get("clock_started_at") or reg["requested_at"]
                if (str(reg["tenant_id"]) == tenant and reg["status"] == "pending"
                        and not n.get("expired_at") and clock > cutoff):
                    clocks.append(clock)
            return {"pending": len(clocks), "oldest": min(clocks) if clocks else None}
        if query.lstrip().startswith("UPDATE work_recipe_approval_notices"):  # revive
            n = self.notices.get(args[0])
            if n and n.get("expired_at"):
                n.update(expired_at=None, notified_at=None, reminded_at=None, clock_started_at=args[1])
                return {"registration_id": args[0]}
            return None
        column = next(c for c in ("notified_at", "reminded_at", "expired_at") if f"{c} = EXCLUDED" in query)
        rid, tenant, clock, value = args
        n = self.notices.setdefault(rid, {"tenant_id": tenant, "clock_started_at": clock})
        if n.get(column) is not None:
            return None
        n[column] = value
        return {"registration_id": rid}

    async def execute(self, query, rid):
        column = next(c for c in ("notified_at", "reminded_at", "expired_at") if f"SET {c}=NULL" in query)
        self.notices[rid][column] = None


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(notifier, "get_pool", lambda: fake)

    async def notify(title, message="", *, severity="info", category="general", **kw):
        fake.alerts.append({"title": title, "message": message, "severity": severity, "category": category})
        return True

    monkeypatch.setattr("app.services.ohvis_alert.notify", notify)
    monkeypatch.delenv(notifier.APPROVERS_ENV, raising=False)
    return fake


def _created(db: FakeDB, rid: str) -> dict:
    return dict(db.regs[rid])


async def test_created_pending_notifies_approver_once(db):
    rid = db.add(TENANT_A, T0)
    assert await notifier.notify_pending_created(_created(db, rid)) is True
    assert await notifier.notify_pending_created(_created(db, rid)) is False  # 같은 요청 재호출
    assert len(db.alerts) == 1
    alert = db.alerts[0]
    assert alert["category"] == "automation_approval"
    assert "승인을 기다리고 있어요" in alert["message"]
    assert "승인자: 대표님" in alert["message"]


async def test_non_pending_registration_is_not_notified(db):
    rid = db.add(TENANT_A, T0, status="approved")
    assert await notifier.notify_pending_created(_created(db, rid)) is False
    assert db.alerts == []


async def test_notify_never_raises_into_caller(monkeypatch):
    def boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(notifier, "get_pool", boom)
    assert await notifier.notify_pending_created({"id": "x", "status": "pending"}) is False


async def test_second_approver_is_configurable(db, monkeypatch):
    monkeypatch.setenv(notifier.APPROVERS_ENV, "대표님, 김관리, 대표님")
    rid = db.add(TENANT_A, T0)
    await notifier.notify_pending_created(_created(db, rid))
    assert [("김관리" in a["message"]) for a in db.alerts] == [False, True]
    assert notifier.approvers() == ["대표님", "김관리"]


async def test_reminder_fires_once_after_48h_and_not_before(db):
    rid = db.add(TENANT_A, T0)
    await notifier.notify_pending_created(_created(db, rid))

    assert (await notifier.run_sweep(now=T0 + timedelta(hours=47)))["reminded"] == 0
    first = await notifier.run_sweep(now=T0 + timedelta(hours=48))
    again = await notifier.run_sweep(now=T0 + timedelta(hours=60))
    third = await notifier.run_sweep(now=T0 + timedelta(days=10))

    assert (first["reminded"], again["reminded"], third["reminded"]) == (1, 0, 0)
    reminders = [a for a in db.alerts if "재알림" in a["title"]]
    assert len(reminders) == 1 and reminders[0]["severity"] == "warning"
    assert len(db.alerts) == 2  # 생성 알림 1 + 재알림 1


async def test_failed_reminder_is_retried_not_lost(db, monkeypatch):
    rid = db.add(TENANT_A, T0)
    await notifier.notify_pending_created(_created(db, rid))

    async def fail(*a, **kw):
        return False

    monkeypatch.setattr("app.services.ohvis_alert.notify", fail)
    assert (await notifier.run_sweep(now=T0 + timedelta(hours=49)))["reminded"] == 0
    assert db.notices[rid]["reminded_at"] is None


async def test_expires_after_14_days_without_deleting_registration(db):
    rid = db.add(TENANT_A, T0)
    await notifier.notify_pending_created(_created(db, rid))

    stats = await notifier.run_sweep(now=T0 + timedelta(days=14))
    again = await notifier.run_sweep(now=T0 + timedelta(days=15))

    assert (stats["expired"], again["expired"]) == (1, 0)
    assert db.notices[rid]["expired_at"] is not None
    assert rid in db.regs and db.regs[rid]["status"] == "pending"  # 지우지도 상태 변경도 안 함
    assert not [a for a in db.alerts if "재알림" in a["title"]]  # 만료일에는 재알림 대신 만료만


async def test_rerequest_after_expiry_restarts_clock_and_notifies(db):
    rid = db.add(TENANT_A, T0)
    await notifier.notify_pending_created(_created(db, rid))
    await notifier.run_sweep(now=T0 + timedelta(days=14))
    before = len(db.alerts)

    rerequest_at = T0 + timedelta(days=14, minutes=5)
    assert await notifier.notify_pending_created(_created(db, rid), now=rerequest_at) is True
    assert len(db.alerts) == before + 1
    assert db.notices[rid]["expired_at"] is None
    soon = await notifier.run_sweep(now=T0 + timedelta(days=14, hours=1))
    assert (soon["expired"], soon["reminded"]) == (0, 0)


async def test_sweep_safety_net_notifies_pending_that_missed_the_create_hook(db):
    rid = db.add(TENANT_A, T0)
    stats = await notifier.run_sweep(now=T0 + timedelta(hours=1))
    assert stats["notified"] == 1
    assert (await notifier.run_sweep(now=T0 + timedelta(hours=2)))["notified"] == 0
    assert db.notices[rid]["notified_at"] is not None


async def test_pending_count_is_tenant_isolated_and_excludes_expired(db):
    db.add(TENANT_A, T0 - timedelta(hours=5), name="a1")
    db.add(TENANT_A, T0 - timedelta(hours=2), name="a2")
    db.add(TENANT_A, T0 - timedelta(days=20), name="a_old")
    db.add(TENANT_A, T0 - timedelta(hours=1), name="a_done", status="approved")
    db.add(TENANT_B, T0 - timedelta(hours=100), name="b1")

    a = await notifier.pending_count(tenant_id=TENANT_A, now=T0)
    b = await notifier.pending_count(tenant_id=TENANT_B, now=T0)
    empty = await notifier.pending_count(tenant_id=str(uuid.uuid4()), now=T0)

    assert a == {"pending": 2, "oldest_hours": 5.0}
    assert b == {"pending": 1, "oldest_hours": 100.0}
    assert empty == {"pending": 0, "oldest_hours": 0}


async def test_pending_count_endpoint_uses_context_tenant(monkeypatch):
    from app.api import automations

    captured = {}

    async def fake_count(*, tenant_id):
        captured["tenant_id"] = tenant_id
        return {"pending": 3, "oldest_hours": 7.5}

    monkeypatch.setattr(automations, "pending_count", fake_count)
    result = await automations.get_pending_count({"tenant": {"id": TENANT_A}})
    assert result == {"pending": 3, "oldest_hours": 7.5}
    assert captured["tenant_id"] == TENANT_A
    paths = {r.path: r.methods for r in automations.router.routes}
    assert paths["/automations/pending-count"] == {"GET"}


def test_message_has_no_secret_and_migration_is_additive():
    assert "***" in notifier._clean("login password=hunter2 now")
    assert "eyJ" not in notifier._clean("eyJhbGciOiJIUzI1NiJ9abcdef")
    sql = (Path(__file__).parents[2] / "migrations" / "20261014_work_recipe_approval_notices.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS work_recipe_approval_notices" in sql
    assert "tenant_id        UUID NOT NULL REFERENCES tenants" in sql
    for banned in ("DROP ", "TRUNCATE", "ALTER TABLE work_recipe_registration_requests"):
        assert banned not in sql.upper().replace("DROP INDEX IF EXISTS", "")
