from __future__ import annotations

import pytest

from app.api import yeoljeong_finance as api
from app.services import pc_agent_collection_queue as queue
from app.services import yeoljeong_finance_service as svc


ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": ""}


def _bank_account() -> dict:
    return {
        "id": "bank-1",
        "business_id": "biz-mia",
        "branch_id": "branch-gangbuk-mia",
        "bank_code": "088",
        "bank_name": "신한은행",
        "account_number_masked": "*******6789",
        "connection_type": "browser",
        "connector_type": "bank-quick-service",
        "institution_code": "shinhan_business",
        "status": "active",
        "platform_account_id": "platform-linked",
    }


def test_quick_credentials_prefer_explicit_platform_account(monkeypatch):
    accounts = [
        {
            "id": "platform-wrong",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "collection_mode": "bank-quick-service",
            "username": "wrong",
            "account_no_masked": "*******1111",
            "password_enc": "wrong-password",
        },
        {
            "id": "platform-linked",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "collection_mode": "bank-quick-service",
            "username": "correct",
            "account_no_masked": "*******6789",
            "password_enc": "correct-password",
            "account_no_enc": "correct-account",
            "account_password_enc": "correct-pin",
            "business_registration_no_enc": "correct-business",
        },
    ]
    monkeypatch.setattr(svc, "_read", lambda name: accounts if name == "platform_accounts" else [])
    monkeypatch.setattr(svc, "_decrypt_secret", lambda value: value)

    resolved = svc._bank_quick_credentials_for_account(
        _bank_account(), business_id="biz-mia", branch_id="branch-gangbuk-mia"
    )

    assert resolved["login_username"] == "correct"
    assert resolved["login_password"] == "correct-password"
    assert resolved["account_no"] == "correct-account"
    assert "wrong-password" not in resolved.values()


def test_quick_credentials_fill_login_from_tenant_scoped_agent_vault(monkeypatch):
    accounts = [
        {
            "id": "platform-linked",
            "service": "shinhan_business",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "collection_mode": "bank-quick-service",
            "username": "",
            "account_no_enc": "account",
            "account_password_enc": "pin",
            "business_registration_no_enc": "business",
        }
    ]
    monkeypatch.setattr(svc, "_read", lambda name: accounts if name == "platform_accounts" else [])
    monkeypatch.setattr(svc, "_decrypt_secret", lambda value: value)
    seen = {}

    def fake_vault(**kwargs):
        seen.update(kwargs)
        return {"login_username": "vault-user", "login_password": "vault-password"}

    monkeypatch.setattr(svc, "_bank_login_from_agent_vault", fake_vault)

    resolved = svc._bank_quick_credentials_for_account(
        _bank_account(),
        business_id="biz-mia",
        branch_id="branch-gangbuk-mia",
        tenant_id="tenant-1",
    )

    assert seen["tenant_id"] == "tenant-1"
    assert seen["service"] == "shinhan_business"
    assert resolved["login_username"] == "vault-user"
    assert resolved["login_password"] == "vault-password"
    assert resolved["account_password"] == "pin"


def test_enqueue_quick_inquiry_uses_secret_free_financial_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path)
    svc._write_secure_file_rows(svc.BANK_ACCOUNTS_LEDGER, [_bank_account()])
    captured = {}

    def fake_enqueue(item):
        captured.update(item)
        return {
            **item,
            "id": "quick-job-1",
            "status": "queued",
            "attempt_count": 0,
            "created_at": "2026-09-12T19:00:00+09:00",
            "updated_at": "2026-09-12T19:00:00+09:00",
        }

    monkeypatch.setattr(queue, "enqueue_collection_item", fake_enqueue)

    result = svc.enqueue_bank_quick_inquiry(
        "bank-1",
        {"date_from": "2026-09-01", "date_to": "2026-09-12", "browser_agent_id": "agent-1"},
        ADMIN,
    )

    assert result["job_id"] == "quick-job-1"
    assert result["status"] == "queued"
    assert captured["queue_type"] == "bank"
    assert captured["min_interval_seconds"] == 300
    assert captured["payload"]["bank_account_id"] == "bank-1"
    serialized = repr(captured).lower()
    assert "password" not in serialized
    assert "secret" not in serialized


def test_get_quick_inquiry_returns_only_result_summary(monkeypatch):
    monkeypatch.setattr(
        queue,
        "queue_snapshot",
        lambda limit=50: [
            {
                "id": "quick-job-1",
                "queue_type": "bank",
                "service": "shinhan_business",
                "business_id": "biz-mia",
                "branch": "branch-gangbuk-mia",
                "status": "succeeded",
                "payload": {"bank_account_id": "bank-1", "date_from": "2026-09-01", "date_to": "2026-09-12"},
                "result": {
                    "bank_collections": [
                        {
                            "bank_account_id": "bank-1",
                            "status": "completed",
                            "collected_rows": 3,
                            "imported_rows": 2,
                            "duplicate_rows": 1,
                            "diagnostics": {"password": "must-not-leak"},
                        }
                    ]
                },
            }
        ],
    )

    result = svc.get_bank_quick_inquiry("quick-job-1", ADMIN)

    assert result["result"] == {
        "collected_rows": 3,
        "imported_rows": 2,
        "duplicate_rows": 1,
        "verified_no_records": False,
        "last_collected_at": "",
    }
    assert "must-not-leak" not in repr(result)


@pytest.mark.asyncio
async def test_quick_inquiry_api_contract(monkeypatch):
    monkeypatch.setattr(
        api.svc,
        "enqueue_bank_quick_inquiry",
        lambda account_id, payload, user: {"job_id": "job-1", "status": "queued", "bank_account_id": account_id},
    )
    monkeypatch.setattr(
        api.svc,
        "get_bank_quick_inquiry",
        lambda job_id, user: {"job_id": job_id, "status": "succeeded", "result": {"imported_rows": 1}},
    )

    posted = await api.enqueue_bank_quick_inquiry(
        "bank-1", api.BankQuickInquiryPayload(date_from="2026-09-01", date_to="2026-09-12"), ADMIN
    )
    fetched = await api.get_bank_quick_inquiry("job-1", ADMIN)

    assert posted["quick_inquiry"]["job_id"] == "job-1"
    assert fetched["quick_inquiry"]["result"]["imported_rows"] == 1
