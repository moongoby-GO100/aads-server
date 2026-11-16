"""오비서 V4.1 미연결 메뉴 ① — 경영 리포트·주문·취소·입금 예정·미수·입금 미매칭.

검사 범위
- 다른 조직(테넌트) 사업자는 404, 원천을 읽기 전에 막힌다.
- 원천이 없으면 0 이 아니라 None('수집 전'), 미연동 사업자는 not_configured(live=False).
- 입금 예정·미매칭 상태 판정(입금 자료 없음을 미입금으로 단정하지 않음).
- 경영 리포트: 모든 입력에 같은 일 단위 기간, 급여는 월 전체만·관리자만, 취소 주문 제외.
- admin.html 네 메뉴가 none() 이 아니라 실제 API src() 에 연결됐는지 정적 검사.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import obys_workspaces as api

TENANT = "d1695f15-6b68-4929-bc8d-646827363ff9"
BUSINESS = {"id": "biz-junghwa", "name": "열정국밥 중화점", "registration_no": ""}
OTHER_BUSINESS = {"id": "biz-other", "name": "다른 조직 사업자"}
ROUTES = ["reports", "orders", "receivables", "unmatched"]
ADMIN_HTML = Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html"


def _user(role: str = "owner") -> dict:
    return {"tenant_id": TENANT, "email": f"{role}@example.com",
            "current_membership": {"tenant_id": TENANT, "status": "active", "role": role}}


def _deposit(id_, day, amount, category=None):
    return {"id": id_, "occurred_on": day, "amount": None if amount is None else Decimal(amount),
            "counterparty": "배민", "memo": "정산", "account_label": "기업 123", "category": category,
            "source": "manual"}


def _settlement(id_, day, amount):
    return {"id": id_, "branch": "중화점", "occurred_on": day, "settlement_id": f"S-{id_}",
            "platform": "baemin", "settlement_amount": amount}


def _order(id_, day, amount, status="배달완료"):
    return {"id": id_, "branch": "중화점", "occurred_on": day, "order_id": f"O-{id_}",
            "platform": "baemin", "gross_amount": amount, "order_status": status}


@pytest.fixture
def sources(monkeypatch):
    """원천 함수 스텁. calls 에 호출 순서를 남긴다."""
    state = {"calls": [], "delivery": {"sales": [], "settlements": []}, "deposits": [], "withdrawals": [],
             "purchases": [], "payroll": [], "legacy": True}

    async def business(user, business_id):
        state["calls"].append("business")
        if business_id != BUSINESS["id"]:
            raise HTTPException(status_code=404, detail="현재 테넌트의 사업자를 찾을 수 없습니다")
        return BUSINESS

    async def delivery(user, business, kind, date_from, date_to):
        state["calls"].append(f"delivery:{kind}")
        if not state["legacy"]:
            return None
        return [dict(row) for row in state["delivery"][kind]
                if (not date_from or row["occurred_on"] >= date_from) and (not date_to or row["occurred_on"] <= date_to)]

    async def bank(user, business_id, date_from, date_to):
        state["calls"].append("bank")
        def keep(rows):
            return [dict(r) for r in rows
                    if (not date_from or r["occurred_on"] >= date_from) and (not date_to or r["occurred_on"] <= date_to)]
        return keep(state["deposits"]), keep(state["withdrawals"])

    async def ledger(*, user, business_id, category, date_from, date_to):
        state["calls"].append(f"ledger:{category}")
        return [(kind, dict(row)) for kind, row in state["purchases"]]

    real_source_rows = api._source_rows

    async def source_rows(**kwargs):
        if kwargs["route"] == "payroll":
            state["calls"].append("payroll")
            return [("generic", dict(row)) for row in state["payroll"]], "yeoljeong_payroll_statements"
        return await real_source_rows(**kwargs)

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api, "_delivery_rows", delivery)
    monkeypatch.setattr(api, "_bank_flows", bank)
    monkeypatch.setattr(api, "_ledger_records", ledger)
    monkeypatch.setattr(api, "_source_rows", source_rows)
    monkeypatch.setattr(api, "_kst_today", lambda: date(2026, 11, 16))
    return state


async def _records(route, user=None, business_id=BUSINESS["id"], date_from=None, date_to=None):
    return await api.workspace_records(business_id, route, date_from, date_to, "전체 상태", "", 200, 0,
                                       user or _user())


# ── 권한·테넌트 격리 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("call", ["records", "summary", "detail"])
async def test_other_organization_business_is_rejected_before_reading_sources(route, call, sources):
    with pytest.raises(HTTPException) as err:
        if call == "records":
            await _records(route, business_id=OTHER_BUSINESS["id"])
        elif call == "summary":
            await api.workspace_summary(OTHER_BUSINESS["id"], route, None, None, _user())
        else:
            await api.workspace_record_detail(OTHER_BUSINESS["id"], route, "2026-10", _user())
    assert err.value.status_code == 404
    assert sources["calls"] == ["business"], "다른 조직 사업자에서 원천을 읽으면 안 된다"


@pytest.mark.asyncio
async def test_real_business_guard_uses_tenant_business_list(monkeypatch):
    async def list_businesses(*, user):
        return [OTHER_BUSINESS]
    monkeypatch.setattr(api.upload_svc, "list_businesses", list_businesses)
    with pytest.raises(HTTPException) as err:
        await api._business(_user(), BUSINESS["id"])
    assert err.value.status_code == 404


def test_four_routes_follow_open_read_contract_like_sales():
    for route in ROUTES:
        assert route in api.ROUTES
        assert route not in api.ROUTE_READ_ROLES
        assert api._require_route_read(_user("viewer"), route) is None


@pytest.mark.asyncio
async def test_non_legacy_tenant_never_reads_delivery_ledger(monkeypatch):
    called = []

    async def connect():
        called.append("connect")
        raise AssertionError("미연동 테넌트가 배달 원장에 접속하면 안 된다")
    monkeypatch.setattr("app.core.obys_tenant.is_legacy_obys_tenant", lambda user: False)
    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    assert await api._delivery_rows(_user(), BUSINESS, "sales", None, None) is None
    assert called == []


def test_legacy_business_id_mapping_matches_previous_rules(monkeypatch):
    canonical = [{"id": "biz-a", "name": "가게 A", "registrationNo": "111-11-11111"},
                 {"id": "biz-b", "name": "가게 B", "registrationNo": ""}]
    monkeypatch.setattr(api.finance_svc, "CANONICAL_BUSINESSES", canonical)
    assert api._legacy_business_id({"id": "biz-a", "registration_no": "1111111111"}) == "biz-a"
    assert api._legacy_business_id({"id": "biz-a", "registration_no": "2222222222"}) is None
    assert api._legacy_business_id({"id": "v41-x", "registration_no": "111-11-11111"}) == "biz-a"
    assert api._legacy_business_id({"id": "biz-b", "registration_no": ""}) == "biz-b"
    assert api._legacy_business_id({"id": "v41-y", "name": "모르는 가게", "registration_no": ""}) is None


# ── 빈 자료·미연동 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["orders", "receivables"])
async def test_unconnected_delivery_business_reports_not_configured(route, sources):
    sources["legacy"] = False
    body = await _records(route)
    assert body["records"] == [] and body["source"] == {"name": "not_configured", "live": False}
    summary = await api.workspace_summary(BUSINESS["id"], route, None, None, _user())
    assert summary["source"]["live"] is False
    assert summary["metrics"] == [{"label": "데이터 원천", "value": "원천 미연동"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["orders", "receivables", "unmatched"])
async def test_connected_but_empty_source_is_live_with_zero_rows(route, sources):
    body = await _records(route)
    assert body["records"] == [] and body["count"] == 0
    assert body["source"]["live"] is True


@pytest.mark.asyncio
async def test_empty_report_months_are_null_not_zero(sources):
    body = await _records("reports", date_from="2026-10-01", date_to="2026-10-31")
    assert body["count"] == 1
    record = body["records"][0]
    raw = record["raw"]
    for key in ("delivery_sales_amount", "purchase_amount", "payroll_gross_amount", "bank_in_amount", "bank_out_amount"):
        assert raw[key] is None, key
    assert record["amount"] is None
    assert record["status"] == "일부 수집 전"
    assert record["display"]["배달앱 매출"] == "수집 전"
    assert record["display"]["통장 입금"] == "수집 전"


# ── null 보존 ───────────────────────────────────────────────────────────────

def test_order_amount_missing_stays_null():
    row = api._order_row({"id": "x", "occurred_on": "2026-11-01", "order_status": "", "gross_amount": ""})
    assert row["gross_amount"] is None and row["status"] == "상태 미수집"
    record = api._record("order", row, BUSINESS["name"])
    assert record["amount"] is None
    assert record["display"]["금액"] == "수집 전"
    assert record["raw"]["gross_amount"] is None


def test_strict_sum_refuses_partial_totals():
    assert api._strict_sum([]) is None
    assert api._strict_sum([Decimal("1"), None]) is None
    assert api._strict_sum([Decimal("1"), Decimal("2")]) == Decimal("3")
    assert api._nullable_amount("12,000") == Decimal("12000")
    assert api._nullable_amount("abc") is None
    assert api._nullable_amount(0) == Decimal("0")


# ── 주문·취소 ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orders_classify_cancel_and_daily_summary_rows(sources):
    sources["delivery"]["sales"] = [
        _order("a", "2026-11-01", 15000),
        _order("b", "2026-11-02", 9000, status="주문취소"),
        _order("c", "2026-11-03", 50000, status="dashboard_summary"),
        _order("d", "2026-10-01", 1000),
    ]
    body = await _records("orders", date_from="2026-11-01", date_to="2026-11-30")
    by_id = {r["id"]: r for r in body["records"]}
    assert set(by_id) == {"a", "b", "c"}
    assert by_id["a"]["status"] == "주문" and by_id["a"]["display"]["금액"] == "15,000원"
    assert by_id["b"]["status"] == "취소"
    assert by_id["c"]["status"] == "일 집계(주문 단위 아님)"
    summary = await api.workspace_summary(BUSINESS["id"], "orders", "2026-11-01", "2026-11-30", _user())
    metrics = {m["label"]: m["value"] for m in summary["metrics"]}
    assert metrics["주문 건수"] == "3건" and metrics["취소"] == "1건"


# ── 입금 예정·미수 / 입금 미매칭 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_receivable_statuses(sources):
    sources["delivery"]["settlements"] = [
        _settlement("s1", "2026-11-03", 100000),   # 같은 일자·금액 입금 있음
        _settlement("s2", "2026-11-05", 70000),    # 지난 날짜, 입금 없음
        _settlement("s3", "2026-11-20", 30000),    # 미래
        _settlement("s4", "2026-11-04", None),     # 금액 수집 전
    ]
    sources["deposits"] = [_deposit("d1", "2026-11-03", "100000"), _deposit("d2", "2026-11-06", "5000")]
    body = await _records("receivables", date_from="2026-11-01", date_to="2026-11-30")
    by_id = {r["id"]: r for r in body["records"]}
    assert by_id["s1"]["status"] == "입금 확인"
    assert by_id["s1"]["raw"]["matched_bank_transaction_id"] == "d1"
    assert by_id["s2"]["status"] == "미입금 · 확인 필요" and by_id["s2"]["display"]["연체일수"] == "11일"
    assert by_id["s3"]["status"] == "입금 예정"
    assert by_id["s4"]["status"] == "금액 수집 전" and by_id["s4"]["amount"] is None
    summary = await api.workspace_summary(BUSINESS["id"], "receivables", "2026-11-01", "2026-11-30", _user())
    metrics = {m["label"]: m["value"] for m in summary["metrics"]}
    assert metrics["미입금 · 확인 필요"] == "1건" and metrics["미입금 금액"] == "70,000원"


@pytest.mark.asyncio
async def test_receivables_without_bank_data_are_not_called_unpaid(sources):
    sources["delivery"]["settlements"] = [_settlement("s2", "2026-11-05", 70000)]
    body = await _records("receivables")
    assert body["records"][0]["status"] == "입금 자료 수집 전"


@pytest.mark.asyncio
async def test_unmatched_deposit_statuses(sources):
    sources["delivery"]["settlements"] = [_settlement("s1", "2026-11-03", 100000)]
    sources["deposits"] = [
        _deposit("d1", "2026-11-03", "100000"),
        _deposit("d2", "2026-11-04", "5000"),
        _deposit("d3", "2026-11-05", "7000", category="카드대금"),
    ]
    body = await _records("unmatched", date_from="2026-11-01", date_to="2026-11-30")
    by_id = {r["id"]: r for r in body["records"]}
    assert by_id["d1"]["status"] == "정산 매칭" and by_id["d1"]["raw"]["matched_settlement_id"] == "s1"
    assert by_id["d2"]["status"] == "미매칭"
    assert by_id["d3"]["status"] == "분류됨 · 정산 미매칭"
    assert [r["id"] for r in body["records"]] == ["d3", "d2", "d1"]


@pytest.mark.asyncio
async def test_unmatched_without_settlement_source_is_labelled(sources):
    sources["legacy"] = False
    sources["deposits"] = [_deposit("d1", "2026-11-03", "100000")]
    body = await _records("unmatched")
    assert body["records"][0]["status"] == "미매칭 · 정산 자료 없음"
    assert body["source"]["name"] == "obys_bank_transactions:정산_미연동"


@pytest.mark.asyncio
async def test_unmatched_detail_finds_record_by_id(sources):
    sources["deposits"] = [_deposit("d9", "2026-11-03", "100")]
    body = await api.workspace_record_detail(BUSINESS["id"], "unmatched", "d9", _user())
    assert body["record"]["id"] == "d9"
    with pytest.raises(HTTPException) as err:
        await api.workspace_record_detail(BUSINESS["id"], "unmatched", "missing", _user())
    assert err.value.status_code == 404


# ── 경영 리포트 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_report_uses_same_period_for_every_input(sources):
    sources["delivery"]["sales"] = [
        _order("a", "2026-10-02", 10000),
        _order("b", "2026-10-03", 4000, status="취소"),
        _order("c", "2026-11-02", 7000),
    ]
    sources["purchases"] = [("ledger", {"occurred_on": "2026-10-05", "total_amount": Decimal("3000")}),
                            ("uploaded", {"occurred_on": "2026-11-05", "amount": Decimal("100")})]
    sources["deposits"] = [_deposit("d1", "2026-10-09", "8000"), _deposit("d2", "2026-11-09", "1")]
    sources["withdrawals"] = [_deposit("w1", "2026-10-10", "2000")]
    sources["payroll"] = [{"id": "p1", "payroll_month": "2026-10", "gross_pay": Decimal("500000")},
                          {"id": "p2", "payroll_month": "2026-11", "gross_pay": Decimal("1")}]
    body = await _records("reports", date_from="2026-10-01", date_to="2026-11-10")
    by_id = {r["id"]: r for r in body["records"]}
    assert list(by_id) == ["2026-11", "2026-10"]
    october, november = by_id["2026-10"]["raw"], by_id["2026-11"]["raw"]
    assert october["delivery_sales_amount"] == "10000"   # 취소 제외
    assert october["purchase_amount"] == "3000"
    assert october["payroll_gross_amount"] == "500000"
    assert october["bank_in_amount"] == "8000" and october["bank_out_amount"] == "2000"
    assert by_id["2026-10"]["status"] == "집계 완료"
    # 11월은 1~10일만 — 월 단위 급여는 부분 기간과 섞지 않는다.
    assert november["period_end"] == "2026-11-10" and november["full_month"] is False
    assert november["payroll_gross_amount"] is None
    assert "월 단위" in by_id["2026-11"]["display"]["인건비"]
    assert november["bank_out_amount"] is None and by_id["2026-11"]["status"] == "일부 수집 전"


@pytest.mark.asyncio
async def test_report_hides_payroll_from_non_managers(sources):
    sources["payroll"] = [{"id": "p1", "payroll_month": "2026-10", "gross_pay": Decimal("500000")}]
    body = await _records("reports", user=_user("viewer"), date_from="2026-10-01", date_to="2026-10-31")
    record = body["records"][0]
    assert record["raw"]["payroll_gross_amount"] is None
    assert "관리자 전용" in record["display"]["인건비"]
    assert "payroll" not in sources["calls"], "관리자가 아니면 급여 원천을 읽지 않는다"


@pytest.mark.asyncio
async def test_report_payroll_with_missing_gross_is_null(sources):
    sources["payroll"] = [{"id": "p1", "payroll_month": "2026-10", "gross_pay": Decimal("1")},
                          {"id": "p2", "payroll_month": "2026-10", "gross_pay": None}]
    body = await _records("reports", date_from="2026-10-01", date_to="2026-10-31")
    assert body["records"][0]["raw"]["payroll_gross_amount"] is None


@pytest.mark.asyncio
async def test_report_default_period_is_six_months_and_range_is_capped(sources):
    body = await _records("reports")
    assert [r["id"] for r in body["records"]] == ["2026-11", "2026-10", "2026-09", "2026-08", "2026-07", "2026-06"]
    with pytest.raises(HTTPException) as err:
        await _records("reports", date_from="2020-01-01", date_to="2026-11-01")
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_report_detail_by_month(sources):
    body = await api.workspace_record_detail(BUSINESS["id"], "reports", "2026-10", _user())
    assert body["record"]["id"] == "2026-10"
    assert body["record"]["raw"]["period_end"] == "2026-10-31"
    with pytest.raises(HTTPException) as err:
        await api.workspace_record_detail(BUSINESS["id"], "reports", "2026-13", _user())
    assert err.value.status_code == 404


# ── 화면 연결 정적 검사 ─────────────────────────────────────────────────────

def _config_line(route: str) -> str:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{route}:C\(.*$", html, re.M)
    assert match, route
    return match.group(0)


@pytest.mark.parametrize("route", ROUTES)
def test_admin_menu_source_is_wired_to_workspace_api(route):
    line = _config_line(route)
    assert "source:none(" not in line
    assert f'source:src("/api/v1/workspaces/{{businessId}}/{route}/records","records"' in line


@pytest.mark.parametrize("route,labels", [
    ("reports", ["보고기간", "배달앱 매출", "매입", "인건비", "통장 입금", "통장 출금", "상태"]),
    ("orders", ["주문번호", "일자", "매출처·채널", "지점", "주문·취소", "금액", "상태"]),
    ("receivables", ["예정번호", "입금처", "지점", "예정일", "금액", "연체일수", "상태"]),
    ("unmatched", ["거래번호", "일자", "계좌", "입금처", "매칭·미매칭", "금액", "상태"]),
])
def test_admin_columns_match_server_display_keys(route, labels):
    line = _config_line(route)
    assert "[" + ",".join(f'"{label}"' for label in labels) + "]" in line
    kind = {"reports": "report", "orders": "order", "receivables": "receivable", "unmatched": "deposit"}[route]
    sample = {
        "report": {"id": "2026-10", "period_start": "2026-10-01", "period_end": "2026-10-31", "missing": [],
                   "status": "집계 완료"},
        "order": api._order_row({"id": "o"}) ,
        "receivable": {**api._settlement_row({"id": "s"}), "status": "입금 예정", "overdue_days": None},
        "deposit": {**_deposit("d", "2026-11-01", "1"), "status": "미매칭"},
    }[kind]
    display = api._record(kind, sample, BUSINESS["name"])["display"]
    assert all(label in display for label in labels), set(labels) - set(display)


def test_admin_static_page_shows_not_collected_instead_of_mock_rows():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert "const rows=()=>[];" in html  # 목업 행 생성기는 빈 배열 그대로
    for route in ("orders", "receivables"):
        assert "register:false" in _config_line(route)


@pytest.mark.asyncio
async def test_report_detail_preserves_filtered_period(sources):
    sources["delivery"]["sales"] = [
        _order("before", "2026-10-01", "900"),
        _order("inside", "2026-10-15", "100"),
    ]
    body = await api.workspace_record_detail(
        BUSINESS["id"], "reports", "2026-10", _user(),
        date_from="2026-10-10", date_to="2026-10-20",
    )
    raw = body["record"]["raw"]
    assert raw["period_start"] == "2026-10-10"
    assert raw["period_end"] == "2026-10-20"
    assert Decimal(raw["delivery_sales_amount"]) == Decimal("100")
    assert raw["payroll_gross_amount"] is None
    with pytest.raises(HTTPException) as err:
        await api.workspace_record_detail(
            BUSINESS["id"], "reports", "2026-10", _user(),
            date_from="2026-11-01", date_to="2026-11-10",
        )
    assert err.value.status_code == 404


def test_report_detail_request_carries_displayed_period():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert "date_from:live.raw.period_start,date_to:live.raw.period_end" in html


@pytest.mark.asyncio
async def test_delivery_reader_preserves_identified_null_and_zero_rows(monkeypatch):
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Connection:
        closed = False

        def transaction(self, *, readonly):
            assert readonly
            return Transaction()

        async def fetch(self, sql, business_id, start, end, limit):
            assert business_id == "legacy-business"
            return [
                {"id": "null", "branch": "", "payload": {"order_id": "O1", "gross_amount": None}},
                {"id": "zero", "branch": "", "payload": {"order_id": "O2", "gross_amount": 0,
                                                        "order_status": "취소"}},
                {"id": "empty", "branch": "", "payload": {}},
            ]

        async def close(self):
            self.closed = True

    connection = Connection()

    async def connect():
        return connection

    monkeypatch.setattr("app.core.obys_tenant.is_legacy_obys_tenant", lambda user: True)
    monkeypatch.setattr(api, "_legacy_business_id", lambda business: "legacy-business")
    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    rows = await api._delivery_rows(_user(), BUSINESS, "sales", None, None)
    assert [row["id"] for row in rows] == ["null", "zero"]
    assert api._order_row(rows[0])["gross_amount"] is None
    assert api._order_row(rows[1])["status"] == "취소"
    assert connection.closed


@pytest.mark.parametrize("route,label", [
    ("receivables", "미입금 금액"), ("unmatched", "미매칭 금액"),
])
def test_empty_summary_amount_is_not_collected(route, label):
    metrics = api._derived_metrics(route, [], "connected")
    assert next(item["value"] for item in metrics if item["label"] == label) == "수집 전"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["orders", "receivables", "unmatched"])
@pytest.mark.parametrize("bounds", [{}, {"date_from": "2026-10-10", "date_to": "2026-10-20"},
                                   {"date_from": "2026-10-10"}, {"date_to": "2026-10-20"}])
async def test_sales_detail_uses_same_period_and_matching_as_list(route, bounds, sources):
    sources["delivery"]["sales"] = [_order("inside", "2026-10-15", "100"),
                                     _order("outside", "2026-09-15", "100")]
    # 기간 밖에도 같은 일자+금액으로 매칭되는 쌍과 중복 후보를 둔다.
    sources["delivery"]["settlements"] = [
        _settlement("inside", "2026-10-15", "100"),
        _settlement("outside", "2026-09-15", "100"),
        _settlement("outside-duplicate", "2026-09-15", "100"),
    ]
    sources["deposits"] = [_deposit("inside", "2026-10-15", "100"),
                           _deposit("outside", "2026-09-15", "100")]
    body = await _records(route, **bounds)
    for listed in body["records"]:
        detail = await api.workspace_record_detail(BUSINESS["id"], route, listed["id"], _user(), **bounds)
        assert detail["record"] == listed
    if "date_from" in bounds:
        with pytest.raises(HTTPException) as err:
            await api.workspace_record_detail(BUSINESS["id"], route, "outside", _user(), **bounds)
        assert err.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("배달완료", "100"), ("취소", "0")])
async def test_report_daily_summary_is_only_fallback_for_days_without_orders(sources, status, expected):
    sources["delivery"]["sales"] = [
        _order("order", "2026-10-15", "100", status),
        _order("summary", "2026-10-15", "100", "dashboard_summary"),
        _order("fallback", "2026-10-16", "200", "dashboard_summary"),
    ]
    body = await _records("reports", date_from="2026-10-01", date_to="2026-10-31")
    assert Decimal(body["records"][0]["raw"]["delivery_sales_amount"]) == Decimal(expected) + Decimal("200")


def test_sales_detail_request_carries_list_period():
    html = ADMIN_HTML.read_text(encoding="utf-8")
    assert '["orders","receivables","unmatched"].includes(state.route)?"?"+new URLSearchParams({date_from:state.dateFrom,date_to:state.dateTo})' in html


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["receivables", "unmatched"])
async def test_outside_matching_candidates_do_not_change_detail_status(route, sources):
    sources["delivery"]["settlements"] = [_settlement("outside", "2026-09-15", "100")]
    sources["deposits"] = [_deposit("outside", "2026-09-15", "100")]
    if route == "receivables":
        sources["delivery"]["settlements"].append(_settlement("inside", "2026-10-15", "100"))
        expected = "입금 자료 수집 전"
    else:
        sources["deposits"].append(_deposit("inside", "2026-10-15", "100"))
        expected = "미매칭 · 정산 자료 없음"
    bounds = {"date_from": "2026-10-10", "date_to": "2026-10-20"}
    listed = (await _records(route, **bounds))["records"][0]
    detail = await api.workspace_record_detail(BUSINESS["id"], route, "inside", _user(), **bounds)
    assert listed["status"] == detail["record"]["status"] == expected
