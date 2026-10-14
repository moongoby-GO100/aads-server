"""클로브 은행거래 거래처 매핑(실측 payload 키)과 기존 review_box 백필."""
from __future__ import annotations

import importlib.util
import json
import uuid
from decimal import Decimal
from pathlib import Path

from app.services import obys_collection_core as core

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("backfill_bank", ROOT / "scripts" / "backfill_obys_clobe_bank_counterparty.py")
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

# 2026-10-14 운영 acct-pg obys DB 실측 payload 형태
PAYLOAD = {
    "transactionId": "TX-0001", "bankName": "국민은행", "transactionAt": "2026-10-10T12:30:00",
    "inAmount": 15000.0, "outAmount": 0.0, "transactionName": "", "transactionDescription": "쿠팡페이주식회",
    "category": "매출", "categoryId": "c1", "isUnclassified": False,
}


def test_counterparty_prefers_business_entity_name():
    raw = {**PAYLOAD, "businessEntityName": "쿠팡(주)"}
    assert core.normalize_item(core.KIND_BANK, raw)["counterparty"] == "쿠팡(주)"


def test_counterparty_falls_back_to_description_then_transaction_name():
    assert core.normalize_item(core.KIND_BANK, PAYLOAD)["counterparty"] == "쿠팡페이주식회"
    blank_entity = {**PAYLOAD, "businessEntityName": ""}
    assert core.normalize_item(core.KIND_BANK, blank_entity)["counterparty"] == "쿠팡페이주식회"
    only_name = {"transactionId": "T", "transactionName": "홍길동", "transactionDescription": "  "}
    assert core.normalize_item(core.KIND_BANK, only_name)["counterparty"] == "홍길동"
    assert core.normalize_item(core.KIND_BANK, {"transactionId": "T"})["counterparty"] == ""
    assert core.normalize_item(core.KIND_BANK, {"businessEntityName": None, "transactionDescription": None})["counterparty"] == ""


def test_other_bank_columns_unchanged():
    row = core.normalize_item(core.KIND_BANK, PAYLOAD)
    assert row["amount"] == Decimal("15000.00") and row["direction"] == "IN"
    assert row["source_key"] == "TX-0001" and row["institution"] == "국민은행"


def test_item_hash_independent_of_counterparty_rule():
    assert core.item_hash(core.KIND_BANK, PAYLOAD) == core.item_hash(core.KIND_BANK, dict(PAYLOAD))


# ── 백필 ────────────────────────────────────────────────

class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.writes: list[tuple] = []
        self.executed: list[str] = []
        self.fetch_sql: list[str] = []
        self.tx_modes: list[bool] = []

    def transaction(self, readonly=False):
        conn = self

        class Tx:
            async def __aenter__(self):
                conn.tx_modes.append(readonly)

            async def __aexit__(self, *exc):
                return False

        return Tx()

    async def fetch(self, sql, tenant_id=None, business_id=None, blocked_tenants=(), blocked_businesses=()):
        self.fetch_sql.append(sql)
        assert "<> ALL($3::text[])" in sql and "<> ALL($4::text[])" in sql
        assert "stage = 'review_box'" in sql and "data_kind = 'bank_transaction'" in sql
        return [r for r in self.rows if r["stage"] == "review_box" and r["data_kind"] == "bank_transaction"
                and (tenant_id is None or str(r["tenant_id"]) == tenant_id)
                and (business_id is None or r["business_id"] == business_id)
                and str(r["tenant_id"]) not in blocked_tenants and (r["business_id"] or "") not in blocked_businesses]

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if not sql.startswith("UPDATE"):
            return "SET"
        assert "stage = 'review_box'" in sql and "IS DISTINCT FROM" in sql
        assert "amount" not in sql and "item_hash" not in sql
        item_id, counterparty = args
        for r in self.rows:
            if r["item_id"] == item_id:
                if r["counterparty"] == counterparty:
                    return "UPDATE 0"
                r["counterparty"] = counterparty
                self.writes.append(args)
                return "UPDATE 1"
        return "UPDATE 0"


TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _row(i, stage="review_box", payload=None, counterparty="", tenant=TENANT_A, business="biz-1"):
    p = dict(PAYLOAD, transactionId=f"TX-{i}") if payload is None else payload
    return {"item_id": uuid.uuid4(), "tenant_id": tenant, "business_id": business, "data_kind": "bank_transaction",
            "stage": stage, "payload": json.dumps(p, ensure_ascii=False), "counterparty": counterparty}


def test_plan_changes_fills_empty_keeps_existing_when_new_is_blank():
    filled = _row(1)
    same = _row(2, counterparty="쿠팡페이주식회")
    blank_payload = _row(3, payload={"transactionId": "TX-3", "transactionName": ""}, counterparty="기존")
    changes, unchanged = backfill.plan_changes([filled, same, blank_payload])
    assert changes == [(filled["item_id"], "쿠팡페이주식회")] and unchanged == 2


def test_plan_changes_corrects_stale_value_with_new_rule():
    row = _row(1, payload={**PAYLOAD, "businessEntityName": "쿠팡(주)"}, counterparty="쿠팡페이주식회")
    changes, unchanged = backfill.plan_changes([row])
    assert changes == [(row["item_id"], "쿠팡(주)")] and unchanged == 0


async def test_backfill_dry_run_is_read_only_unlocked_and_writes_nothing():
    rows = [_row(i) for i in range(50)]
    conn = FakeConn(rows + [_row(900, stage="confirmed"), _row(901, stage="reviewed")])
    result = await backfill.backfill(conn, apply=False)
    assert {k: result[k] for k in ("mode", "targets", "changed", "would_change", "unchanged")} == {
        "mode": "dry-run", "targets": 50, "changed": 0, "would_change": 50, "unchanged": 0}
    assert conn.writes == [] and conn.executed == [] and conn.tx_modes == [True]
    assert all("FOR UPDATE" not in sql for sql in conn.fetch_sql)
    assert result["by_scope"] == [{"tenant_id": str(TENANT_A), "business_id": "biz-1", "targets": 50, "would_change": 50}]


async def test_backfill_apply_locks_writes_only_review_box_and_is_idempotent():
    conn = FakeConn([_row(1), _row(2), _row(3, stage="confirmed"), _row(4, counterparty="쿠팡페이주식회")])
    result = await backfill.backfill(conn, apply=True)
    assert result["targets"] == 3 and result["changed"] == 2 and result["would_change"] == 2 and result["unchanged"] == 1
    assert conn.tx_modes == [False] and conn.executed[0] == backfill.LOCK_TIMEOUT_SQL
    assert all(sql.endswith("FOR UPDATE") for sql in conn.fetch_sql)
    assert all(cp == "쿠팡페이주식회" for _id, cp in conn.writes)
    again = await backfill.backfill(conn, apply=True)
    assert again["changed"] == 0 and again["would_change"] == 0 and again["unchanged"] == 3
    assert len(conn.writes) == 2
    confirmed = next(r for r in conn.rows if r["stage"] == "confirmed")
    assert confirmed["counterparty"] == ""


async def test_backfill_scope_filters_and_reports_counts_per_scope():
    rows = [_row(1), _row(2), _row(3, business="biz-2"), _row(4, tenant=TENANT_B)]
    conn = FakeConn(rows)
    everything = await backfill.backfill(conn, apply=False)
    assert [(s["tenant_id"], s["business_id"], s["targets"]) for s in everything["by_scope"]] == [
        (str(TENANT_A), "biz-1", 2), (str(TENANT_A), "biz-2", 1), (str(TENANT_B), "biz-1", 1)]
    scoped = await backfill.backfill(conn, apply=False, tenant_id=str(TENANT_A), business_id="biz-1")
    assert scoped["targets"] == 2
    applied = await backfill.backfill(conn, apply=True, tenant_id=str(TENANT_B))
    assert applied["targets"] == 1 and applied["changed"] == 1
    assert rows[0]["counterparty"] == "" and rows[3]["counterparty"] == "쿠팡페이주식회"


async def test_backfill_never_touches_blocked_tenants_and_businesses(monkeypatch):
    blocked_tenant = uuid.UUID(sorted(core.blocked_tenants())[0])
    blocked_biz = sorted(core.blocked_businesses())[0]
    rows = [_row(1), _row(2, tenant=blocked_tenant), _row(3, business=blocked_biz)]
    for apply in (False, True):
        result = await backfill.backfill(FakeConn(rows), apply=apply)
        assert result["targets"] == 1
    assert rows[0]["counterparty"] == "쿠팡페이주식회"
    assert rows[1]["counterparty"] == "" and rows[2]["counterparty"] == ""
    explicit = await backfill.backfill(FakeConn(rows), apply=True, tenant_id=str(blocked_tenant))
    assert explicit["targets"] == 0 and rows[1]["counterparty"] == ""
    monkeypatch.setenv("OBYS_COLLECTION_BLOCKED_BUSINESS_IDS", "biz-extra")
    extra = [_row(4, business="biz-extra"), _row(5)]
    assert (await backfill.backfill(FakeConn(extra), apply=True))["targets"] == 1
    assert extra[0]["counterparty"] == ""


def test_backfill_sql_shape():
    assert "FOR UPDATE" not in backfill.SELECT_SQL and backfill.SELECT_FOR_UPDATE_SQL.endswith("FOR UPDATE")
    assert "counterparty IS DISTINCT FROM $2" in backfill.UPDATE_SQL
    assert "tenant_id::text <> ALL($3::text[])" in backfill.SELECT_SQL
    assert "COALESCE(business_id, '') <> ALL($4::text[])" in backfill.SELECT_FOR_UPDATE_SQL
