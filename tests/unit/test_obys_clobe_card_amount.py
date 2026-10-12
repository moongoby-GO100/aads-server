"""클로브 카드승인 금액·가맹점 매핑(실측 payload 키)과 기존 review_box 백필."""
from __future__ import annotations

import importlib.util
import json
import uuid
from decimal import Decimal
from pathlib import Path

from app.services import obys_collection_core as core

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("backfill_card", ROOT / "scripts" / "backfill_obys_clobe_card_amount.py")
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

# 2026-10-12 운영 acct-pg obys DB 실측 payload 형태
PAYLOAD = {
    "approvalId": "AP-0001", "approvalNo": "12345678", "usedAt": "2026-10-10T12:30:00",
    "usedAmountKrw": 22500.0, "vat": 2045.0, "memberStoreName": "지에스25 (GS25) 상계정원점",
    "memberStoreType": "편의점", "cancelType": "NONE", "remainingUsedAmountKrw": 22500.0,
    "homeForeignType": "DOMESTIC", "cardNo": "1234-****-****-5678",
}


def test_card_normalize_maps_real_keys():
    row = core.normalize_item(core.KIND_CARD, PAYLOAD)
    assert row["amount"] == Decimal("22500.00")
    assert row["counterparty"] == "지에스25 (GS25) 상계정원점"
    assert row["source_key"] == "AP-0001"
    assert row["direction"] == "OUT"


def test_card_normalize_keeps_legacy_keys():
    row = core.normalize_item(core.KIND_CARD, {"id": "x", "usedAmount": 1000, "merchantName": "가게"})
    assert row["amount"] == Decimal("1000.00") and row["counterparty"] == "가게"


def test_card_cancellation_is_negative_and_not_double_flipped():
    cancel = {**PAYLOAD, "cancelType": "FULL"}
    assert core.normalize_item(core.KIND_CARD, cancel)["amount"] == Decimal("-22500.00")
    already_negative = {**PAYLOAD, "cancelType": "FULL", "usedAmountKrw": -22500.0}
    assert core.normalize_item(core.KIND_CARD, already_negative)["amount"] == Decimal("-22500.00")
    assert core.is_card_cancelled(cancel) and not core.is_card_cancelled(PAYLOAD)


def test_card_foreign_uses_krw_only():
    foreign = {**PAYLOAD, "homeForeignType": "FOREIGN", "usedAmount": 15.5, "usedAmountKrw": 21000.0}
    assert core.normalize_item(core.KIND_CARD, foreign)["amount"] == Decimal("21000.00")
    no_krw = {k: v for k, v in foreign.items() if k != "usedAmountKrw"}
    assert core.normalize_item(core.KIND_CARD, no_krw)["amount"] is None


# ── 대사 합계 (리뷰 P1-1, P1-2) ───────────────────────────

def _card_sums(rows):
    return {k: sum((core.row_sum_value(core.KIND_CARD, k, r) for r in rows), Decimal("0"))
            for k in core.sum_item_keys(core.KIND_CARD)}


def test_card_reconcile_uses_one_row_key_per_total():
    assert core.sum_item_keys(core.KIND_CARD) == ("usedAmountKrw", "vat")
    assert [m for m, _ in core._SUM_FIELDS[core.KIND_CARD]] == ["totalUsedAmountSum", "totalVatAmountSum"]


def test_reconcile_sum_matches_normalized_amount_for_original_positive_cancel():
    rows = [PAYLOAD, {**PAYLOAD, "approvalId": "AP-2", "cancelType": "FULL", "usedAmountKrw": 22500.0, "vat": 2045.0}]
    sums = _card_sums(rows)
    assert sums["usedAmountKrw"] == sum((core.normalize_item(core.KIND_CARD, r)["amount"] for r in rows), Decimal("0"))
    assert sums["usedAmountKrw"] == Decimal("0.00") and sums["vat"] == Decimal("0.00")
    out = core.reconcile(core.KIND_CARD, 2, sums, {"totalElements": 2, "totalUsedAmountSum": 0, "totalVatAmountSum": 0})
    assert out["ok"] and [c["status"] for c in out["sum_checks"]] == ["ok", "ok"]


def test_reconcile_sum_matches_normalized_amount_for_original_negative_cancel():
    cancel = {**PAYLOAD, "approvalId": "AP-2", "cancelType": "FULL", "usedAmountKrw": -22500.0, "vat": -2045.0}
    assert core.row_sum_value(core.KIND_CARD, "usedAmountKrw", cancel) == Decimal("-22500.00")
    assert core.row_sum_value(core.KIND_CARD, "vat", cancel) == Decimal("-2045.00")
    sums = _card_sums([PAYLOAD, cancel])
    assert sums["usedAmountKrw"] == Decimal("0.00")
    out = core.reconcile(core.KIND_CARD, 2, sums, {"totalElements": 2, "totalUsedAmountSum": 0, "totalVatAmountSum": 0})
    assert out["ok"]
    assert core.reconcile(core.KIND_CARD, 2, sums, {"totalElements": 2, "totalUsedAmountSum": 45000})["ok"] is False


def test_reconcile_foreign_card_is_ok_with_krw_total():
    foreign = {**PAYLOAD, "homeForeignType": "FOREIGN", "usedAmount": 15.5, "usedAmountKrw": 21000.0}
    sums = _card_sums([foreign])
    assert sums["usedAmountKrw"] == Decimal("21000.00")
    out = core.reconcile(core.KIND_CARD, 1, sums, {"totalElements": 1, "totalUsedAmountSum": 21000, "totalVatAmountSum": 2045})
    assert out["ok"] and [c["status"] for c in out["sum_checks"]] == ["ok", "ok"]


def test_reconcile_legacy_keys_and_missing_values():
    legacy = {"id": "x", "usedAmount": 1000, "vatAmount": 90}
    sums = _card_sums([legacy])
    assert sums == {"usedAmountKrw": Decimal("1000.00"), "vat": Decimal("90.00")}
    assert core.reconcile(core.KIND_CARD, 1, sums, {"totalElements": 1, "totalUsedAmountSum": 1000, "totalVatAmountSum": 90})["ok"]
    no_vat = {"id": "y", "usedAmountKrw": 1000}
    assert core.row_sum_value(core.KIND_CARD, "vat", no_vat) is None
    out = core.reconcile(core.KIND_CARD, 1, {"usedAmountKrw": Decimal("1000"), "vat": None},
                         {"totalElements": 1, "totalUsedAmountSum": 1000, "totalVatAmountSum": 90})
    assert out["ok"] and out["sum_checks"][1]["status"] == "skipped"
    foreign_without_krw = {"id": "z", "homeForeignType": "FOREIGN", "usedAmount": 15.5}
    assert core.row_sum_value(core.KIND_CARD, "usedAmountKrw", foreign_without_krw) is None


def test_reconcile_skips_without_meta_totals():
    out = core.reconcile(core.KIND_CARD, 1, {"usedAmountKrw": Decimal("22500")}, {"totalElements": 1})
    assert out["ok"] and all(c["status"] == "skipped" for c in out["sum_checks"])


def test_row_sum_sql_mirrors_python_rules():
    used = core.row_sum_sql(core.KIND_CARD, "usedAmountKrw")
    assert "-abs(" in used and "cancelType" in used and "homeForeignType" in used
    assert used.index("usedAmountKrw") < used.index("usedAmount'") < used.index("'amount'")
    assert "* -1" not in used and "THEN -1" not in used
    vat = core.row_sum_sql(core.KIND_CARD, "vat")
    assert "-abs(" in vat and vat.index("'vat'") < vat.index("'vatAmount'")
    assert "취소" in core.row_sum_sql(core.KIND_CASH, "totalAmount")
    assert core.row_sum_sql(core.KIND_TAX, "totalAmount").endswith("* 1)")
    assert "cancelType" not in "".join(core.CANCEL_SIGN_SQL.values())


def test_shadow_stream_totals_use_normalized_card_amount():
    from types import SimpleNamespace

    from app.services import obys_collection_service as svc

    ctx = SimpleNamespace(kind=core.KIND_CARD, mode="shadow")
    shadow = {
        "a": {"direction": "OUT", "raw": PAYLOAD},
        "b": {"direction": "OUT", "raw": {**PAYLOAD, "approvalId": "AP-2", "cancelType": "FULL", "usedAmountKrw": -22500.0}},
        "c": {"direction": "OUT", "raw": {**PAYLOAD, "approvalId": "AP-3", "homeForeignType": "FOREIGN",
                                          "usedAmount": 15.5, "usedAmountKrw": 21000.0}},
    }
    import asyncio

    count, sums = asyncio.run(svc._stream_totals(None, ctx, None, shadow))
    assert count == 3 and sums["usedAmountKrw"] == Decimal("21000.00")
    meta = {"totalElements": 3, "totalUsedAmountSum": 21000, "totalVatAmountSum": 2045}
    assert core.reconcile(core.KIND_CARD, count, sums, meta)["ok"]


def test_item_hash_ignores_normalized_columns():
    assert core.item_hash(core.KIND_CARD, PAYLOAD) == core.item_hash(core.KIND_CARD, dict(PAYLOAD))


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
        assert "stage = 'review_box'" in sql and "data_kind = 'card_approval'" in sql
        return [r for r in self.rows if r["stage"] == "review_box" and r["data_kind"] == "card_approval"
                and (tenant_id is None or str(r["tenant_id"]) == tenant_id)
                and (business_id is None or r["business_id"] == business_id)
                and str(r["tenant_id"]) not in blocked_tenants and (r["business_id"] or "") not in blocked_businesses]

    async def execute(self, sql, *args):
        self.executed.append(sql)
        if not sql.startswith("UPDATE"):
            return "SET"
        assert "stage = 'review_box'" in sql and "IS DISTINCT FROM" in sql
        item_id, amount, counterparty = args
        for r in self.rows:
            if r["item_id"] == item_id:
                if r["amount"] == amount and r["counterparty"] == counterparty:
                    return "UPDATE 0"
                r["amount"], r["counterparty"] = amount, counterparty
                self.writes.append(args)
                return "UPDATE 1"
        return "UPDATE 0"


TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _row(i, stage="review_box", payload=None, amount=None, counterparty="", tenant=TENANT_A, business="biz-1"):
    p = dict(PAYLOAD, approvalId=f"AP-{i}") if payload is None else payload
    return {"item_id": uuid.uuid4(), "tenant_id": tenant, "business_id": business, "data_kind": "card_approval",
            "stage": stage, "payload": json.dumps(p, ensure_ascii=False), "amount": amount, "counterparty": counterparty}


async def test_backfill_dry_run_is_read_only_unlocked_and_writes_nothing():
    rows = [_row(i) for i in range(154)]
    conn = FakeConn(rows + [_row(900, stage="confirmed"), _row(901, stage="reviewed")])
    result = await backfill.backfill(conn, apply=False)
    assert {k: result[k] for k in ("mode", "targets", "changed", "would_change", "unchanged")} == {
        "mode": "dry-run", "targets": 154, "changed": 0, "would_change": 154, "unchanged": 0}
    assert conn.writes == [] and conn.executed == []
    assert conn.tx_modes == [True]
    assert all("FOR UPDATE" not in sql for sql in conn.fetch_sql)
    assert result["by_scope"] == [{"tenant_id": str(TENANT_A), "business_id": "biz-1", "targets": 154, "would_change": 154}]


async def test_backfill_apply_locks_writes_only_review_box_and_is_idempotent():
    conn = FakeConn([_row(1), _row(2), _row(3, stage="confirmed"),
                     _row(4, amount=Decimal("22500.00"), counterparty="지에스25 (GS25) 상계정원점")])
    result = await backfill.backfill(conn, apply=True)
    assert result["targets"] == 3 and result["changed"] == 2 and result["would_change"] == 2 and result["unchanged"] == 1
    assert conn.tx_modes == [False]
    assert conn.executed[0] == backfill.LOCK_TIMEOUT_SQL
    assert all(sql.endswith("FOR UPDATE") for sql in conn.fetch_sql)
    for _item_id, amount, counterparty in conn.writes:
        assert amount == Decimal("22500.00") and counterparty == "지에스25 (GS25) 상계정원점"
    again = await backfill.backfill(conn, apply=True)
    assert again["changed"] == 0 and again["would_change"] == 0 and again["unchanged"] == 3
    assert len(conn.writes) == 2


async def test_backfill_scope_filters_and_reports_counts_per_scope():
    rows = [_row(1), _row(2), _row(3, business="biz-2"), _row(4, tenant=TENANT_B)]
    conn = FakeConn(rows)
    everything = await backfill.backfill(conn, apply=False)
    assert everything["targets"] == 4 and [(s["tenant_id"], s["business_id"], s["targets"]) for s in everything["by_scope"]] == [
        (str(TENANT_A), "biz-1", 2), (str(TENANT_A), "biz-2", 1), (str(TENANT_B), "biz-1", 1)]
    scoped = await backfill.backfill(conn, apply=False, tenant_id=str(TENANT_A), business_id="biz-1")
    assert scoped["targets"] == 2 and scoped["scope"] == {"tenant_id": str(TENANT_A), "business_id": "biz-1"}
    applied = await backfill.backfill(conn, apply=True, tenant_id=str(TENANT_B))
    assert applied["targets"] == 1 and applied["changed"] == 1
    assert rows[0]["amount"] is None and rows[3]["amount"] == Decimal("22500.00")


def test_backfill_sql_scopes_by_tenant_and_business_and_updates_idempotently():
    assert "tenant_id" in backfill.SELECT_SQL and "business_id" in backfill.SELECT_SQL
    assert "FOR UPDATE" not in backfill.SELECT_SQL and backfill.SELECT_FOR_UPDATE_SQL.endswith("FOR UPDATE")
    assert "amount IS DISTINCT FROM $2" in backfill.UPDATE_SQL


async def test_backfill_does_not_blank_existing_values():
    row = _row(1, payload={"approvalId": "AP-1"}, amount=Decimal("5.00"), counterparty="기존")
    result = await backfill.backfill(FakeConn([row]), apply=True)
    assert result["changed"] == 0 and result["unchanged"] == 1


async def test_backfill_recomputes_cancellation_sign_for_wrong_existing_amount():
    cancel = dict(PAYLOAD, approvalId="AP-9", cancelType="FULL")
    row = _row(9, payload=cancel, amount=Decimal("22500.00"), counterparty="지에스25 (GS25) 상계정원점")
    conn = FakeConn([row])
    result = await backfill.backfill(conn, apply=True)
    assert result["changed"] == 1 and conn.writes[0][1] == Decimal("-22500.00")


async def test_backfill_never_touches_blocked_tenants_and_businesses(monkeypatch):
    blocked_tenant = uuid.UUID(sorted(core.blocked_tenants())[0])
    blocked_biz = sorted(core.blocked_businesses())[0]
    rows = [_row(1), _row(2, tenant=blocked_tenant), _row(3, business=blocked_biz)]
    for apply in (False, True):
        conn = FakeConn(rows)
        result = await backfill.backfill(conn, apply=apply)
        assert result["targets"] == 1
        assert result["excluded_by_policy"]["businesses"] == sorted(core.blocked_businesses())
    assert rows[0]["amount"] == Decimal("22500.00")
    assert rows[1]["amount"] is None and rows[2]["amount"] is None
    explicit = await backfill.backfill(FakeConn(rows), apply=True, tenant_id=str(blocked_tenant))
    assert explicit["targets"] == 0 and explicit["changed"] == 0 and rows[1]["amount"] is None
    monkeypatch.setenv("OBYS_COLLECTION_BLOCKED_BUSINESS_IDS", "biz-extra")
    extra = [_row(4, business="biz-extra"), _row(5)]
    assert (await backfill.backfill(FakeConn(extra), apply=True))["targets"] == 1
    assert extra[0]["amount"] is None


def test_backfill_sql_applies_block_policy_regardless_of_scope_args():
    assert "tenant_id::text <> ALL($3::text[])" in backfill.SELECT_SQL
    assert "COALESCE(business_id, '') <> ALL($4::text[])" in backfill.SELECT_FOR_UPDATE_SQL


# ── 대사 합계 소수 처리 (재리뷰 P2) ───────────────────────

def test_python_sum_rounds_each_row_and_sql_does_too():
    rows = [{**PAYLOAD, "approvalId": f"AP-{i}", "usedAmountKrw": 1.004} for i in range(3)]
    sums = _card_sums(rows)
    assert sums["usedAmountKrw"] == Decimal("3.00")
    meta = {"totalElements": 3, "totalUsedAmountSum": 3.00, "totalVatAmountSum": 3 * 2045}
    assert core.reconcile(core.KIND_CARD, 3, sums, meta)["ok"]
    for kind, key in ((core.KIND_CARD, "usedAmountKrw"), (core.KIND_CARD, "vat"),
                      (core.KIND_CASH, "totalAmount"), (core.KIND_TAX, "totalAmount")):
        sql = core.row_sum_sql(kind, key)
        assert "round(" in sql and ", 2)" in sql, (kind, key)
    # 합산 전 행 단위 반올림: round 가 CASE/부호 곱셈 안쪽(행 값)에 있어야 한다
    assert core.row_sum_sql(core.KIND_CARD, "usedAmountKrw").count("round(") == 2


def test_dec_rounds_half_up_like_sql_round():
    assert core._dec("1.005") == Decimal("1.01") and core._dec("-1.005") == Decimal("-1.01")
    assert core._dec("1.004") == Decimal("1.00")
