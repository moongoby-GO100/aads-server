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
async def test_acct_summary_uses_full_total_and_adds_local_rows(monkeypatch):
    async def business(*args):
        return BUSINESS

    async def rows(**kwargs):
        return [
            ("acct-source", {"id": "acct:1:1", "total_amount": Decimal("5"),
                "source_total_count": 4210, "source_total_amount": Decimal("999999")}),
            ("ledger", {"id": "local", "total_amount": Decimal("55000")}),
        ], "acct+obys"

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api, "_source_rows", rows)
    result = await api.workspace_summary("biz-lylon-e2e", "purchases", None, None, USER)
    assert result["metrics"][0]["value"] == "4,211건"
    assert result["metrics"][1]["value"] == "1,054,999원"


@pytest.mark.asyncio
async def test_acct_detail_fetches_older_record_by_id_with_scope(monkeypatch):
    async def business(user, business_id):
        assert user is USER and business_id == BUSINESS["id"]
        return BUSINESS

    async def source(user, business_id, category, **kwargs):
        assert user is USER and business_id == BUSINESS["id"]
        assert category == "purchase" and kwargs["record_id"] == "acct:1:2"
        return [{"id": "acct:1:2", "total_amount": 55}], "acct"

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", source)
    result = await api.workspace_record_detail(BUSINESS["id"], "purchases", "acct:1:2", USER)
    assert result["record"]["id"] == "acct:1:2"


@pytest.mark.asyncio
async def test_acct_detail_rejects_unauthorized_business_before_remote_lookup(monkeypatch):
    async def business(*args):
        raise HTTPException(status_code=404)

    async def source(*args, **kwargs):
        pytest.fail("remote DB must not be queried before ownership validation")

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", source)
    with pytest.raises(HTTPException) as err:
        await api.workspace_record_detail("foreign", "purchases", "acct:1:2", USER)
    assert err.value.status_code == 404


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

    async def no_acct_source(user, business_id, category, date_from=None, date_to=None, **kwargs):
        # 이 사업자는 ACCT 매출 원천이 비어 있다(2026-09-22 실측: ty_mth='1' 0건).
        return [], "acct.source_file/atom_record:wehago:sales"

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "list_manual_entries", manual)
    monkeypatch.setattr(api.upload_svc, "list_ledger_rows", uploaded)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", no_acct_source)

    result = await api.workspace_records(
        business_id=BUSINESS["id"], route="sales", date_from=None, date_to=None,
        status="전체 상태", search="", limit=200, current_user=USER,
    )

    assert result["business"]["name"] == "주식회사 라일론"
    assert result["source"]["live"] is True
    # JSON 전표 조회 0건은 별도 엑셀 원천의 부재를 의미하지 않는다.
    assert "조회조건_전표없음" in result["source"]["name"]
    assert "원천_미적재" not in result["source"]["name"]
    assert result["count"] == 1
    assert result["records"][0]["display"]["매출처·채널"] == "라일론 매출처"
    assert result["records"][0]["display"]["금액"] == "11,000원"


@pytest.mark.asyncio
async def test_purchase_records_prefer_acct_source_over_local_ledger(monkeypatch):
    async def businesses(*, user):
        return [BUSINESS]

    async def manual(**kwargs):
        return []

    async def uploaded(**kwargs):
        return []

    async def acct_source(user, business_id, category, date_from=None, date_to=None, **kwargs):
        assert category == "purchase"
        return [{
            "id": "acct:1234:7",
            "category": "purchase",
            "occurred_on": "2026-08-24",
            "counterparty": "라일론 매입처",
            "counterparty_biz_no": "204-86-48463",
            "description": "원재료",
            "detail_type_label": "과세매입",
            "evidence_label": "세금계산서",
            "debit_account": "원재료",
            "credit_account": "외상매입금",
            "supply_amount": Decimal("100000"),
            "tax_amount": Decimal("10000"),
            "total_amount": Decimal("110000"),
            "status": "확정",
            "source": "acct_wehago_purchase",
        }], "acct.source_file/atom_record:wehago:purchase"

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "list_manual_entries", manual)
    monkeypatch.setattr(api.upload_svc, "list_ledger_rows", uploaded)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", acct_source)

    result = await api.workspace_records(
        business_id=BUSINESS["id"], route="purchases", date_from=None, date_to=None,
        status="전체 상태", search="", limit=200, current_user=USER,
    )

    assert result["count"] == 1
    assert "원천_미적재" not in result["source"]["name"]
    assert result["source"]["name"].startswith("acct.source_file/atom_record:wehago:purchase")
    record = result["records"][0]
    assert record["display"]["매입처"] == "라일론 매입처"
    assert record["display"]["증빙"] == "세금계산서"
    assert record["display"]["구분"] == "과세매입"
    assert record["display"]["금액"] == "110,000원"


@pytest.mark.asyncio
async def test_acct_source_failure_is_not_reported_as_zero(monkeypatch):
    async def businesses(*, user):
        return [BUSINESS]

    async def manual(**kwargs):
        return []

    async def uploaded(**kwargs):
        return []

    async def failing(user, business_id, category, date_from=None, date_to=None, **kwargs):
        raise HTTPException(status_code=502, detail="ACCT 전표 조회 실패")

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "list_manual_entries", manual)
    monkeypatch.setattr(api.upload_svc, "list_ledger_rows", uploaded)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", failing)

    with pytest.raises(HTTPException) as excinfo:
        await api.workspace_records(
            business_id=BUSINESS["id"], route="purchases", date_from=None, date_to=None,
            status="전체 상태", search="", limit=200, current_user=USER,
        )
    assert excinfo.value.status_code == 502


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
async def test_lylon_journals_use_acct_canonical_source(monkeypatch):
    async def businesses(*, user):
        return [BUSINESS]

    async def scope(user, business_id):
        assert user is USER
        assert business_id == BUSINESS["id"]
        return 11, 11

    async def fetch(sql, tenant_id):
        assert tenant_id == 11
        assert "journal_entry" in sql
        assert "e.company_id = 11" in sql
        return [{
            "id": "entry-1",
            "transaction_date": date(2026, 8, 24),
            "description": "라일론 전표",
            "is_closed": False,
            "debit_total": Decimal("11000"),
            "credit_total": Decimal("11000"),
            "source_total_count": 13623,
            "source_total_amount": Decimal("123456789"),
            "status": "균형",
            "lines": [
                {"account_code": "83300", "debit": 11000, "credit": 0},
                {"account_code": "10300", "debit": 0, "credit": 11000},
            ],
        }]

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.acct_purchase, "_authorized_acct_scope", scope)
    monkeypatch.setattr(api.acct_purchase, "_fetch_acct_journals", fetch)

    result = await api.workspace_summary(
        business_id=BUSINESS["id"], route="journals", date_from=None,
        date_to=None, current_user=USER,
    )
    assert result["source"] == {
        "name": "acct.journal_entry", "live": True, "record_count": 13623,
    }
    assert result["metrics"][0]["value"] == "13,623건"
    assert result["metrics"][1]["value"] == "123,456,789원"


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


def test_operational_obys_enters_v41_after_login_with_legacy_recovery():
    index = Path("app/static/apps/obys/index.html").read_text(encoding="utf-8")
    v41 = Path("app/static/apps/obys/mockup-v4-1.html").read_text(encoding="utf-8")
    prd = Path("docs/PRD-OBYS-MOCKUP-V4-1-FINAL.md").read_text(encoding="utf-8")

    assert 'const V41_APP_PATH = "/static/apps/obys/mockup-v4-1.html"' in index
    assert "shouldOpenV41App() && openV41App()" in index
    assert 'params.get("legacy") === "1"' in index
    assert 'id="businessSelect"' in v41
    assert 'id="legacyBtn"' in v41
    assert 'id="logoutBtn"' in v41
    assert "renderSessionRecovery" in v41
    assert "운영 오비서 정식 진입 통합" in prd
    assert api.acct_purchase._acct_company_map()["biz-lylon-e2e"] == (11, 11)


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
