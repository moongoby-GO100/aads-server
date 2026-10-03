"""클로브 수집 계약의 순수 로직: 읽기 허용목록, 회사 연결 판정, 정규화·해시, 대사, 기간."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_core as core

ALLOWED_TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
LYLON_TENANT = "d1695f15-6b68-4929-bc8d-646827363ff9"
DANHARU_REG, EONNI_REG, OTHER_REG = "1234567890", "2345678901", "3456789012"


def _biz(biz_id, name, reg, tenant=ALLOWED_TENANT):
    return {"id": biz_id, "tenant_id": tenant, "name": name, "registration_no": reg}


BUSINESSES = [
    _biz("biz-danharu", "단하루", "123-45-67890"),
    _biz("biz-eonni-naengmyeon", "언니냉면", "기초등록 필요"),
    _biz("biz-junghwa", "열정국밥 중화점", "345-67-89012"),
    _biz("biz-lylon-e2e", "주식회사 라일론", "456-78-90123", tenant=LYLON_TENANT),
]


# ── 읽기 전용 허용목록 ───────────────────────────────────

def test_collection_allowlist_is_read_only_and_exact():
    assert set(clobe.COLLECTION_DATA_TOOLS) == set(core.DATA_KINDS)
    assert set(clobe.COLLECTION_DATA_TOOLS.values()) <= set(clobe.COLLECTION_TOOLS)
    for name in clobe.COLLECTION_TOOLS:
        ok, reason = clobe.classify_tool(name)
        assert ok, (name, reason)


@pytest.mark.parametrize("name", [
    "create_tax_invoice", "issue_tax_invoice", "file_vat_return", "transfer_funds", "pay_tax", "send_remittance",
    "update_label", "delete_transaction", "get_journal_ledger", "get_employees", "get_payroll_list",
    "unknown_tool", "",
])
async def test_non_allowlisted_tools_are_denied_before_any_network(name, monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("network or db must not be touched")

    monkeypatch.setattr(clobe, "assert_tool_callable", boom)
    with pytest.raises(clobe.ClobeToolDenied):
        await clobe.assert_collection_tool(name)


async def test_read_session_denies_write_tool_without_opening_a_session(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("must not open a session")

    monkeypatch.setattr(clobe.ReadSession, "_open", boom)
    async with clobe.ReadSession() as session:
        with pytest.raises(clobe.ClobeToolDenied):
            await session.call("create_tax_invoice", {"companyId": "c1"})


async def test_read_session_wraps_arguments_and_refreshes_once_on_401(monkeypatch):
    calls: list[dict] = []
    refreshed: list[bool] = []

    class FakeMcp:
        def __init__(self, http, token):
            self.token = token

        async def initialize(self):
            return {}

        async def request(self, method, params=None):
            calls.append({"token": self.token, "params": params})
            if self.token == "old":
                raise clobe.ClobeReauthRequired("mcp_unauthorized")
            return {"structuredContent": {"ok": True}}

    tokens = iter(["old", "new"])

    async def valid_token():
        return next(tokens)

    async def refresh(*, force=False):
        refreshed.append(force)

    class Pool:
        async def execute(self, *_a):
            return None

    async def allowed(_name):
        return None

    monkeypatch.setattr(clobe, "_McpSession", FakeMcp)
    monkeypatch.setattr(clobe, "_valid_access_token", valid_token)
    monkeypatch.setattr(clobe, "refresh_access_token", refresh)
    monkeypatch.setattr(clobe, "get_pool", lambda: Pool())
    monkeypatch.setattr(clobe, "assert_tool_callable", allowed)
    async with clobe.ReadSession() as session:
        out = await session.call("get_tax_invoices", {"companyId": "c1"})
    assert out == {"ok": True}
    assert refreshed == [True]
    assert calls[-1]["params"] == {"name": "get_tax_invoices", "arguments": {"input": {"companyId": "c1"}}}


async def test_read_session_marks_reauth_when_still_unauthorized_after_refresh(monkeypatch):
    marks: list[tuple] = []

    class FakeMcp:
        def __init__(self, http, token):
            pass

        async def initialize(self):
            return {}

        async def request(self, method, params=None):
            raise clobe.ClobeReauthRequired("mcp_unauthorized")

    async def valid_token():
        return "t"

    async def refresh(*, force=False):
        return None

    async def mark(status, error):
        marks.append((status, error))

    async def allowed(_name):
        return None

    monkeypatch.setattr(clobe, "_McpSession", FakeMcp)
    monkeypatch.setattr(clobe, "_valid_access_token", valid_token)
    monkeypatch.setattr(clobe, "refresh_access_token", refresh)
    monkeypatch.setattr(clobe, "_mark", mark)
    monkeypatch.setattr(clobe, "assert_tool_callable", allowed)
    async with clobe.ReadSession() as session:
        with pytest.raises(clobe.ClobeReauthRequired):
            await session.call("get_my_context")
    assert marks == [(clobe.STATUS_REAUTH, "mcp_unauthorized_after_refresh")]


def test_parse_tool_json_rejects_errors_and_non_json():
    assert clobe.parse_tool_json({"structuredContent": {"a": 1}}) == {"a": 1}
    assert clobe.parse_tool_json({"content": [{"type": "text", "text": '{"b": 2}'}]}) == {"b": 2}
    for bad in ({"isError": True, "content": []}, {"content": [{"text": "not json"}]}, {"content": []}):
        with pytest.raises(clobe.ClobeError):
            clobe.parse_tool_json(bad)


def test_parse_companies_requires_company_id():
    ctx = {"companies": [
        {"companyId": "c1", "companyName": " 단하루 ", "businessRegNo": "123-45-67890", "role": "OWNER"},
        {"companyName": "no id"},
        "junk",
    ]}
    assert clobe.parse_companies(ctx) == [
        {"company_id": "c1", "name": "단하루", "reg_no": "1234567890", "role": "OWNER"}]
    with pytest.raises(clobe.ClobeError):
        clobe.parse_companies({})


# ── 회사 ↔ 사업자 연결 판정 ──────────────────────────────

def _company(name, reg):
    return {"company_id": f"cid-{name}", "name": name, "reg_no": reg, "role": "OWNER"}


def test_exact_reg_and_name_links_automatically():
    d = core.decide_link(_company("단하루", DANHARU_REG), BUSINESSES)
    assert d["status"] == "linked" and d["business"]["id"] == "biz-danharu" and d["basis"] == "reg_no_exact"


def test_name_only_match_is_review_never_linked():
    d = core.decide_link(_company("언니냉면", EONNI_REG), BUSINESSES)
    assert d["status"] == "review" and d["business"] is None
    assert d["candidates"] == ["biz-eonni-naengmyeon"] and d["basis"] == "name_only_candidate"


def test_same_reg_but_different_name_is_review_not_equated():
    d = core.decide_link(_company("윤희에프엔비", "3456789012"), BUSINESSES)
    assert d["status"] == "review" and d["basis"] == "reg_no_match_needs_review"


def test_unmatched_company_is_not_equated_with_yeoljeong():
    d = core.decide_link(_company("열정국밥", "9999999999"), BUSINESSES)
    assert d["status"] == "review" and d["business"] is None and d["basis"] == "name_only_candidate"
    d2 = core.decide_link(_company("전혀다른상호", "9999999999"), BUSINESSES)
    assert d2["status"] == "review" and d2["basis"] == "no_candidate" and d2["candidates"] == []


def test_lylon_is_blocked_by_registration_overlap():
    d = core.decide_link(_company("주식회사 라일론", "4567890123"), BUSINESSES)
    assert d["status"] == "blocked" and d["business"] is None


def test_lylon_business_is_never_a_candidate_even_by_name():
    d = core.decide_link(_company("라일론", "9999999999"), BUSINESSES)
    assert d["status"] == "review" and "biz-lylon-e2e" not in d["candidates"]


def test_businesses_outside_collection_scope_are_not_candidates():
    outside = [_biz("biz-x", "신규상점", "111-11-11111", tenant="aaaaaaaa-0000-0000-0000-000000000000")]
    d = core.decide_link(_company("신규상점", "1111111111"), outside)
    assert d["status"] == "review" and d["candidates"] == []


def test_admin_approval_rules():
    eonni = {"name": "언니냉면", "reg_no": EONNI_REG}
    biz = BUSINESSES[1]
    assert core.check_admin_approval(eonni, biz, name_evidence=False) == (False, "name_evidence_required")
    assert core.check_admin_approval(eonni, biz, name_evidence=True) == (True, "admin_name_evidence")
    assert core.check_admin_approval(eonni, BUSINESSES[0], name_evidence=True) == (False, "reg_no_conflict")
    assert core.check_admin_approval(eonni, BUSINESSES[3], name_evidence=True) == (False, "business_blocked")
    assert core.check_admin_approval({"name": "열정국밥", "reg_no": OTHER_REG}, BUSINESSES[2], name_evidence=False) == (
        True, "reg_no_exact_admin")
    assert core.check_admin_approval({"name": "전혀", "reg_no": ""}, BUSINESSES[1], name_evidence=True) == (
        False, "name_mismatch")


def test_blocked_tenant_cannot_be_unblocked_by_allow_env(monkeypatch):
    monkeypatch.setenv("OBYS_CLOBE_COLLECTION_TENANT_IDS", LYLON_TENANT)
    assert not core.tenant_in_scope(LYLON_TENANT)
    monkeypatch.setenv("OBYS_COLLECTION_BLOCKED_TENANT_IDS", "")
    assert not core.tenant_in_scope(LYLON_TENANT)


# ── 정규화 · 해시 ────────────────────────────────────────

BANK = {"transactionId": 77, "accountId": 1, "transactionAt": "2026-09-01T10:11:12", "transactionName": "거래처",
        "inAmount": 1500.0, "outAmount": 0.0, "bankName": "신한", "categoryId": None, "memo": None}
TAX = {"id": "t-1", "type": "SALES", "issueDate": "2026-09-02", "supplyValue": 100.0, "taxAmount": 10.0,
       "totalAmount": 110.0, "contractorCompanyName": "고객", "supplierCompanyName": "우리", "settlementStatus": "OPEN"}


def test_normalize_bank_and_tax():
    n = core.normalize_item(core.KIND_BANK, BANK)
    assert (n["source_key"], n["direction"], n["amount"], n["institution"]) == ("77", "IN", Decimal("1500.00"), "신한")
    assert n["occurred_on"] == date(2026, 9, 1)
    t = core.normalize_item(core.KIND_TAX, TAX)
    assert (t["source_key"], t["direction"], t["counterparty"], t["amount"]) == ("t-1", "SALES", "고객", Decimal("110.00"))


def test_hash_ignores_clobe_side_classification_but_not_source_facts():
    base = core.item_hash(core.KIND_BANK, BANK)
    assert core.item_hash(core.KIND_BANK, {**BANK, "categoryId": 9, "memo": "x", "isUnclassified": False}) == base
    assert core.item_hash(core.KIND_BANK, {**BANK, "inAmount": 1501.0}) != base
    t = core.item_hash(core.KIND_TAX, TAX)
    assert core.item_hash(core.KIND_TAX, {**TAX, "settlementStatus": "DONE", "matchedCount": 3}) == t
    assert core.item_hash(core.KIND_TAX, {**TAX, "totalAmount": 111.0}) != t


def test_keyless_items_get_fingerprint_plus_ordinal():
    raw = {"usedAt": "2026-09-03T10:00:00", "usedAmount": 5000, "merchantName": "가게"}
    rows = []
    for _ in range(2):
        n = core.normalize_item(core.KIND_CARD, raw)
        n["raw"] = raw
        rows.append(n)
    core.assign_source_keys(core.KIND_CARD, rows)
    assert rows[0]["source_key"] != rows[1]["source_key"]
    assert rows[0]["source_key"].endswith(":1") and rows[1]["source_key"].endswith(":2")
    again = [dict(core.normalize_item(core.KIND_CARD, raw), raw=raw) for _ in range(2)]
    core.assign_source_keys(core.KIND_CARD, again)
    assert [r["source_key"] for r in again] == [r["source_key"] for r in rows]


# ── 대사 ─────────────────────────────────────────────────

def test_reconcile_detects_missing_pages_and_sum_drift():
    meta = {"totalElements": 5, "supplyValueSum": 500.0, "taxAmountSum": 50.0, "totalAmountSum": 550.0}
    full = {"supplyValue": Decimal("500"), "taxAmount": Decimal("50"), "totalAmount": Decimal("550")}
    assert core.reconcile(core.KIND_TAX, 5, full, meta)["ok"] is True
    short = core.reconcile(core.KIND_TAX, 4, full, meta)
    assert short["ok"] is False and "count_mismatch" in short["mismatches"]
    drift = core.reconcile(core.KIND_TAX, 5, {**full, "totalAmount": Decimal("540")}, meta)
    assert drift["mismatches"] == ["sum_mismatch:totalAmountSum"]
    skipped = core.reconcile(core.KIND_TAX, 5, {"supplyValue": None, "taxAmount": None, "totalAmount": None}, meta)
    assert skipped["ok"] is True and {c["status"] for c in skipped["sum_checks"]} == {"skipped"}


def test_bank_reconcile_is_count_only():
    meta = {"totalElements": 3, "inAmountSumKrw": 0.0}
    assert core.reconcile(core.KIND_BANK, 3, {}, meta)["ok"] is True
    assert core.reconcile(core.KIND_BANK, 2, {}, meta)["ok"] is False


def test_scrape_summary_reports_partial_source():
    payload = {"assets": [
        {"category": "BANK", "status": "SUCCESS", "scrapedAt": "2026-10-03T06:00:00", "failureCategory": None},
        {"category": "BANK", "status": "ERROR", "scrapedAt": "2026-10-02T06:00:00", "failureCategory": "AUTH"},
        {"category": "TAX_INVOICE", "status": "SUCCESS", "scrapedAt": "2026-10-03T01:00:00"},
    ]}
    bank = core.scrape_summary(payload, core.KIND_BANK)
    assert (bank["asset_total"], bank["asset_error"], bank["failure_categories"]) == (2, 1, ["AUTH"])
    assert bank["source_as_of"].isoformat().startswith("2026-10-02T06:00:00")
    assert core.scrape_summary(payload, core.KIND_CARD)["source_as_of"] is None


def test_resolve_period_and_retry_backoff():
    assert core.resolve_period("2026-09-01", "2026-09-30") == (date(2026, 9, 1), date(2026, 9, 30))
    assert core.resolve_period(None, "2026-09-30")[1] == date(2026, 9, 30)
    for args in (("2026-10-01", "2026-09-01"), ("2024-01-01", "2026-09-01"), ("bad", "2026-09-01")):
        with pytest.raises(ValueError):
            core.resolve_period(*args)
    delays = [core.retry_delay_seconds(n) for n in range(1, 9)]
    assert delays == sorted(delays) and delays[0] == 60 and delays[-1] == 1920 or delays[-1] <= 3600


def test_unsupported_wetax_certificate_is_file_input():
    wetax = next(s for s in core.UNSUPPORTED_SOURCES if s["kind"] == "wetax_tax_payment_certificate")
    assert wetax["supported"] is False and wetax["input_mode"] == "file_upload"


def test_cash_receipt_cancellations_are_signed_negative_for_sum_reconciliation():
    assert core.is_cancellation(core.KIND_CASH, {"transactionType": "취소거래"})
    assert not core.is_cancellation(core.KIND_CASH, {"transactionType": "승인거래"})
    assert not core.is_cancellation(core.KIND_TAX, {"transactionType": "취소거래"})
    assert "취소" in core.CANCEL_SIGN_SQL[core.KIND_CASH]
