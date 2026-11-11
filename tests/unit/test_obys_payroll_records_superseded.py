"""급여 현황 목록·합계에서 대체된(superseded) 이전 명세 제외 검증(ACCT-OBYS-PAYROLL-LIST-SUPERSEDED).

증상:
  - 급여 명세를 정정하면 save_payroll 이 새 버전을 만들고 이전 버전의
    statement_payload.superseded_by 에 새 id 를 적는다.
  - GET /api/v1/workspaces/{bid}/payroll/records 가 superseded 를 거르지 않아
    급여 현황 화면(admin.html renderPeopleV41·payrollSummaryHtmlV41)에 같은 직원·같은 달
    명세가 중복 표시되고 합계가 이중 집계됨.

해결:
  - 새 테이블 SQL 및 legacy SQL 모두 statement_payload->>'superseded_by' 가
    비어 있지 않은 행을 기본 제외.
  - include_history=true 쿼리 파라미터가 지정된 경우에만 이전 버전을 포함.
  - 개별 명세 조회(records/{record_id})는 이전 버전도 조회 가능하도록 유지.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.api import obys_workspaces as api

TENANT_ID = "d1695f15-6b68-4929-bc8d-646827363ff9"
OWNER_USER = {
    "tenant_id": TENANT_ID,
    "email": "owner@example.com",
    "current_membership": {"tenant_id": TENANT_ID, "status": "active", "role": "owner"},
}
BUSINESS = {
    "id": "biz-payroll-test",
    "name": "주식회사 오비서급여",
}


@pytest.fixture(autouse=True)
def _stub_business(monkeypatch):
    async def business(user, business_id):
        return BUSINESS

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api, "_require_route_read", lambda *args: None)


@pytest.mark.asyncio
async def test_payroll_records_default_excludes_superseded_v1(monkeypatch):
    """같은 직원·같은 월에 v1(superseded_by=v2 id)과 v2가 있으면 기본 응답에 v2만 있다."""
    v1_row = {
        "id": "stmt-6f00dd0b",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 50000,
        "tax_withholding": 6250,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 6250,
        "net_pay": 43750,
        "amount": 43750,
        "status": "confirmed",
        "created_at": "2026-11-01T10:00:00+09:00",
        "statement_payload": {"superseded_by": "stmt-d924e856"},
    }
    v2_row = {
        "id": "stmt-d924e856",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 60000,
        "tax_withholding": 10000,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 10000,
        "net_pay": 50000,
        "amount": 50000,
        "status": "confirmed",
        "created_at": "2026-11-02T10:00:00+09:00",
        "statement_payload": {},
    }

    async def fake_source_rows(**kwargs):
        assert kwargs["route"] == "payroll"
        assert kwargs["include_history"] is False
        return [("generic", v1_row), ("generic", v2_row)], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    result = await api.workspace_records(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from=None,
        date_to=None,
        status="전체 상태",
        search="",
        limit=200,
        offset=0,
        current_user=OWNER_USER,
        include_history=False,
    )

    records = result["records"]
    assert len(records) == 1
    assert result["count"] == 1
    assert records[0]["id"] == "stmt-d924e856"
    assert records[0]["amount"] == "50000"
    assert records[0]["raw"]["net_pay"] == 50000


@pytest.mark.asyncio
async def test_payroll_records_include_history_returns_both(monkeypatch):
    """include_history=true 면 v1과 v2 둘 다 있다."""
    v1_row = {
        "id": "stmt-6f00dd0b",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 50000,
        "tax_withholding": 6250,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 6250,
        "net_pay": 43750,
        "amount": 43750,
        "status": "confirmed",
        "created_at": "2026-11-01T10:00:00+09:00",
        "statement_payload": {"superseded_by": "stmt-d924e856"},
    }
    v2_row = {
        "id": "stmt-d924e856",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 60000,
        "tax_withholding": 10000,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 10000,
        "net_pay": 50000,
        "amount": 50000,
        "status": "confirmed",
        "created_at": "2026-11-02T10:00:00+09:00",
        "statement_payload": {},
    }

    async def fake_source_rows(**kwargs):
        assert kwargs["route"] == "payroll"
        assert kwargs["include_history"] is True
        return [("generic", v1_row), ("generic", v2_row)], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    result = await api.workspace_records(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from=None,
        date_to=None,
        status="전체 상태",
        search="",
        limit=200,
        offset=0,
        current_user=OWNER_USER,
        include_history=True,
    )

    records = result["records"]
    assert len(records) == 2
    assert result["count"] == 2
    ids = {r["id"] for r in records}
    assert ids == {"stmt-6f00dd0b", "stmt-d924e856"}


@pytest.mark.asyncio
async def test_payroll_records_without_superseded_by_appear_as_is(monkeypatch):
    """superseded_by 가 없는 행은 그대로 나온다."""
    normal_row1 = {
        "id": "stmt-normal-1",
        "employee_name": "소창영",
        "employee_email": "so@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 3000000,
        "tax_withholding": 100000,
        "insurance_deduction": 200000,
        "other_deduction": 0,
        "total_deductions": 300000,
        "net_pay": 2700000,
        "amount": 2700000,
        "status": "confirmed",
        "created_at": "2026-11-05T10:00:00+09:00",
        # statement_payload has no superseded_by
        "statement_payload": {"memo": "정상 급여"},
    }
    normal_row2 = {
        "id": "stmt-normal-2",
        "employee_name": "김직원",
        "employee_email": "kim@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 2000000,
        "tax_withholding": 50000,
        "insurance_deduction": 100000,
        "other_deduction": 0,
        "total_deductions": 150000,
        "net_pay": 1850000,
        "amount": 1850000,
        "status": "draft",
        "created_at": "2026-11-06T10:00:00+09:00",
        # statement_payload has superseded_by as empty string
        "statement_payload": {"superseded_by": ""},
    }

    async def fake_source_rows(**kwargs):
        return [("generic", normal_row1), ("generic", normal_row2)], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    result = await api.workspace_records(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from=None,
        date_to=None,
        status="전체 상태",
        search="",
        limit=200,
        offset=0,
        current_user=OWNER_USER,
        include_history=False,
    )

    ids = [r["id"] for r in result["records"]]
    assert ids == ["stmt-normal-1", "stmt-normal-2"]


@pytest.mark.asyncio
async def test_source_rows_sql_applies_superseded_filter_to_db(monkeypatch):
    """_source_rows()가 DB SQL에 statement_payload->>'superseded_by' 필터를 정상 적용한다."""
    captured_queries = []

    class FakeConn:
        async def fetch(self, sql, *args):
            captured_queries.append((sql, args))
            return []

        async def close(self):
            pass

    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=FakeConn()))
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "_with_payroll_settlement_state", AsyncMock(side_effect=lambda u, rows: rows))

    # 1. include_history=False (default)
    await api._source_rows(
        route="payroll",
        user=OWNER_USER,
        business_id=BUSINESS["id"],
        date_from="2026-11-01",
        date_to="2026-11-30",
        include_history=False,
    )
    assert len(captured_queries) == 1
    sql, params = captured_queries[0]
    assert "yeoljeong_payroll_statements" in sql
    assert "statement_payload->>'superseded_by' IS NULL OR statement_payload->>'superseded_by' = ''" in sql
    assert params == (BUSINESS["id"], "2026-11", "2026-11")

    # 2. include_history=True
    captured_queries.clear()
    await api._source_rows(
        route="payroll",
        user=OWNER_USER,
        business_id=BUSINESS["id"],
        date_from="2026-11-01",
        date_to="2026-11-30",
        include_history=True,
    )
    assert len(captured_queries) == 1
    sql2, params2 = captured_queries[0]
    assert "yeoljeong_payroll_statements" in sql2
    assert "superseded_by" not in sql2
    assert params2 == (BUSINESS["id"], "2026-11", "2026-11")


@pytest.mark.asyncio
async def test_legacy_people_rows_sql_superseded_filter(monkeypatch):
    """_legacy_people_rows()의 SQL도 superseded_by 필터를 기본 적용하고 include_history 시 해제한다."""
    captured_legacy_calls = []

    async def fake_fetch_legacy(sql, *, parameters):
        captured_legacy_calls.append((sql, parameters))
        return []

    monkeypatch.setattr(api, "_fetch_legacy_people", fake_fetch_legacy)
    monkeypatch.setattr(api.acct_purchase, "_acct_company_map", lambda: {"legacy-mia": "biz-mia"})
    from app.services import yeoljeong_finance_service as yfs
    monkeypatch.setattr(yfs, "CANONICAL_BUSINESSES", [{"id": "legacy-mia", "name": "미아점", "registrationNo": "1234567890"}])
    monkeypatch.setattr(api, "_business", AsyncMock(return_value={"id": "legacy-mia", "name": "미아점", "registration_no": "1234567890"}))

    from app.core import obys_tenant
    monkeypatch.setattr(obys_tenant, "is_legacy_obys_tenant", lambda u: True)

    # 1. include_history=False
    await api._legacy_people_rows(
        user=OWNER_USER,
        business_id=BUSINESS["id"],
        route="payroll",
        start="2026-11-01",
        end="2026-11-30",
        include_history=False,
    )
    assert len(captured_legacy_calls) == 1
    legacy_sql, _ = captured_legacy_calls[0]
    assert "statement_payload" in legacy_sql
    assert "superseded_by" in legacy_sql

    # 2. include_history=True
    captured_legacy_calls.clear()
    await api._legacy_people_rows(
        user=OWNER_USER,
        business_id=BUSINESS["id"],
        route="payroll",
        start="2026-11-01",
        end="2026-11-30",
        include_history=True,
    )
    assert len(captured_legacy_calls) == 1
    legacy_sql2, _ = captured_legacy_calls[0]
    assert "superseded_by" not in legacy_sql2


@pytest.mark.asyncio
async def test_workspace_record_detail_fetches_superseded_version(monkeypatch):
    """개별 명세 상세 조회(records/{record_id})는 superseded 된 이전 버전도 404 없이 조회된다."""
    v1_row = {
        "id": "stmt-v1",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 50000,
        "tax_withholding": 6250,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 6250,
        "net_pay": 43750,
        "amount": 43750,
        "status": "confirmed",
        "created_at": "2026-11-01T10:00:00+09:00",
        "statement_payload": {"superseded_by": "stmt-v2"},
    }

    async def fake_source_rows(**kwargs):
        assert kwargs["route"] == "payroll"
        # workspace_record_detail passes include_history=True
        assert kwargs["include_history"] is True
        return [("generic", v1_row)], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    detail = await api.workspace_record_detail(
        business_id=BUSINESS["id"],
        route="payroll",
        record_id="stmt-v1",
        current_user=OWNER_USER,
    )

    assert detail["record"]["id"] == "stmt-v1"
    assert detail["record"]["amount"] == "43750"


@pytest.mark.asyncio
async def test_workspace_summary_excludes_superseded_from_totals(monkeypatch):
    """급여 현황 요약(workspace_summary)도 기본값에서 superseded 명세를 제외하여 이중 합산을 방지한다."""
    v1_row = {
        "id": "stmt-v1",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 50000,
        "tax_withholding": 6250,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 6250,
        "net_pay": 43750,
        "amount": 43750,
        "status": "확정",
        "created_at": "2026-11-01T10:00:00+09:00",
        "statement_payload": {"superseded_by": "stmt-v2"},
    }
    v2_row = {
        "id": "stmt-v2",
        "employee_name": "함준호",
        "employee_email": "ham@example.com",
        "branch": "본점",
        "payroll_month": "2026-11",
        "gross_pay": 60000,
        "tax_withholding": 10000,
        "insurance_deduction": 0,
        "other_deduction": 0,
        "total_deductions": 10000,
        "net_pay": 50000,
        "amount": 50000,
        "status": "확정",
        "created_at": "2026-11-02T10:00:00+09:00",
        "statement_payload": {},
    }

    async def fake_source_rows(**kwargs):
        assert kwargs["route"] == "payroll"
        if kwargs["include_history"]:
            return [("generic", v1_row), ("generic", v2_row)], "yeoljeong_payroll_statements"
        return [("generic", v2_row)], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    # 1. 기본값: v2만 반영되어 50,000원
    summary_default = await api.workspace_summary(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from="2026-11-01",
        date_to="2026-11-30",
        current_user=OWNER_USER,
        include_history=False,
    )
    metric_map = {m["label"]: m["value"] for m in summary_default["metrics"]}
    assert metric_map["DB 건수"] == "1건"
    assert metric_map["원천 합계(제외·보류 포함)"] == "50,000원"

    # 2. include_history=True: v1 + v2 반영되어 43,750 + 50,000 = 93,750원
    summary_history = await api.workspace_summary(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from="2026-11-01",
        date_to="2026-11-30",
        current_user=OWNER_USER,
        include_history=True,
    )
    metric_map_hist = {m["label"]: m["value"] for m in summary_history["metrics"]}
    assert metric_map_hist["DB 건수"] == "2건"
    assert metric_map_hist["원천 합계(제외·보류 포함)"] == "93,750원"
