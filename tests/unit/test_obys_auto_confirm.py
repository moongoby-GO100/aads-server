"""클로브 자동 확정: 순수 판정, 무쓰기 기본값, 기존 확정 경로와 수집 격리."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services import obys_auto_confirm as auto
from app.services import obys_collection_scheduler as sched
from app.services import obys_collection_service as svc
from tests.unit.test_obys_bulk_confirm import BANK, BIZ, TAX, TENANT, FakeConn, Rec

TODAY = date(2026, 11, 24)


def row(**kw):
    return {"item_id": "candidate", "tenant_id": TENANT, "business_id": BIZ,
            "data_kind": BANK, "source_key": "one", "item_hash": "h-one", "direction": "IN",
            "occurred_on": TODAY, "amount": Decimal("100"), "counterparty": "", **kw}


def classify(candidate, items=(), ledger=()):
    return auto.classify_item(candidate, auto.build_history(list(items), list(ledger), today=TODAY))


@pytest.mark.parametrize("changes,reason", [
    ({"direction": ""}, "direction_invalid"), ({"direction": "SALES"}, "direction_invalid"),
    ({"amount": 0}, "amount_zero"), ({"amount": -1}, "amount_negative"),
    ({"occurred_on": TODAY + timedelta(days=1)}, "future_date"),
    ({"source_key": " "}, "source_key_missing"),
])
def test_simple_reasons(changes, reason):
    assert classify(row(**changes)) == (False, [reason])


@pytest.mark.parametrize("kind,direction", [(BANK, "IN"), (BANK, "OUT"), (TAX, "SALES"),
    (TAX, "PURCHASE"), ("cash_receipt", "SALES"), ("cash_receipt", "PURCHASE"), ("card_approval", "OUT")])
def test_valid_directions(kind, direction):
    assert classify(row(data_kind=kind, direction=direction)) == (True, [])


def test_hash_conflict_and_scope():
    assert classify(row(), ledger=[row(item_hash="old")]) == (False, ["hash_conflict"])
    assert classify(row(), ledger=[row()]) == (True, [])
    assert classify(row(), ledger=[row(item_hash="old", business_id="other")]) == (True, [])


def test_duplicate_different_source_only():
    assert classify(row(), [row(source_key="two")]) == (False, ["duplicate_suspect"])
    assert classify(row(), [row(item_id="version")]) == (True, [])
    for change in ({"business_id": "other"}, {"tenant_id": "other"}, {"data_kind": TAX},
                   {"occurred_on": TODAY - timedelta(days=1)}, {"amount": 101}, {"counterparty": "different"}):
        assert classify(row(), [row(source_key="two", **change)]) == (True, [])


def test_outlier_sample_count_window_scope_and_boundary():
    items = [row(item_id=str(i), source_key=str(i), amount=10) for i in range(30)]
    assert classify(row(amount=51), items) == (False, ["amount_outlier"])
    assert classify(row(amount=50), items) == (True, [])
    assert classify(row(amount=51), items[:29]) == (True, [])
    for changes in ({"business_id": "other"}, {"data_kind": TAX},
                    {"occurred_on": TODAY - timedelta(days=91)}, {"occurred_on": TODAY + timedelta(days=1)}):
        assert classify(row(amount=51), [{**r, **changes} for r in items]) == (True, [])
    assert classify(row(amount=51), [*items[:29], row(amount=51)]) == (True, [])  # 자기 표본 제외


def test_reasons_accumulate():
    ok, reasons = classify(row(direction="", amount=0, source_key=""))
    assert not ok and reasons == ["direction_invalid", "amount_zero", "source_key_missing"]


class AutoConn(FakeConn):
    def __init__(self):
        super().__init__()
        self.table = True
        self.exceptions = {}
        self.sql = []

    def add(self, *args, **kwargs):
        item_id = super().add(*args, **kwargs)
        self.items[item_id].update(first_seen_at=datetime.now(timezone.utc), counterparty="")
        return item_id

    def transaction(self, **kwargs):
        return super().transaction()

    def _eligible(self, args):
        return [r for r in self.items.values() if str(r["tenant_id"]) == str(args[0])
                and r["business_id"] == args[1] and r["data_kind"] in args[2] and r["stage"] == "review_box"
                and (args[3] is None or r["first_seen_at"] >= args[3])]

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return "obys_auto_confirm_exception" if self.table else None
        if sql.startswith("SELECT count(*) FROM obys_clobe_item i"):
            return len(self._eligible(args))
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if sql.startswith("SELECT i.*"):
            rows = sorted(self._eligible(args), key=lambda r: (r["data_kind"], r["source_key"], str(r["item_id"])))
            return [Rec(r) for r in rows[args[5]:args[5] + args[4]]]
        if sql.startswith("SELECT * FROM obys_clobe_item WHERE tenant_id=$1 AND business_id=$2 AND data_kind"):
            return [Rec(r) for r in self.items.values() if r["business_id"] == args[1]
                    and r["data_kind"] in args[2] and r["stage"] in ("confirmed", "reviewed", "review_box")]
        if sql.startswith("SELECT * FROM obys_clobe_ledger_entry"):
            return [Rec(tenant_id=k[0], business_id=k[1], data_kind=k[2], source_key=k[3], **v)
                    for k, v in self.ledger.items()]
        return await super().fetch(sql, *args)

    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO obys_auto_confirm_exception"):
            self.writes += 1
            self.exceptions[args[0]] = args[3]
            return
        if sql.startswith("UPDATE obys_auto_confirm_exception"):
            self.writes += 1
            for item_id in args[2]:
                self.exceptions.pop(item_id, None)
            return
        return await super().execute(sql, *args)


@pytest.fixture
def world(monkeypatch):
    conn = AutoConn()

    async def connect():
        return conn

    monkeypatch.setattr(svc, "_connect", connect)
    monkeypatch.setenv("OBYS_AUTO_CONFIRM_ENABLED", "1")
    monkeypatch.setenv("OBYS_AUTO_CONFIRM_BACKLOG", "1")
    monkeypatch.delenv("OBYS_CLOBE_VOUCHER_PILOT_BUSINESSES", raising=False)
    return conn


async def test_only_normal_confirmed_idempotent_and_actor_fixed(world):
    good = world.add(BANK, "good", TODAY, "100")
    bad = world.add(BANK, "bad", TODAY, "0", direction="")
    channel = world.add("channel_revenue", "channel", TODAY, "1")
    result = await auto.auto_confirm_business(TENANT, BIZ, actor="human")
    assert (result["confirmed"], result["exceptions"], result["remaining"]) == (1, 1, 1)
    assert world.items[good]["stage"] == "confirmed"
    assert world.items[good]["confirmed_by"] == world.items[good]["reviewed_by"] == auto.ACTOR
    assert world.items[bad]["stage"] == world.items[channel]["stage"] == "review_box"
    assert world.exceptions[bad] == ["direction_invalid", "amount_zero"]
    assert any("FOR UPDATE OF i SKIP LOCKED" in sql for sql in world.sql)
    assert (await auto.auto_confirm_business(TENANT, BIZ))["confirmed"] == 0
    assert len(world.ledger) == 1


async def test_disabled_no_connection(world, monkeypatch):
    monkeypatch.delenv("OBYS_AUTO_CONFIRM_ENABLED")

    async def forbidden():
        raise AssertionError("disabled must not connect")

    monkeypatch.setattr(svc, "_connect", forbidden)
    assert (await auto.auto_confirm_business(TENANT, BIZ))["skipped"] == "disabled"
    assert world.writes == 0


async def test_only_new_items_without_backlog(world, monkeypatch):
    old = world.add(BANK, "old", TODAY, "100")
    start = datetime.now(timezone.utc)
    new = world.add(BANK, "new", TODAY, "101")
    world.items[old]["first_seen_at"] = start - timedelta(days=1)
    monkeypatch.setenv("OBYS_AUTO_CONFIRM_BACKLOG", "0")
    assert (await auto.auto_confirm_business(TENANT, BIZ))["skipped"] == "collection_start_required"
    assert (await auto.auto_confirm_business(TENANT, BIZ, since=start))["confirmed"] == 1
    assert world.items[old]["stage"] == "review_box" and world.items[new]["stage"] == "confirmed"


async def test_dry_run_full_preview_pages_without_writes(world, monkeypatch):
    monkeypatch.setenv("OBYS_AUTO_CONFIRM_ENABLED", "0")
    for i in range(3):
        world.add(BANK, str(i), TODAY, str(i + 1))
    first = await auto.auto_confirm_business(TENANT, BIZ, dry_run=True, limit=2)
    last = await auto.auto_confirm_business(TENANT, BIZ, dry_run=True, limit=2, preview_offset=2)
    assert (first["normal"], last["normal"], first["remaining"]) == (2, 1, 3)
    assert first["confirmed"] == last["confirmed"] == world.writes == 0
    assert all("FOR UPDATE" not in sql for sql in world.sql)


async def test_missing_exception_table_does_not_block_posting(world, caplog):
    world.table = False
    world.add(BANK, "good", TODAY, "100")
    world.add(BANK, "bad", TODAY, "0")
    result = await auto.auto_confirm_business(TENANT, BIZ)
    assert result["confirmed"] == result["exceptions"] == 1
    assert not world.exceptions and "table missing" in caplog.text


async def test_hash_conflict_stays_in_review_box(world):
    world.add(BANK, "key", TODAY, "100", item_hash="original")
    await auto.auto_confirm_business(TENANT, BIZ)
    changed = world.add(BANK, "key", TODAY, "101", item_hash="changed")
    result = await auto.auto_confirm_business(TENANT, BIZ)
    assert result["confirmed"] == 0 and result["reasons"] == {"hash_conflict": 1}
    assert world.items[changed]["stage"] == "review_box"
    assert next(iter(world.ledger.values()))["item_hash"] == "original"


async def test_limit_remaining_and_other_stages_untouched(world):
    for i in range(3):
        world.add(BANK, f"key-{i}", TODAY, str(100 + i))
    preserved = [world.add(BANK, stage, TODAY, "200", stage=stage)
                 for stage in ("reviewed", "rejected", "superseded", "confirmed")]
    first = await auto.auto_confirm_business(TENANT, BIZ, limit=2)
    second = await auto.auto_confirm_business(TENANT, BIZ, limit=2)
    assert (first["confirmed"], first["remaining"], second["confirmed"], second["remaining"]) == (2, 1, 1, 0)
    assert [world.items[i]["stage"] for i in preserved] == ["reviewed", "rejected", "superseded", "confirmed"]


async def test_repaired_exception_marked_resolved(world):
    item_id = world.add(BANK, "bad", TODAY, "0")
    await auto.auto_confirm_business(TENANT, BIZ)
    assert item_id in world.exceptions
    world.items[item_id]["amount"] = Decimal("100")
    assert (await auto.auto_confirm_business(TENANT, BIZ))["confirmed"] == 1
    assert item_id not in world.exceptions


async def test_posting_failure_rolls_back(world, monkeypatch):
    item_id = world.add(BANK, "key", TODAY, "100")

    async def fail(*args):
        raise RuntimeError("posting failure")

    monkeypatch.setattr(svc, "_post_reviewed_rows", fail)
    with pytest.raises(RuntimeError):
        await auto.auto_confirm_business(TENANT, BIZ)
    assert world.items[item_id]["stage"] == "review_box" and not world.ledger


@pytest.mark.parametrize("status", ["partial", "failed", "needs_reauth", "lease_lost"])
async def test_unsuccessful_collection_never_connects(world, monkeypatch, status):
    async def forbidden():
        raise AssertionError("must not connect")

    monkeypatch.setattr(svc, "_connect", forbidden)
    await sched._auto_confirm_after_collection("company", {"status": status}, datetime.now(timezone.utc))
    assert world.writes == 0


async def test_collection_exception_isolated_and_logged(world, monkeypatch, caplog):
    async def fail():
        raise RuntimeError("connection unavailable")

    monkeypatch.setattr(svc, "_connect", fail)
    await sched._auto_confirm_after_collection("company", {"status": "succeeded"}, datetime.now(timezone.utc))
    assert "obys_auto_confirm_failed" in caplog.text


async def test_success_forwards_scope_and_collection_start(world, monkeypatch):
    seen = {}
    started = datetime.now(timezone.utc)

    async def link(*args):
        return {"tenant_id": TENANT, "business_id": BIZ}

    async def confirm(tenant, business, **kwargs):
        seen.update(tenant=tenant, business=business, **kwargs)

    monkeypatch.setattr(svc, "_get_link", link)
    monkeypatch.setattr(svc, "_scoped_link_or_raise", lambda link: None)
    monkeypatch.setattr(auto, "auto_confirm_business", confirm)
    await sched._auto_confirm_after_collection("company", {"status": "succeeded"}, started)
    assert seen == {"tenant": TENANT, "business": BIZ, "since": started}


async def test_scheduler_continues_after_auto_confirm_failure(world, monkeypatch, caplog):
    calls = []

    async def collection(company_id, **kwargs):
        calls.append(company_id)
        return {"status": "succeeded", "runs": []}

    async def link(*args):
        return {"tenant_id": TENANT, "business_id": BIZ, "link_status": "linked"}

    async def fail(*args, **kwargs):
        raise RuntimeError("posting failure")

    monkeypatch.setattr(svc, "run_collection", collection)
    monkeypatch.setattr(svc, "_get_link", link)
    monkeypatch.setattr(svc, "_scoped_link_or_raise", lambda link: None)
    monkeypatch.setattr(auto, "auto_confirm_business", fail)
    for company in ("first", "second"):
        result = await sched._run_company(company, {}, datetime.now(timezone.utc), TODAY,
                                          "test", time.monotonic() + 30)
        assert result["status"] == "succeeded"
    assert calls.count("first") and calls.count("second")
    assert "obys_auto_confirm_failed" in caplog.text
