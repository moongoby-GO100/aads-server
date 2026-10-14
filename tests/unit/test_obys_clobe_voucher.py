"""클로브 확정 거래 → 오비서 전표 초안: 매핑표·차대 균형·멱등·파일럿 가드·차단 범위·백필.

DB 없이 SQL 문자열로 분기하는 메모리 연결을 쓴다. 실제 PostgreSQL 제약(CHECK/UNIQUE)은
tests/integration/test_obys_collection_postgres.py 의 clobe 전표 케이스가 맡는다.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import obys_clobe_voucher as v
from app.services import obys_collection_core as core
from app.services import obys_collection_service as svc

TENANT = uuid.UUID("15055cac-71b0-45ec-b714-7093dde189ff")
LYLON_TENANT = "d1695f15-6b68-4929-bc8d-646827363ff9"
PILOT = "biz-chicblack"
DAY = date(2026, 10, 1)
D = Decimal


def draft(kind, direction="", amount="11000", counterparty="상호", **payload):
    return v.build_draft(kind, direction=direction, occurred_on=DAY, amount=amount, counterparty=counterparty, payload=payload)


def shape(d):
    return [(ln.side, v.ACCOUNTS[ln.account][0], ln.amount) for ln in d.lines]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(v.PILOT_ENV, raising=False)
    monkeypatch.delenv("OBYS_COLLECTION_BLOCKED_TENANT_IDS", raising=False)
    monkeypatch.delenv("OBYS_COLLECTION_BLOCKED_BUSINESS_IDS", raising=False)


# ── 매핑표(코드 상수를 테스트로 고정) ──

def test_account_table_is_pinned():
    assert v.ACCOUNTS == {
        "cash": ("10100", "현금"),
        "bank": ("10300", "보통예금"),
        "receivable": ("10800", "외상매출금"),
        "vat_paid": ("13500", "부가세대급금"),
        "payable": ("25100", "외상매입금"),
        "unpaid": ("25300", "미지급금"),
        "vat_received": ("25500", "부가세예수금"),
        "sales": ("40100", "상품매출"),
        "interest_income": ("90100", "이자수익"),
        "other_opex": ("83100", "(판)지급수수료"),
        "unclassified_debit": ("9998", "미분류(가계정)"),
        "unclassified_credit": ("9999", "미분류(가계정)"),
    }
    assert v.CATEGORY_ACCOUNTS == {
        "매출": ("sales", "income"),
        "이자수익": ("interest_income", "income"),
        "기타영업비용": ("other_opex", "expense"),
    }


def test_tax_invoice_sales_and_purchase():
    sales = draft(core.KIND_TAX, "SALES", "11000", totalAmount=11000, taxAmount=1000)
    assert shape(sales) == [("debit", "10800", D("11000.00")), ("credit", "40100", D("10000.00")), ("credit", "25500", D("1000.00"))]
    assert (sales.supply, sales.tax, sales.total, sales.status) == (D("10000.00"), D("1000.00"), D("11000.00"), "draft")
    buy = draft(core.KIND_TAX, "PURCHASE", "11000", totalAmount=11000, taxAmount=1000, category="기타영업비용")
    assert shape(buy) == [("debit", "83100", D("10000.00")), ("debit", "13500", D("1000.00")), ("credit", "25100", D("11000.00"))]
    assert buy.status == "draft"


def test_card_cash_receipt_and_bank_mapping():
    card = draft(core.KIND_CARD, "", "5500", usedAmountKrw=5500, vat=500, category="기타영업비용")
    assert shape(card) == [("debit", "83100", D("5000.00")), ("debit", "13500", D("500.00")), ("credit", "25300", D("5500.00"))]
    cash_buy = draft(core.KIND_CASH, "PURCHASE", "3300", totalAmount=3300, vatAmount=300, category="기타영업비용")
    assert shape(cash_buy) == [("debit", "83100", D("3000.00")), ("debit", "13500", D("300.00")), ("credit", "10100", D("3300.00"))]
    cash_sale = draft(core.KIND_CASH, "SALES", "3300", totalAmount=3300, vatAmount=300)
    assert shape(cash_sale) == [("debit", "10100", D("3300.00")), ("credit", "40100", D("3000.00")), ("credit", "25500", D("300.00"))]
    bank_in = draft(core.KIND_BANK, "IN", "100000", category="매출")
    assert shape(bank_in) == [("debit", "10300", D("100000.00")), ("credit", "40100", D("100000.00"))]
    bank_out = draft(core.KIND_BANK, "OUT", "100000", category="기타영업비용")
    assert shape(bank_out) == [("debit", "83100", D("100000.00")), ("credit", "10300", D("100000.00"))]
    interest = draft(core.KIND_BANK, "IN", "12", category=" 이자 수익 ")
    assert shape(interest)[1][1] == "90100"


# ── 미분류 ──

@pytest.mark.parametrize("payload", [{}, {"category": "알수없음"}, {"category": "기타영업비용", "isUnclassified": True}])
def test_unclassified_bank_out_goes_to_suspense_and_is_marked(payload):
    d = draft(core.KIND_BANK, "OUT", "7000", **payload)
    assert shape(d) == [("debit", "9998", D("7000.00")), ("credit", "10300", D("7000.00"))]
    assert d.unclassified and d.status == "needs_review"
    assert d.description.startswith("[미분류]") and "계정 확인 필요" in d.description


def test_unclassified_bank_in_uses_credit_suspense_and_wrong_side_category_is_unclassified():
    assert shape(draft(core.KIND_BANK, "IN", "500"))[1][1] == "9999"
    assert shape(draft(core.KIND_BANK, "IN", "500", category="기타영업비용"))[1][1] == "9999"
    assert shape(draft(core.KIND_BANK, "OUT", "500", category="매출"))[0][1] == "9998"


def test_classified_description_has_no_marker():
    d = draft(core.KIND_BANK, "OUT", "7000", counterparty="가게", category="기타영업비용")
    assert d.description == "[클로브] 은행 출금 가게" and not d.unclassified


# ── 균형·경계 ──

@pytest.mark.parametrize("kind,direction,amount,payload", [
    (core.KIND_TAX, "SALES", "11000", {"totalAmount": 11000, "taxAmount": 1000}),
    (core.KIND_TAX, "PURCHASE", "11000", {"totalAmount": 11000, "taxAmount": 1000}),
    (core.KIND_TAX, "SALES", "11000", {"totalAmount": 11000, "taxAmount": 11000}),
    (core.KIND_TAX, "SALES", "11000", {"totalAmount": 11000, "taxAmount": -5}),
    (core.KIND_CARD, "", "5500", {"usedAmountKrw": 5500, "vat": 500}),
    (core.KIND_CARD, "", "-5500", {"usedAmountKrw": 5500, "vat": 500, "cancelType": "CANCEL"}),
    (core.KIND_CASH, "SALES", "3333.33", {"totalAmount": 3333.33, "vatAmount": 303.03}),
    (core.KIND_CASH, "PURCHASE", "-3300", {"totalAmount": -3300, "vatAmount": -300, "transactionType": "승인취소"}),
    (core.KIND_BANK, "IN", "1", {}),
    (core.KIND_BANK, "OUT", "999999999.99", {}),
])
def test_every_draft_is_balanced_with_positive_lines_and_total_is_supply_plus_tax(kind, direction, amount, payload):
    d = draft(kind, direction, amount, **payload)
    assert v.is_balanced(d) and len(d.lines) >= 2
    assert all(ln.amount > 0 for ln in d.lines)
    assert d.total == d.supply + d.tax > 0
    assert d.status in ("draft", "needs_review")


def test_tax_equal_to_total_is_treated_as_zero_tax():
    d = draft(core.KIND_TAX, "SALES", "11000", totalAmount=11000, taxAmount=11000)
    assert (d.supply, d.tax) == (D("11000.00"), D("0.00")) and len(d.lines) == 2


def test_cancellation_flips_sides_and_keeps_amounts_positive():
    normal = draft(core.KIND_CARD, "", "5500", usedAmountKrw=5500, vat=500, category="기타영업비용")
    cancel = draft(core.KIND_CARD, "", "-5500", usedAmountKrw=5500, vat=500, cancelType="CANCEL", category="기타영업비용")
    assert [(s, a, m) for (s, a, m) in shape(cancel)] == [
        ({"debit": "credit", "credit": "debit"}[s], a, m) for (s, a, m) in shape(normal)]
    assert "취소" in cancel.description and v.is_balanced(cancel)


@pytest.mark.parametrize("kwargs,reason", [
    ({"occurred_on": None, "amount": "1"}, "no_date"),
    ({"occurred_on": DAY, "amount": "0"}, "zero_amount"),
])
def test_unbuildable_items_are_skipped_with_reason(kwargs, reason):
    with pytest.raises(v.VoucherSkip) as exc:
        v.build_draft(core.KIND_BANK, direction="IN", counterparty="", payload={}, **kwargs)
    assert exc.value.reason == reason
    with pytest.raises(v.VoucherSkip) as exc:
        v.build_draft("other", direction="IN", occurred_on=DAY, amount="1", counterparty="", payload={})
    assert exc.value.reason == "unknown_kind"
    with pytest.raises(v.VoucherSkip) as exc:
        v.build_draft(core.KIND_BANK, direction="", occurred_on=DAY, amount="1", counterparty="", payload={})
    assert exc.value.reason == "unknown_bank_direction"


def test_voucher_no_is_deterministic_and_fits_the_column():
    eid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert v.voucher_no(eid, DAY) == "CLOBE-20261001-123456781234"
    assert v.voucher_no(eid, DAY) == v.voucher_no(eid, DAY)


# ── 파일럿 가드·차단 범위 ──

def test_pilot_default_and_env_override(monkeypatch):
    assert v.pilot_businesses() == frozenset({"biz-chicblack"})
    assert v.voucher_enabled(TENANT, PILOT) and not v.voucher_enabled(TENANT, "biz-danharu")
    monkeypatch.setenv(v.PILOT_ENV, "biz-danharu, biz-x")
    assert v.voucher_enabled(TENANT, "biz-danharu") and not v.voucher_enabled(TENANT, PILOT)
    monkeypatch.setenv(v.PILOT_ENV, "")
    assert not v.voucher_enabled(TENANT, PILOT)


def test_blocked_scope_is_excluded_even_when_listed_in_the_pilot_env(monkeypatch):
    monkeypatch.setenv(v.PILOT_ENV, "biz-lylon-e2e,biz-chicblack")
    assert not v.voucher_enabled(TENANT, "biz-lylon-e2e")
    assert not v.voucher_enabled(LYLON_TENANT, PILOT)
    monkeypatch.setenv("OBYS_COLLECTION_BLOCKED_BUSINESS_IDS", PILOT)
    assert not v.voucher_enabled(TENANT, PILOT)


# ── 메모리 연결 ──

class FakeConn:
    def __init__(self, entries=()):
        self.vouchers: list[dict] = []
        self.lines: list[dict] = []
        self.locks: list[str] = []
        self.entries = list(entries)
        self.tx_readonly: list[bool] = []
        self.fail_insert = False

    def transaction(self, readonly=False):
        conn = self

        class Tx:
            async def __aenter__(self):
                self.snap = (list(conn.vouchers), list(conn.lines))
                conn.tx_readonly.append(readonly)

            async def __aexit__(self, exc_type, *_):
                if exc_type:
                    conn.vouchers, conn.lines = self.snap
                return False

        return Tx()

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            self.locks.append(args[0])
        elif sql.startswith("INSERT INTO yeoljeong_journal_vouchers"):
            if self.fail_insert:
                raise RuntimeError("check constraint violated")
            keys = ("id", "tenant_id", "business_id", "voucher_no", "transaction_date", "description", "status",
                    "source_type", "source_id", "supply_amount", "tax_amount", "total_amount", "evidence_source", "created_by")
            self.vouchers.append(dict(zip(keys, args)))
        elif sql.startswith("INSERT INTO yeoljeong_journal_lines"):
            keys = ("id", "voucher_id", "line_no", "side", "account_code", "account_name", "amount", "tax_code", "memo")
            self.lines.append(dict(zip(keys, args)))
        elif sql.startswith("SET LOCAL"):
            pass
        else:
            raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT tenant_id FROM yeoljeong_businesses"):
            return TENANT if args[0] in ("biz-chicblack", "biz-danharu", "biz-lylon-e2e") else None
        assert "FROM yeoljeong_journal_vouchers" in sql
        tenant, business, source_type, source_id = args
        return 1 if any(
            x["tenant_id"] == tenant and x["business_id"] == business and x["source_type"] == source_type
            and x["source_id"] == source_id for x in self.vouchers) else None

    async def fetch(self, sql, *args):
        assert "FROM obys_clobe_ledger_entry" in sql
        tenant, business = args
        done = {x["source_id"] for x in self.vouchers if x["source_type"] == "clobe"}
        return [e for e in self.entries if e["business_id"] == business and e["entry_id"] not in done]


def item(kind=core.KIND_BANK, direction="OUT", amount="7000", payload=None, counterparty="가게"):
    return {"data_kind": kind, "direction": direction, "occurred_on": DAY, "amount": D(amount),
            "counterparty": counterparty, "payload": payload if payload is not None else {"category": "기타영업비용"}}


def create(conn, entry_id, it, business=PILOT, tenant=TENANT):
    return asyncio.run(v.create_draft_for_entry(
        conn, tenant_id=tenant, business_id=business, entry_id=entry_id, item=it, actor="u@x"))


def test_create_writes_one_draft_voucher_with_balanced_lines_and_source_link():
    conn, eid = FakeConn(), uuid.uuid4()
    assert create(conn, eid, item()) == "created"
    (row,) = conn.vouchers
    assert (row["source_type"], row["source_id"], row["business_id"], row["tenant_id"]) == ("clobe", eid, PILOT, TENANT)
    assert row["status"] == "draft" and row["status"] not in ("approved", "posted")
    assert row["total_amount"] == row["supply_amount"] + row["tax_amount"] == D("7000.00")
    assert row["evidence_source"] == "clobe:bank_transaction" and row["created_by"] == "u@x"
    debit = sum(x["amount"] for x in conn.lines if x["side"] == "debit")
    credit = sum(x["amount"] for x in conn.lines if x["side"] == "credit")
    assert debit == credit == D("7000.00") and len(conn.lines) >= 2
    assert [x["line_no"] for x in conn.lines] == list(range(1, len(conn.lines) + 1))
    assert all(x["voucher_id"] == row["id"] for x in conn.lines)
    assert any(str(eid) in k for k in conn.locks)


def test_unclassified_item_is_created_as_needs_review_with_suspense_memo():
    conn = FakeConn()
    assert create(conn, uuid.uuid4(), item(payload={})) == "created"
    assert conn.vouchers[0]["status"] == "needs_review"
    assert "[미분류]" in conn.vouchers[0]["description"]
    assert {x["account_code"] for x in conn.lines} == {"9998", "10300"}
    assert [x["memo"] for x in conn.lines if x["account_code"] == "9998"] == ["미분류 - 계정 확인 필요"]


def test_confirming_twice_creates_one_voucher():
    conn, eid = FakeConn(), uuid.uuid4()
    assert [create(conn, eid, item()) for _ in range(2)] == ["created", "exists"]
    assert len(conn.vouchers) == 1
    assert len(conn.lines) == 2


def test_other_entry_gets_its_own_voucher():
    conn = FakeConn()
    create(conn, uuid.uuid4(), item())
    create(conn, uuid.uuid4(), item())
    assert len(conn.vouchers) == 2 and len({x["voucher_no"] for x in conn.vouchers}) == 2


def test_non_pilot_and_blocked_businesses_write_nothing(monkeypatch):
    conn = FakeConn()
    assert create(conn, uuid.uuid4(), item(), business="biz-danharu") == "disabled"
    monkeypatch.setenv(v.PILOT_ENV, "biz-lylon-e2e")
    assert create(conn, uuid.uuid4(), item(), business="biz-lylon-e2e") == "disabled"
    assert create(conn, uuid.uuid4(), item(), tenant=LYLON_TENANT) == "disabled"
    assert not conn.vouchers and not conn.lines and not conn.locks


def test_unbuildable_item_is_skipped_without_writes():
    conn = FakeConn()
    assert create(conn, uuid.uuid4(), item(amount="0")) == "skipped:zero_amount"
    assert not conn.vouchers


def test_payload_may_arrive_as_json_text():
    conn = FakeConn()
    assert create(conn, uuid.uuid4(), item(payload='{"category": "기타영업비용"}')) == "created"
    assert conn.vouchers[0]["status"] == "draft"


# ── 확정 훅(obys_collection_service._draft_voucher) ──

def test_hook_failure_does_not_propagate_and_rolls_back_only_the_voucher():
    conn = FakeConn()
    conn.fail_insert = True
    asyncio.run(svc._draft_voucher(conn, TENANT, PILOT, uuid.uuid4(), item(), "u@x"))
    assert not conn.vouchers


def test_hook_is_a_noop_outside_the_pilot():
    conn = FakeConn()
    asyncio.run(svc._draft_voucher(conn, TENANT, "biz-danharu", uuid.uuid4(), item(), "u@x"))
    assert not conn.vouchers and not conn.locks and not conn.tx_readonly


def test_hook_creates_a_draft_for_the_pilot():
    conn, eid = FakeConn(), uuid.uuid4()
    asyncio.run(svc._draft_voucher(conn, TENANT, PILOT, eid, item(), "u@x"))
    asyncio.run(svc._draft_voucher(conn, TENANT, PILOT, eid, item(), "u@x"))
    assert len(conn.vouchers) == 1


# ── 백필 스크립트 ──

def _load_backfill():
    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_obys_clobe_vouchers.py"
    spec = importlib.util.spec_from_file_location("backfill_obys_clobe_vouchers", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bf = _load_backfill()


def entries(n=3, business=PILOT):
    return [{"entry_id": uuid.uuid4(), "business_id": business, **item(payload={} if i == 0 else {"category": "기타영업비용"})}
            for i in range(n)]


def test_backfill_dry_run_writes_nothing_and_reports_the_plan():
    conn = FakeConn(entries(3))
    out = asyncio.run(bf.backfill(conn, apply=False, business_id=PILOT))
    assert out["mode"] == "dry-run" and out["pending_entries"] == 3
    assert (out["would_create"], out["unclassified"]) == (3, 1)
    assert not conn.vouchers and not conn.lines
    assert conn.tx_readonly == [True]


def test_backfill_apply_twice_creates_one_voucher_per_entry():
    conn = FakeConn(entries(3))
    first = asyncio.run(bf.backfill(conn, apply=True, business_id=PILOT))
    second = asyncio.run(bf.backfill(conn, apply=True, business_id=PILOT))
    assert first["created"] == 3 and second["pending_entries"] == 0 and second["created"] == 0
    assert len(conn.vouchers) == 3
    assert {x["status"] for x in conn.vouchers} <= {"draft", "needs_review"}
    assert {x["created_by"] for x in conn.vouchers} == {bf.ACTOR}
    assert conn.tx_readonly == [False, False]


def test_backfill_skips_entries_that_already_have_a_voucher():
    rows = entries(2)
    conn = FakeConn(rows)
    create(conn, rows[0]["entry_id"], rows[0])
    out = asyncio.run(bf.backfill(conn, apply=True, business_id=PILOT))
    assert out["pending_entries"] == 1 and len(conn.vouchers) == 2


@pytest.mark.parametrize("business", ["biz-danharu", "biz-lylon-e2e", "biz-missing"])
def test_backfill_refuses_out_of_scope_businesses_before_reading_or_writing(business):
    conn = FakeConn(entries(2, business=business))
    with pytest.raises(bf.BackfillRefused):
        asyncio.run(bf.backfill(conn, apply=True, business_id=business))
    assert not conn.vouchers and not conn.tx_readonly


def test_backfill_cli_requires_business_id_and_rejects_apply_with_dry_run():
    with pytest.raises(SystemExit):
        bf.main([])
    with pytest.raises(SystemExit):
        bf.main(["--business-id", PILOT, "--apply", "--dry-run"])


def test_backfill_defaults_to_dry_run(monkeypatch):
    seen = {}

    async def fake_run(args):
        seen["apply"] = args.apply
        return 0

    monkeypatch.setattr(bf, "run", fake_run)
    assert bf.main(["--business-id", PILOT]) == 0 and seen == {"apply": False}
    assert bf.main(["--business-id", PILOT, "--apply"]) == 0 and seen == {"apply": True}


# ── 마이그레이션·코드 규칙 ──

def test_migration_is_additive_and_adds_only_the_clobe_source():
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "20261014_obys_journal_source_clobe.sql").read_text(encoding="utf-8")
    upper = sql.upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "UPDATE "):
        assert forbidden not in upper
    assert "'clobe'" in sql
    for kept in ("uploaded_ledger_row", "manual_ledger_entry", "card_transaction", "bank_transaction"):
        assert kept in sql


def test_module_never_writes_approved_or_posted_status():
    root = Path(__file__).resolve().parents[2]
    for rel in ("app/services/obys_clobe_voucher.py", "scripts/backfill_obys_clobe_vouchers.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "'approved'" not in text and '"approved"' not in text and "'posted'" not in text and '"posted"' not in text
    assert {v.STATUS_CLEAN, v.STATUS_REVIEW} == {"draft", "needs_review"}
