"""오비서 V4.1 미연결 메뉴 ② — 매입처·지점·원가·마진·메뉴 원가·세무 연동 (OBYS-V41-UNWIRED-MENUS-MASTER-20261116).

검사 범위
- 엔드포인트별 권한(테넌트 밖 사업자 404, 원가·마진은 owner/admin 만, 세무 연동은 기존대로 owner/admin)
- 빈 자료는 0건·live=True 로 돌려주고 가짜 행을 만들지 않는다
- 원천이 없는 값은 None 으로 보존되고 화면 표시는 '수집 전'
- 원가·마진은 산식(calc_note)과 근거 건수를 남기고 금액을 추정하지 않는다
- admin.html 다섯 메뉴가 none() 이 아니라 src() 로 연결됐고 열·필터가 서버 표시 키와 맞는다
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import obys_workspaces as api

TENANT = "d1695f15-6b68-4929-bc8d-646827363ff9"
BUSINESS = {"id": "biz-unwired", "name": "미연결 점검 사업자"}
ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/static/apps/obys/admin.html").read_text(encoding="utf-8")
ROUTES = ["suppliers", "branches", "margins", "menu-costs", "tax-connect"]
DEDICATED = ["branches", "margins", "menu-costs", "tax-connect"]


def _user(role: str = "owner") -> dict:
    return {"tenant_id": TENANT, "email": f"{role}@example.com",
            "current_membership": {"tenant_id": TENANT, "status": "active", "role": role}}


class FakeConnection:
    def __init__(self, tables: dict[str, list[dict]], settings=None):
        self.tables = tables
        self.settings = settings
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        assert args[0] == BUSINESS["id"], "사업자 범위 없이 조회하면 안 된다"
        for table, rows in self.tables.items():
            if f"FROM {table}" in sql:
                return [dict(row) for row in rows]
        raise AssertionError(f"예상하지 않은 조회: {sql}")

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        assert "yeoljeong_settings" in sql and "scope = 'ui'" in sql
        return self.settings

    async def close(self):
        return None


@pytest.fixture
def wire(monkeypatch):
    state = {"conn": FakeConnection({}), "ledger": {"sales": [], "purchase": []}, "acct": {}}

    async def business(user, business_id):
        if business_id != BUSINESS["id"]:
            raise HTTPException(status_code=404, detail="현재 테넌트의 사업자를 찾을 수 없습니다")
        return BUSINESS

    async def connect():
        return state["conn"]

    async def ledger(*, user, business_id, category, date_from, date_to):
        return [("ledger", dict(row)) for row in state["ledger"][category]]

    async def acct(user, business_id, category, **kwargs):
        value = state["acct"].get(category)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise HTTPException(status_code=403, detail="사업자 범위가 필요합니다")
        return value, "acct"

    async def local_rows(*args, **kwargs):
        return []

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    monkeypatch.setattr(api, "_ledger_records", ledger)
    monkeypatch.setattr(api, "_local_source_rows", local_rows)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", acct)
    return state


async def _records(route, user=None, business_id=BUSINESS["id"], **kwargs):
    return await api.workspace_records(
        business_id, route, kwargs.get("date_from"), kwargs.get("date_to"),
        kwargs.get("status", "전체 상태"), "", 200, 0, user or _user())


async def _summary(route, user=None):
    return await api.workspace_summary(BUSINESS["id"], route, None, None, user or _user())


# ── 권한 ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_other_tenant_business_is_404_for_every_new_menu(route, wire):
    with pytest.raises(HTTPException) as err:
        await _records(route, business_id="biz-other-tenant")
    assert err.value.status_code == 404
    assert wire["conn"].queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["margins", "tax-connect"])
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_margins_and_tax_connect_are_manager_only(route, role, wire):
    for call in (_records, _summary):
        with pytest.raises(HTTPException) as err:
            await call(route, _user(role))
        assert err.value.status_code == 403
    assert wire["conn"].queries == [], "권한 검사 전에 원천을 조회하면 안 된다"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["branches", "menu-costs"])
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_branches_and_menu_costs_stay_open_to_active_members(route, role, wire):
    wire["conn"] = FakeConnection({"yeoljeong_branches": [], "yeoljeong_inventory_items": []})
    result = await _records(route, _user(role))
    assert result["source"]["live"] is True


def test_margins_role_matches_client_manager_routes():
    assert api.ROUTE_READ_ROLES["margins"] == frozenset({"owner", "admin"})
    restricted = re.search(r"const MANAGER_READ_ROUTES=new Set\((\[.*?\])\)", HTML).group(1)
    assert set(json.loads(restricted)) == set(api.ROUTE_READ_ROLES)


# ── 빈 자료 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("route", DEDICATED)
async def test_empty_source_returns_zero_rows_not_demo(route, wire):
    wire["conn"] = FakeConnection({"yeoljeong_branches": [], "yeoljeong_inventory_items": [],
                                   "yeoljeong_payroll_statements": []}, settings=None)
    result = await _records(route)
    assert result["records"] == [] and result["count"] == 0
    assert result["source"] == {"name": api.DEDICATED_ROUTE_SOURCES[route], "live": True}
    summary = await _summary(route)
    assert summary["source"]["record_count"] == 0
    assert all("원" not in str(m["value"]) or m["label"] == "데이터 원천" for m in summary["metrics"]), \
        "빈 자료에서 금액 합계를 만들면 안 된다"


# ── 지점 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_branches_preserve_null_and_hide_coordinates(wire):
    wire["conn"] = FakeConnection({"yeoljeong_branches": [
        {"id": "br-1", "name": "중화점", "status": "active", "address": "", "sort_order": 1,
         "latitude": 37.6, "longitude": 127.0, "geofence_radius_m": 50, "updated_at": None},
        {"id": "br-2", "name": "망우점", "status": "inactive", "address": "서울 중랑구", "sort_order": 2,
         "latitude": None, "longitude": None, "geofence_radius_m": None, "updated_at": None},
    ]})
    result = await _records("branches")
    first, second = result["records"]
    assert first["display"]["지점"] == "중화점" and first["display"]["지점코드"] == "br-1"
    assert first["display"]["주소"] == "수집 전"
    assert first["display"]["영업시간"] == "수집 전"
    assert first["raw"]["business_hours"] is None
    assert first["display"]["등록검증"] == "출퇴근 위치 등록"
    assert second["display"]["등록검증"] == "출퇴근 위치 미등록"
    assert second["status"] == "휴업"
    assert first["amount"] is None
    assert "latitude" not in json.dumps(result) and "longitude" not in json.dumps(result)


# ── 원가·마진 ────────────────────────────────────────────────────────────────

def test_margin_formula_uses_only_source_values():
    sales = [("ledger", {"occurred_on": "2026-10-03", "total_amount": Decimal("1000000")}),
             ("ledger", {"occurred_on": "2026-10-20", "total_amount": Decimal("500000")}),
             ("ledger", {"occurred_on": "2026-10-21", "total_amount": Decimal("900000"), "status": "제외(중복/취소)"})]
    purchases = [("ledger", {"occurred_on": "2026-10-05", "total_amount": Decimal("600000")})]
    payroll = [{"month": "2026-10", "payroll_count": 2, "labor_amount": Decimal("300000")}]
    (_, row), = api.build_margin_rows(sales, purchases, payroll)
    assert row["sales_amount"] == Decimal("1500000"), "제외 행은 매출에 넣지 않는다"
    assert row["purchase_amount"] == Decimal("600000")
    assert row["gross_margin"] == Decimal("900000")
    assert row["operating_margin"] == Decimal("600000")
    assert row["operating_margin_rate"] == "40.0"
    assert row["status"] == "계산 완료"
    assert "매출총이익 = 매출 − 변동원가(매입)" in row["calc_note"]
    assert "매출 2건" in row["calc_note"] and "급여 2건" in row["calc_note"]


def test_margin_missing_inputs_stay_null_not_zero():
    sales = [("ledger", {"occurred_on": "2026-11-01", "total_amount": Decimal("700000")})]
    rows = dict(api.build_margin_rows(sales, [], [])[0:1])
    row = rows["margin"]
    assert row["purchase_amount"] is None and row["labor_amount"] is None
    assert row["gross_margin"] is None and row["operating_margin"] is None
    assert row["gross_margin_rate"] is None and row["operating_margin_rate"] is None
    assert row["status"] == "수집 전"
    assert "수집 전 항목: 변동원가(매입), 인건비" in row["calc_note"]
    display = api._dedicated_display("margin", row)
    assert display["변동원가"] == "수집 전" and display["마진"] == "수집 전" and display["마진율"] == "수집 전"
    assert display["매출"] == "700,000원"


def test_margin_labor_only_month_is_partial_and_undated_rows_are_reported():
    sales = [("ledger", {"occurred_on": None, "total_amount": Decimal("10")})]
    payroll = [{"month": "2026-09", "payroll_count": 1, "labor_amount": Decimal("100")}]
    (_, row), = api.build_margin_rows(sales, [], payroll)
    assert row["sales_amount"] is None and row["labor_amount"] == Decimal("100")
    assert row["status"] == "수집 전"
    assert "일자 없는 원장 매출 1건" in row["calc_note"]


@pytest.mark.parametrize("acct_sales,expected", [(12, "ACCT 매출 원천 12건"), (None, "ACCT 매출 원천 확인 실패")])
def test_margin_does_not_mix_partial_ledger_when_acct_source_exists(acct_sales, expected):
    sales = [("ledger", {"occurred_on": "2026-10-03", "total_amount": Decimal("100")})]
    purchases = [("ledger", {"occurred_on": "2026-10-03", "total_amount": Decimal("40")})]
    (_, row), = api.build_margin_rows(sales, purchases, [], acct_sales=acct_sales, acct_purchases=0)
    assert row["sales_amount"] is None, "ACCT 원천이 있으면 오비서 원장만의 부분 합계를 매출로 쓰지 않는다"
    assert row["obys_ledger_sales_amount"] == Decimal("100")
    assert row["gross_margin"] is None
    assert row["status"] == "확인 필요"
    assert expected in row["calc_note"]


@pytest.mark.asyncio
async def test_margins_endpoint_reads_ledgers_payroll_and_acct_presence(wire):
    wire["ledger"]["sales"] = [{"occurred_on": "2026-10-03", "total_amount": Decimal("200000")}]
    wire["ledger"]["purchase"] = [{"occurred_on": "2026-10-04", "total_amount": Decimal("50000")}]
    wire["conn"] = FakeConnection({"yeoljeong_payroll_statements": [
        {"month": "2026-10", "payroll_count": 1, "labor_amount": Decimal("100000")}]})
    result = await _records("margins", date_from="2026-10-01", date_to="2026-10-31")
    (record,) = result["records"]
    assert record["id"] == "2026-10:2026-10-01..2026-10-31" and record["status"] == "계산 완료"
    assert record["display"]["마진"] == "50,000원" and record["display"]["마진율"] == "25.0%"
    assert record["calc_note"] and record["raw"]["calc_note"] == record["calc_note"]
    assert record["amount"] is None
    sql, args = wire["conn"].queries[0]
    assert "superseded_by" in sql and args == (BUSINESS["id"], "2026-10", "2026-10")
    summary = await _summary("margins")
    labels = {m["label"]: m["value"] for m in summary["metrics"]}
    assert labels["마진 계산 완료"] == "1건"


# 리뷰 지적 1 — 일 단위 조회 기간에 월 급여 전체를 차감하지 않는다
def test_margin_partial_month_range_does_not_subtract_full_month_payroll():
    sales = [("ledger", {"occurred_on": "2026-11-17", "total_amount": Decimal("1000000")})]
    purchases = [("ledger", {"occurred_on": "2026-11-18", "total_amount": Decimal("400000")})]
    payroll = [{"month": "2026-11", "payroll_count": 3, "labor_amount": Decimal("900000")}]
    (_, row), = api.build_margin_rows(sales, purchases, payroll,
                                      date_from="2026-11-16", date_to="2026-11-20")
    assert row["gross_margin"] == Decimal("600000")
    assert row["labor_amount"] is None, "11월 16~20일 매출에서 11월 전체 급여를 빼면 안 된다"
    assert row["payroll_month_amount"] == Decimal("900000"), "원천 값은 근거로 보존한다"
    assert row["operating_margin"] is None and row["operating_margin_rate"] is None
    assert row["status"] == "일부 수집 전"
    assert (row["period_from"], row["period_to"]) == ("2026-11-16", "2026-11-20")
    assert "2026-11-16~2026-11-20" in row["calc_note"] and "급여 3건" in row["calc_note"]
    display = api._dedicated_display("margin", row)
    assert display["인건비"] == "수집 전" and display["마진"] == "수집 전"
    assert display["기간"] == "2026-11 (2026-11-16~2026-11-20)"


@pytest.mark.parametrize("date_from,date_to", [("2026-11-01", "2026-11-30"), (None, None),
                                               ("2026-10-15", "2026-12-10"), (None, "2026-11-30")])
def test_margin_full_month_coverage_keeps_payroll(date_from, date_to):
    sales = [("ledger", {"occurred_on": "2026-11-17", "total_amount": Decimal("1000000")})]
    purchases = [("ledger", {"occurred_on": "2026-11-18", "total_amount": Decimal("400000")})]
    payroll = [{"month": "2026-11", "payroll_count": 1, "labor_amount": Decimal("100000")}]
    (_, row), = api.build_margin_rows(sales, purchases, payroll, date_from=date_from, date_to=date_to)
    assert row["labor_amount"] == Decimal("100000") and row["operating_margin"] == Decimal("500000")
    assert row["period_from"] is None and row["status"] == "계산 완료"


def test_margin_partial_month_helper_handles_month_edges():
    assert api._month_partially_covered("2026-12", "2026-12-01", "2026-12-31") is None
    assert api._month_partially_covered("2028-02", "2028-02-01", "2028-02-29") is None
    assert api._month_partially_covered("2026-02", "2026-02-01", "2026-02-27") == ("2026-02-01", "2026-02-27")
    assert api._month_partially_covered("2026-10", "2026-10-02", None) == ("2026-10-02", "2026-10-31")


@pytest.mark.asyncio
async def test_margins_endpoint_passes_day_range_to_payroll_guard(wire):
    wire["ledger"]["sales"] = [{"occurred_on": "2026-11-17", "total_amount": Decimal("200000")}]
    wire["ledger"]["purchase"] = [{"occurred_on": "2026-11-18", "total_amount": Decimal("50000")}]
    wire["conn"] = FakeConnection({"yeoljeong_payroll_statements": [
        {"month": "2026-11", "payroll_count": 1, "labor_amount": Decimal("100000")}]})
    (record,) = (await _records("margins", date_from="2026-11-16", date_to="2026-11-20"))["records"]
    assert record["raw"]["labor_amount"] is None
    assert record["display"]["마진"] == "수집 전" and record["display"]["마진율"] == "수집 전"
    assert record["display"]["매출총이익"] == "150,000원"


@pytest.mark.asyncio
async def test_margin_detail_replays_list_period_and_legacy_month(wire, monkeypatch):
    wire["ledger"]["sales"] = [{"occurred_on": "2026-11-17", "total_amount": Decimal("200000")},
                               {"occurred_on": "2026-11-25", "total_amount": Decimal("300000")}]
    wire["ledger"]["purchase"] = [{"occurred_on": "2026-11-18", "total_amount": Decimal("50000")}]
    wire["conn"] = FakeConnection({"yeoljeong_payroll_statements": [
        {"month": "2026-11", "payroll_count": 1, "labor_amount": Decimal("100000")}]})

    async def ledger(*, user, business_id, category, date_from, date_to):
        rows = [("ledger", dict(row)) for row in wire["ledger"][category]]
        return api._filter_source_dates(rows, date_from, date_to)

    monkeypatch.setattr(api, "_ledger_records", ledger)
    (listed,) = (await _records("margins", date_from="2026-11-16", date_to="2026-11-20"))["records"]
    assert listed["id"] == "2026-11:2026-11-16..2026-11-20"
    detail = (await api.workspace_record_detail(BUSINESS["id"], "margins", listed["id"], _user()))["record"]
    for key in ("sales_amount", "purchase_amount", "labor_amount", "operating_margin", "operating_margin_rate"):
        assert detail["raw"][key] == listed["raw"][key]
    assert detail["raw"]["labor_amount"] is None
    (monthly,) = (await _records("margins"))["records"]
    legacy = (await api.workspace_record_detail(BUSINESS["id"], "margins", "2026-11", _user()))["record"]
    assert legacy == monthly
    assert legacy["raw"]["operating_margin"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("record_id", ["2026-11:bad..2026-11-20", "2026-11:2026-11-31..2026-12-01",
                                      "2026-11:2026-11-20..2026-11-16", "2026-11:..", "2026-13"])
async def test_margin_detail_rejects_invalid_period(record_id, wire):
    with pytest.raises(HTTPException) as err:
        await api.workspace_record_detail(BUSINESS["id"], "margins", record_id, _user())
    assert err.value.status_code in (400, 404)


# 리뷰 지적 2 — 마진율 열은 인건비 차감 마진율 하나의 산식만 쓴다
def test_margin_rate_column_never_falls_back_to_gross_rate():
    sales = [("ledger", {"occurred_on": "2026-10-03", "total_amount": Decimal("1000")})]
    purchases = [("ledger", {"occurred_on": "2026-10-04", "total_amount": Decimal("400")})]
    (_, row), = api.build_margin_rows(sales, purchases, [])
    assert row["gross_margin_rate"] == "60.0" and row["operating_margin_rate"] is None
    display = api._dedicated_display("margin", row)
    assert display["마진"] == "수집 전"
    assert display["마진율"] == "수집 전", "인건비가 없으면 매출총이익률로 대신 채우지 않는다"
    assert display["매출총이익률"] == "60.0%"


@pytest.mark.asyncio
async def test_margins_acct_failure_is_not_hidden_as_zero(wire):
    wire["ledger"]["sales"] = [{"occurred_on": "2026-10-03", "total_amount": Decimal("1")}]
    wire["acct"]["sales"] = HTTPException(status_code=502, detail="ACCT down")
    wire["acct"]["purchase"] = []
    wire["conn"] = FakeConnection({"yeoljeong_payroll_statements": []})
    (record,) = (await _records("margins"))["records"]
    assert record["status"] == "확인 필요"
    assert record["raw"]["sales_amount"] is None


# ── 메뉴 원가 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_menu_costs_show_ingredient_price_and_keep_menu_values_pending(wire):
    wire["conn"] = FakeConnection({"yeoljeong_inventory_items": [
        {"id": "inv-1", "branch_id": "br-1", "name": "양파", "category": "채소", "unit": "kg",
         "current_stock": 3, "unit_cost": Decimal("1800"), "supplier": "마켓봄", "updated_at": None},
        {"id": "inv-2", "branch_id": None, "name": "포장용기", "category": None, "unit": None,
         "current_stock": None, "unit_cost": None, "supplier": None, "updated_at": None},
    ]})
    first, second = (await _records("menu-costs"))["records"]
    assert first["display"]["재료 단가"] == "1,800원"
    assert first["display"]["메뉴 원가"] == "수집 전" and first["display"]["적용 메뉴"] == "수집 전"
    assert first["raw"]["menu_cost"] is None and first["raw"]["sale_price"] is None
    assert first["status"] == "레시피 미등록"
    assert "Σ(레시피 수량 × 재료 단가)" in first["calc_note"]
    assert second["display"]["재료 단가"] == "수집 전" and second["raw"]["unit_cost"] is None
    assert second["status"] == "단가 수집 전"
    pending = await api.workspace_records(BUSINESS["id"], "menu-costs", None, None, "단가 수집 전", "", 200, 0, _user())
    assert [r["id"] for r in pending["records"]] == ["inv-2"]


# ── 세무 연동 ────────────────────────────────────────────────────────────────

SETTINGS = {"integrations": [
    {"id": "int-hometax", "service": "hometax", "businessId": BUSINESS["id"], "status": "vault-ready",
     "lastSyncAt": "2026-11-15T09:00:00+09:00", "lastSyncStatus": "completed",
     "username": "secret-user", "password": "secret-pass", "businessRegistrationNo": "123-45-67890"},
    {"id": "int-upload", "service": "tax_invoice_upload", "businessId": BUSINESS["id"],
     "status": "upload_required", "category": "전자계산서"},
    {"id": "int-agent", "service": "accountant_tax_agent", "businessId": BUSINESS["id"]},
    {"id": "int-other", "service": "hometax", "businessId": "biz-other"},
    {"id": "int-baemin", "service": "baemin", "businessId": BUSINESS["id"], "status": "completed"},
]}


@pytest.mark.parametrize("payload", [SETTINGS, json.dumps(SETTINGS)])
def test_tax_connect_rows_whitelist_fields_and_scope(payload):
    rows = dict((row["id"], row) for _, row in api.build_tax_connect_rows(payload, BUSINESS["id"]))
    assert set(rows) == {"int-hometax", "int-upload", "int-agent"}
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "secret-user" not in dumped and "secret-pass" not in dumped and "123-45-67890" not in dumped
    assert rows["int-hometax"]["status"] == "정상"
    assert rows["int-hometax"]["last_success_at"] == "2026-11-15T09:00:00+09:00"
    assert rows["int-upload"]["status"] == "확인 필요" and rows["int-upload"]["last_success_at"] is None
    assert rows["int-agent"]["status"] == "수집 전" and rows["int-agent"]["evidence_scope"] is None


# 리뷰 지적 3 — status 하나만 보고 수집 실패·인증 필요를 '정상'으로 덮지 않는다
@pytest.mark.parametrize("item,expected", [
    ({"status": "vault-ready", "lastSyncStatus": "failed"}, "확인 필요"),
    ({"status": "vault-ready", "lastSyncStatus": "success", "portalStatus": "credential_required"}, "확인 필요"),
    ({"lastSyncStatus": "success", "portalStatus": "credential_required"}, "확인 필요"),
    ({"status": "completed", "portalStatus": "needs_auth"}, "확인 필요"),
    ({"status": "vault-ready", "lastSyncStatus": "completed", "portalStatus": "ready"}, "정상"),
    ({"status": "vault-ready"}, "정상"),
    ({"status": "vault-ready", "lastSyncStatus": "running"}, "running"),
    ({}, "수집 전"),
])
def test_tax_connect_status_checks_every_state_field(item, expected):
    payload = {"integrations": [{"id": "t", "service": "hometax", "businessId": BUSINESS["id"], **item}]}
    ((_, row),) = api.build_tax_connect_rows(payload, BUSINESS["id"])
    assert row["status"] == expected


@pytest.mark.asyncio
async def test_tax_connect_summary_counts_sync_failure_behind_vault_ready(wire):
    settings = {"integrations": [
        {"id": "a", "service": "hometax", "businessId": BUSINESS["id"], "status": "vault-ready",
         "lastSyncStatus": "failed", "lastSyncAt": "2026-11-15T09:00:00+09:00"},
        {"id": "b", "service": "accountant_tax_agent", "businessId": BUSINESS["id"],
         "lastSyncStatus": "success", "portalStatus": "credential_required"},
    ]}
    wire["conn"] = FakeConnection({}, settings=settings)
    records = {r["id"]: r for r in (await _records("tax-connect"))["records"]}
    assert records["a"]["display"]["마지막성공"] == "수집 전", "실패한 수집 시각을 마지막 성공으로 쓰지 않는다"
    labels = {m["label"]: m["value"] for m in (await _summary("tax-connect"))["metrics"]}
    assert labels["인증·확인 필요"] == "2건"


@pytest.mark.parametrize("payload", [None, "", "not-json", {"integrations": "x"}, {}])
def test_tax_connect_rows_handle_missing_settings(payload):
    assert api.build_tax_connect_rows(payload, BUSINESS["id"]) == []


@pytest.mark.asyncio
async def test_tax_connect_endpoint_and_summary(wire):
    wire["conn"] = FakeConnection({}, settings=json.dumps(SETTINGS))
    result = await _records("tax-connect")
    by_id = {r["id"]: r for r in result["records"]}
    assert by_id["int-agent"]["display"]["마지막성공"] == "수집 전"
    assert by_id["int-hometax"]["display"]["서비스"] == "홈택스"
    assert "secret" not in json.dumps(result, ensure_ascii=False)
    labels = {m["label"]: m["value"] for m in (await _summary("tax-connect"))["metrics"]}
    assert labels == {"세무 연동 수": "3건", "인증·확인 필요": "1건",
                      "데이터 원천": "yeoljeong_settings.integrations"}


# ── 매입처(기존 매입 원천 경로 재사용) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_suppliers_keep_existing_purchase_source_path(wire, monkeypatch):
    calls = []

    async def acct(user, business_id, category, **kwargs):
        calls.append(category)
        return [], "acct"

    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", acct)
    result = await _records("suppliers")
    assert calls == ["purchase"]
    assert result["source"]["live"] is True and result["records"] == []
    assert "suppliers" in api.ACCT_SOURCE_ROUTES and "suppliers" not in api.DEDICATED_ROUTE_SOURCES


# ── 화면 연결 정적 검사 ──────────────────────────────────────────────────────

def _config_line(route: str) -> str:
    key = f'"{route}":C(' if "-" in route else f"{route}:C("
    return next(line for line in HTML.splitlines() if line.strip().startswith(key))


@pytest.mark.parametrize("route", ROUTES)
def test_admin_menu_is_wired_with_src_not_none(route):
    line = _config_line(route)
    assert "source:none(" not in line
    assert f'source:src("/workspaces/{{businessId}}/{route}/records","records"' in line


def _assigned(route: str) -> dict:
    key = f'configs["{route}"]' if "-" in route else f"configs.{route}"
    line = next(x for x in HTML.splitlines() if x.strip().startswith(f"Object.assign({key},{{register:false"))
    return {name: json.loads(re.search(name + r":(\[[^\]]*\])", line).group(1)) for name in ("columns", "hierarchy", "status")}


@pytest.mark.parametrize("route,kind", [("branches", "branch"), ("margins", "margin"),
                                        ("menu-costs", "menu-cost"), ("tax-connect", "tax-connect")])
def test_admin_columns_match_server_display_keys(route, kind):
    conf = _assigned(route)
    display_keys = set(api._dedicated_display(kind, {"id": "x"})) | {"사업자"}
    assert set(conf["columns"]) <= display_keys, set(conf["columns"]) - display_keys
    assert set(conf["hierarchy"]) <= set(conf["columns"]), "필터 단계는 표에 있는 열이어야 동작한다"
    assert conf["status"][0] == "전체 상태"


def test_remaining_none_menus_belong_to_other_task():
    remaining = re.findall(r'source:none\("GET /api/v1/workspaces/\{businessId\}/([a-z-]+)/records"', HTML)
    assert set(remaining).isdisjoint(ROUTES)
