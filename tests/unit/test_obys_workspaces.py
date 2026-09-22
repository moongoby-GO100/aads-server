from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.api import obys_workspaces as api


USER = {"tenant_id": "d1695f15-6b68-4929-bc8d-646827363ff9"}
BUSINESS = {
    "id": "biz-lylon-e2e",
    "name": "주식회사 라일론",
    "entity_type": "corporation",
    "tax_type": "일반과세",
}


@pytest.mark.asyncio
async def test_sales_records_are_tenant_business_scoped_and_db_backed(monkeypatch):
    async def businesses(*, user):
        assert user is USER
        return [BUSINESS]

    async def manual(**kwargs):
        assert kwargs["business_id"] == BUSINESS["id"]
        assert kwargs["category"] == "sales"
        return [{
            "id": UUID("11111111-1111-1111-1111-111111111111"),
            "category": "sales",
            "occurred_on": date(2026, 9, 21),
            "counterparty": "라일론 매출처",
            "description": "온라인 주문",
            "supply_amount": Decimal("10000"),
            "tax_amount": Decimal("1000"),
            "total_amount": Decimal("11000"),
            "source": "manual",
        }]

    async def uploaded(**kwargs):
        return []

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "list_manual_entries", manual)
    monkeypatch.setattr(api.upload_svc, "list_ledger_rows", uploaded)

    result = await api.workspace_records(
        business_id=BUSINESS["id"], route="sales", date_from=None, date_to=None,
        status="전체 상태", search="", limit=200, current_user=USER,
    )

    assert result["business"]["name"] == "주식회사 라일론"
    assert result["source"]["live"] is True
    assert result["count"] == 1
    assert result["records"][0]["display"]["매출처·채널"] == "라일론 매출처"
    assert result["records"][0]["display"]["금액"] == "11,000원"


@pytest.mark.asyncio
async def test_configured_empty_route_returns_explicit_zero_not_demo_rows(monkeypatch):
    async def businesses(*, user):
        return [BUSINESS]

    class EmptyConnection:
        async def fetch(self, sql, business_id):
            assert "yeoljeong_employee_join_requests" in sql
            assert business_id == BUSINESS["id"]
            return []

        async def close(self):
            return None

    async def connect():
        return EmptyConnection()

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    result = await api.workspace_records(
        business_id=BUSINESS["id"], route="employees", date_from=None, date_to=None,
        status="전체 상태", search="", limit=200, current_user=USER,
    )
    assert result["records"] == []
    assert result["source"] == {"name": "yeoljeong_employee_join_requests", "live": True}


@pytest.mark.asyncio
async def test_other_tenant_business_is_not_exposed(monkeypatch):
    async def businesses(*, user):
        return [BUSINESS]

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    with pytest.raises(HTTPException) as exc:
        await api.workspace_records(
            business_id="biz-other", route="sales", date_from=None, date_to=None,
            status="전체 상태", search="", limit=200, current_user=USER,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_manual_purchase_is_written_through_existing_ledger_service(monkeypatch):
    async def businesses(*, user):
        return [BUSINESS]

    captured = {}

    async def create_manual_entry(**kwargs):
        captured.update(kwargs)
        return {"id": UUID("22222222-2222-2222-2222-222222222222"), "status": "saved"}

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "create_manual_entry", create_manual_entry)
    payload = api.WorkspaceManualRecord(
        occurred_on="2026-09-22", counterparty="라일론 공급사", description="원재료",
        supply_amount=10000, tax_amount=1000, total_amount=11000,
    )
    result = await api.create_workspace_record(
        business_id=BUSINESS["id"], route="purchases", payload=payload, current_user=USER,
    )
    assert captured["category"] == "purchase"
    assert captured["payload"]["business_id"] == BUSINESS["id"]
    assert result["source"] == "obys_manual_ledger"


def test_v41_static_page_uses_workspace_api_without_seeded_rows():
    html = Path("app/static/apps/obys/mockup-v4-1.html").read_text(encoding="utf-8")
    assert "const rows=()=>[]" in html
    assert "/api/v1/workspaces" in html
    assert "오비서 DB 실데이터" in html
    assert "샘플 데이터<br>" not in html


def test_raw_source_masks_nested_credentials_and_personal_identifiers():
    record = api._record(
        "generic",
        {
            "id": "row-1",
            "payload": {
                "access_token": "plain-token",
                "nested": {"api_key": "plain-key", "safe": "visible"},
            },
            "employee_email": "person@example.com",
            "account_number_masked": "123-***-45",
        },
        BUSINESS["name"],
    )
    assert record["raw"]["payload"]["access_token"] == "[MASKED]"
    assert record["raw"]["payload"]["nested"]["api_key"] == "[MASKED]"
    assert record["raw"]["payload"]["nested"]["safe"] == "visible"
    assert record["raw"]["employee_email"] == "[MASKED]"
    assert record["raw"]["account_number_masked"] == "123-***-45"


def test_date_filter_applies_to_uploaded_rows_and_rejects_reverse_range():
    rows = [
        ("uploaded", {"occurred_on": date(2026, 9, 1)}),
        ("uploaded", {"occurred_on": date(2026, 9, 22)}),
    ]
    assert api._filter_source_dates(rows, "2026-09-10", "2026-09-30") == [rows[1]]
    with pytest.raises(HTTPException) as exc:
        api._filter_source_dates(rows, "2026-09-30", "2026-09-01")
    assert exc.value.status_code == 422
