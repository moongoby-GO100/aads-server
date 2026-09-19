from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from app.services import obys_upload_service as service


pytestmark = pytest.mark.asyncio


def _url() -> str:
    value = os.getenv("OBYS_LEDGER_TEST_DB_URL", "")
    if not value:
        pytest.skip("OBYS_LEDGER_TEST_DB_URL is required for isolated PostgreSQL verification")
    return value


def _user(tenant_id, email: str) -> dict:
    return {
        "tenant_id": str(tenant_id), "email": email, "user_id": str(uuid4()),
        "current_membership": {"tenant_id": str(tenant_id), "status": "active", "role": "owner"},
    }


async def _business(conn, tenant_id, business_id: str, name: str) -> None:
    await conn.execute(
        """INSERT INTO yeoljeong_businesses
               (id,tenant_id,entity_type,name,registration_no,representative,tax_type,
                opened_at,address,memo,sort_order,updated_by)
             VALUES ($1,$2,'corporation',$3,'','','','','','',99,'integration-test')""",
        business_id,
        tenant_id,
        name,
    )


async def test_tenant_isolation_and_all_manual_ledgers(monkeypatch, tmp_path):
    database_url = _url()
    monkeypatch.setenv("OBYS_DATABASE_URL", database_url)
    monkeypatch.setattr(service, "UPLOAD_ROOT", tmp_path / "uploads")
    tenant_a, tenant_b = uuid4(), uuid4()
    business_a, business_b = f"biz-{uuid4().hex[:16]}", f"biz-{uuid4().hex[:16]}"
    conn = await asyncpg.connect(database_url)
    try:
        await _business(conn, tenant_a, business_a, "테넌트 A")
        await _business(conn, tenant_b, business_b, "테넌트 B")
    finally:
        await conn.close()

    user_a, user_b = _user(tenant_a, "a@example.com"), _user(tenant_b, "b@example.com")

    sale = await service.create_manual_entry(
        user=user_a,
        category="sales",
        payload={
            "business_id": business_a,
            "occurred_on": "2026-09-19",
            "counterparty": "거래처",
            "description": "수기 매출",
            "supply_amount": "100.10",
            "tax_amount": "10.01",
            "total_amount": "110.11",
        },
    )
    assert sale["total_amount"] == Decimal("110.11")
    assert len(await service.list_manual_entries(user=user_a, business_id=business_a, category="sales")) == 1
    with pytest.raises(HTTPException) as cross_sale:
        await service.get_manual_entry(user=user_b, category="sales", entry_id=sale["id"])
    assert cross_sale.value.status_code == 404
    changed = await service.update_manual_entry(
        user=user_a,
        category="sales",
        entry_id=sale["id"],
        payload={"description": "수정 매출"},
    )
    assert changed["description"] == "수정 매출"

    card = await service.create_card_transaction(
        user=user_a,
        payload={
            "business_id": business_a,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "merchant": "가맹점",
            "description": "법인카드",
            "supply_amount": "1000",
            "tax_amount": "100",
            "total_amount": "1100",
            "card_last4": "1234",
        },
    )
    assert card["card_number_masked"].endswith("1234")
    assert "card_last4" not in card
    with pytest.raises(HTTPException) as cross_card:
        await service.get_card_transaction(user=user_b, transaction_id=card["id"])
    assert cross_card.value.status_code == 404

    bank = await service.create_bank_transaction(
        user=user_a,
        payload={
            "business_id": business_a,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "direction": "out",
            "amount": 2200,
            "balance": 100000,
            "counterparty": "공급처",
            "memo": "식자재",
            "category": "매입",
            "account_label": "신한 운영계좌",
        },
    )
    assert bank["amount"] == 2200
    with pytest.raises(HTTPException) as cross_bank:
        await service.get_bank_transaction(user=user_b, transaction_id=bank["id"])
    assert cross_bank.value.status_code == 404

    upload = await service.create_card_upload(
        user=user_a,
        business_id=business_a,
        filename="card.csv",
        content_type="text/csv",
        data=b"date,amount\n2026-09-19,1100",
    )
    assert upload["status"] == "pending_review"
    assert len(await service.list_card_uploads(user=user_a, business_id=business_a)) == 1

    await service.delete_manual_entry(user=user_a, category="sales", entry_id=sale["id"])
    await service.delete_card_transaction(user=user_a, transaction_id=card["id"])
    await service.delete_bank_transaction(user=user_a, transaction_id=bank["id"])
    await service.delete_card_upload(user=user_a, upload_id=upload["id"])
    assert await service.list_manual_entries(user=user_a, business_id=business_a, category="sales") == []
    assert await service.list_card_transactions(user=user_a, business_id=business_a) == []
    assert await service.list_bank_transactions(user=user_a, business_id=business_a) == []
    assert await service.list_card_uploads(user=user_a, business_id=business_a) == []
