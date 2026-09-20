from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.obys_tenant import require_legacy_obys_access
from app.services import obys_upload_service as service


def _tenant_user(role: str = "owner") -> dict:
    tenant_id = str(uuid4())
    return {
        "user_id": "user-1",
        "email": "lylon-test@newtalk.kr",
        "tenant_id": tenant_id,
        "current_tenant": {"id": tenant_id, "name": "라일론"},
        "current_membership": {
            "tenant_id": tenant_id,
            "role": role,
            "status": "active",
        },
    }


def test_tenant_session_is_derived_from_active_membership() -> None:
    session = service.tenant_session_for_user(_tenant_user("owner"))
    assert session["tenant"]["name"] == "라일론"
    assert session["permissions"]["role"] == "owner"
    assert session["permissions"]["can_manage_settings"] is True


def test_viewer_cannot_write_ledger() -> None:
    with pytest.raises(HTTPException) as exc_info:
        service._require_write(_tenant_user("viewer"))
    assert exc_info.value.status_code == 403


def test_membership_must_match_active_tenant() -> None:
    user = _tenant_user("admin")
    user["current_membership"]["tenant_id"] = str(uuid4())
    with pytest.raises(HTTPException) as exc_info:
        service.tenant_session_for_user(user)
    assert exc_info.value.status_code == 403


def test_money_total_must_match_supply_plus_tax() -> None:
    assert service._money_parts({"supply_amount": "1000", "tax_amount": "100", "total_amount": "1100"}) == (
        Decimal("1000"), Decimal("100"), Decimal("1100")
    )
    with pytest.raises(HTTPException) as exc_info:
        service._money_parts({"supply_amount": "1000", "tax_amount": "100", "total_amount": "1000"})
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"supply_amount": "NaN", "tax_amount": "0", "total_amount": "0"},
        {"supply_amount": "Infinity", "tax_amount": "0", "total_amount": "0"},
        {"supply_amount": "-1", "tax_amount": "0", "total_amount": "-1"},
        {"supply_amount": "10000000000000000", "tax_amount": "0", "total_amount": "10000000000000000"},
    ],
)
def test_decimal_contract_rejects_invalid_amounts(payload) -> None:
    with pytest.raises(HTTPException) as exc_info:
        service._money_parts(payload)
    assert exc_info.value.status_code == 422


def test_card_public_shape_never_exposes_last4_field() -> None:
    public = service._public_card({"id": "card-1", "card_last4": "1234", "total_amount": 100})
    assert public["card_number_masked"] == "**** **** **** 1234"
    assert "card_last4" not in public


def test_card_is_a_canonical_preview_commit_category() -> None:
    assert service.LEDGER_CATEGORIES == {"sales", "purchase", "transaction", "card"}
    # The legacy evidence-only endpoint remains separate, while spreadsheet
    # imports use the same preview/commit contract as the other three ledgers.
    assert "card" not in service.GENERIC_LEDGER_CATEGORIES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/yeoljeong-finance/session",
        "/api/v1/yeoljeong-finance/ledger-entries",
        "/api/v1/yeoljeong-finance/card-transactions",
        "/api/v1/yeoljeong-finance/card-uploads",
        "/api/v1/yeoljeong-finance/ledger-bank-transactions",
    ],
)
async def test_new_tenant_can_enter_scoped_ledger_routes(path: str) -> None:
    request = type("Request", (), {"url": type("URL", (), {"path": path})()})()
    user = _tenant_user("owner")
    assert await require_legacy_obys_access(request, user) is user


@pytest.mark.asyncio
async def test_foreign_business_is_404_fail_closed() -> None:
    class Conn:
        async def fetchval(self, _query, *_args):
            return False

    with pytest.raises(HTTPException) as exc_info:
        await service._require_business(Conn(), uuid4(), "foreign-business")
    assert exc_info.value.status_code == 404


def test_ui_exposes_four_real_ledger_pages() -> None:
    html = Path("app/static/apps/obys/index.html").read_text(encoding="utf-8")
    module = Path("app/static/apps/obys/modules/ledger-details.js").read_text(encoding="utf-8")
    styles = Path("app/static/apps/obys/modules/ledger-details.css").read_text(encoding="utf-8")
    api = Path("app/api/obys_finance.py").read_text(encoding="utf-8")
    migration = Path("migrations/20260919_obys_bank_ledger_details.sql").read_text(encoding="utf-8")
    for view in ("salesLedger", "purchaseLedger", "bankLedger", "cardLedger"):
        assert f'id="{view}View"' in html
        assert view in module
    for route in ("/ledger-entries", "/card-transactions", "/card-uploads", "/ledger-bank-transactions"):
        assert route in api
    assert "yeoljeong_manual_bank_transactions" in migration
    assert "tenant_id" in migration
    details_migration = Path("migrations/20260919_obys_ledger_details_r2.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS yeoljeong_card_uploads" in details_migration
    assert "DROP " not in details_migration.upper()
    assert "TRUNCATE " not in details_migration.upper()
    assert "업로드 원본 행은 보존 정책에 따라 수정·삭제할 수 없습니다." in module
    assert "min-height: 44px" in styles
    assert 'href="/static/apps/obys/modules/ledger-details.css?v=20260921-r3"' in html
    assert 'src="/static/apps/obys/modules/ledger-details.js?v=20260921-r3"' in html
    assert "엑셀 파일 등록" in module
    assert "등록 양식 내려받기" in module
    assert "data-upload-drop" in module
    assert "data-ledger-search" in module
    assert "ledger-kpis" in module
    # The legacy evidence endpoint remains in the API, but the detail UI uses
    # one canonical spreadsheet flow for all four categories, including card.
    assert '"/card-uploads"' in api
    assert '"/card-uploads"' not in module
    for route in ('"/uploads/preview"', '"/uploads/commit"', '`/uploads?${scope}&category=card`', 'const base = "/uploads"'):
        assert route in module
    for control in ("data-ledger-from", "data-ledger-to", "data-ledger-search", "data-ledger-source"):
        assert control in module
    for action in ("전표 만들기", "전표 검토", "승인", "확정", "역분개"):
        assert action in module
    assert '"/ledger-bank-transactions"' in module
    assert ".ledger-upload-grid" in styles
    assert "/uploads/preview" in module
    assert "/uploads/commit" in module
    assert "전표 검토함" in module
    assert "@media (max-width: 640px)" in styles


def test_journal_migration_is_additive_and_scoped() -> None:
    migration = Path("migrations/20260921_obys_journal_vouchers_r3.sql").read_text(encoding="utf-8")
    assert "yeoljeong_journal_vouchers" in migration
    assert "tenant_id" in migration and "business_id" in migration
    assert "ON yeoljeong_journal_vouchers (tenant_id,business_id,source_type,source_id)" in migration
    assert "draft','needs_review','approved','posted','reversed" in migration
    assert "TRUNCATE " not in migration.upper()
