"""급여 현황 월 필터링(ACCT-OBYS-PAYROLL-MONTH-FILTER) 검증.

증상:
  - 급여 현황(admin.html#payroll)에서 급여월 2026-10을 선택하면 date_from=2026-10-01, date_to=2026-10-31이 전달됨.
  - _source_rows()의 SQL은 payroll_month('2026-10') 기준으로 3건을 정상 조회함.
  - 그러나 workspace_records()가 _filter_source_dates()를 추가 적용하면서, payroll 행에 날짜 키(work_date 등)가 없어
    created_at(11월 생성)을 기준으로 걸러 10월분 명세가 모두 제외되어 0건이 반환됨.

수정:
  - workspace_records()에서 route == "payroll"인 경우 _filter_source_dates를 적용하지 않고,
    날짜 유효성/범위 검증만 유지함.
  - 다른 라우트(attendance 등)는 기존 work_date/created_at 기준 필터링 동작을 그대로 유지함.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

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


@pytest.mark.asyncio
async def test_payroll_records_includes_october_created_in_november(monkeypatch):
    """payroll_month='2026-10', created_at='2026-11-11' 명세가 date_from=2026-10-01, date_to=2026-10-31에 포함된다."""
    october_row = {
        "id": "pay-stmt-10",
        "employee_name": "소창영",
        "employee_email": "so@example.com",
        "branch": "본점",
        "payroll_month": "2026-10",
        "gross_pay": 3000000,
        "tax_withholding": 80000,
        "insurance_deduction": 200000,
        "other_deduction": 0,
        "total_deductions": 280000,
        "net_pay": 2720000,
        "status": "confirmed",
        "created_at": "2026-11-11T14:30:00+09:00",
    }

    async def fake_source_rows(**kwargs):
        assert kwargs["route"] == "payroll"
        assert kwargs["date_from"] == "2026-10-01"
        assert kwargs["date_to"] == "2026-10-31"
        return [("generic", october_row)], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    result = await api.workspace_records(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from="2026-10-01",
        date_to="2026-10-31",
        status="전체 상태",
        search="",
        limit=200,
        offset=0,
        current_user=OWNER_USER,
    )

    assert result["count"] == 1
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["id"] == "pay-stmt-10"
    assert record["raw"]["payroll_month"] == "2026-10"
    assert record["raw"]["created_at"] == "2026-11-11T14:30:00+09:00"


@pytest.mark.asyncio
async def test_payroll_records_excludes_september_via_sql_filter(monkeypatch):
    """payroll_month='2026-09' 명세는 2026-10-01 ~ 2026-10-31 요청 시 SQL 월 필터에 의해 제외된다."""
    db_rows = [
        {
            "id": "pay-10",
            "business_id": BUSINESS["id"],
            "payroll_month": "2026-10",
            "employee_name": "소창영",
            "tax_withholding": 0,
            "insurance_deduction": 0,
            "other_deduction": 0,
            "total_deductions": 0,
            "gross_pay": 3000000,
            "net_pay": 3000000,
            "status": "confirmed",
            "created_at": "2026-11-04T10:00:00+09:00",
            "deleted_at": None,
        },
        {
            "id": "pay-09",
            "business_id": BUSINESS["id"],
            "payroll_month": "2026-09",
            "employee_name": "소창영",
            "tax_withholding": 0,
            "insurance_deduction": 0,
            "other_deduction": 0,
            "total_deductions": 0,
            "gross_pay": 3000000,
            "net_pay": 3000000,
            "status": "confirmed",
            "created_at": "2026-10-05T10:00:00+09:00",
            "deleted_at": None,
        },
    ]

    class FakeConnection:
        async def fetch(self, sql, business_id, start=None, end=None):
            assert "yeoljeong_payroll_statements" in sql
            assert business_id == BUSINESS["id"]
            # _source_rows의 SQL 조건: ($2 IS NULL OR left(payroll_month,7)>=$2) AND ($3 IS NULL OR left(payroll_month,7)<=$3)
            filtered = []
            for row in db_rows:
                pm = row["payroll_month"][:7]
                if start and pm < start:
                    continue
                if end and pm > end:
                    continue
                filtered.append(row)
            return filtered

        async def close(self):
            return None

    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=FakeConnection()))
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))
    async def fake_with_settlement(user, rows):
        return rows
    monkeypatch.setattr(api, "_with_payroll_settlement_state", fake_with_settlement)

    result = await api.workspace_records(
        business_id=BUSINESS["id"],
        route="payroll",
        date_from="2026-10-01",
        date_to="2026-10-31",
        status="전체 상태",
        search="",
        limit=200,
        offset=0,
        current_user=OWNER_USER,
    )

    ids = [r["id"] for r in result["records"]]
    assert "pay-10" in ids
    assert "pay-09" not in ids


@pytest.mark.asyncio
async def test_payroll_records_invalid_date_format_raises_422(monkeypatch):
    """잘못된 날짜 형식이거나 시작일>종료일이면 422 예외가 발생한다."""
    async def fake_source_rows(**kwargs):
        return [], "yeoljeong_payroll_statements"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    # 1. 잘못된 형식의 date_from
    with pytest.raises(HTTPException) as exc1:
        await api.workspace_records(
            business_id=BUSINESS["id"],
            route="payroll",
            date_from="not-a-date",
            date_to="2026-10-31",
            current_user=OWNER_USER,
        )
    assert exc1.value.status_code == 422
    assert "조회 날짜 형식" in exc1.value.detail

    # 2. 잘못된 형식의 date_to
    with pytest.raises(HTTPException) as exc2:
        await api.workspace_records(
            business_id=BUSINESS["id"],
            route="payroll",
            date_from="2026-10-01",
            date_to="2026-13-40",
            current_user=OWNER_USER,
        )
    assert exc2.value.status_code == 422
    assert "조회 날짜 형식" in exc2.value.detail

    # 3. 시작일이 종료일보다 늦은 경우 (역순 범위)
    with pytest.raises(HTTPException) as exc3:
        await api.workspace_records(
            business_id=BUSINESS["id"],
            route="payroll",
            date_from="2026-10-31",
            date_to="2026-10-01",
            current_user=OWNER_USER,
        )
    assert exc3.value.status_code == 422
    assert "시작일은 종료일보다 늦을 수 없습니다" in exc3.value.detail


@pytest.mark.asyncio
async def test_attendance_route_still_applies_work_date_filter(monkeypatch):
    """attendance 경로는 work_date 기준 필터가 그대로 적용된다."""
    attendance_rows = [
        (
            "generic",
            {
                "id": "att-in-range",
                "employee_name": "김직원",
                "work_date": "2026-10-15",
                "created_at": "2026-10-15T09:00:00+09:00",
                "status": "정상출근",
            },
        ),
        (
            "generic",
            {
                "id": "att-before-range",
                "employee_name": "김직원",
                "work_date": "2026-09-28",
                # created_at이 10월이어도 work_date가 9월이므로 필터되어야 함
                "created_at": "2026-10-01T09:00:00+09:00",
                "status": "정상출근",
            },
        ),
        (
            "generic",
            {
                "id": "att-after-range",
                "employee_name": "김직원",
                "work_date": "2026-11-02",
                "created_at": "2026-11-02T09:00:00+09:00",
                "status": "정상출근",
            },
        ),
    ]

    async def fake_source_rows(**kwargs):
        assert kwargs["route"] == "attendance"
        return list(attendance_rows), "yeoljeong_attendance_records"

    monkeypatch.setattr(api, "_source_rows", fake_source_rows)

    result = await api.workspace_records(
        business_id=BUSINESS["id"],
        route="attendance",
        date_from="2026-10-01",
        date_to="2026-10-31",
        status="전체 상태",
        search="",
        limit=200,
        offset=0,
        current_user=OWNER_USER,
    )

    ids = [r["id"] for r in result["records"]]
    assert ids == ["att-in-range"]
    assert "att-before-range" not in ids
    assert "att-after-range" not in ids
