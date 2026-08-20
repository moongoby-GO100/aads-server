import base64
import os
import importlib.util
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile


_SERVICE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "yeoljeong_finance_service.py"
_SPEC = importlib.util.spec_from_file_location("yeoljeong_finance_service", _SERVICE_PATH)
service = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(service)


@pytest.fixture(autouse=True)
def isolate_yeoljeong_storage(tmp_path, monkeypatch):
    """Never let unit tests read or write operational files or PostgreSQL."""
    def disable_db(coroutine):
        close = getattr(coroutine, "close", None)
        if close:
            close()
        return None

    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(service, "_run_db", disable_db)


def seed_approved_employee(name="가입 직원", email="member@example.com"):
    service._write("employee_join_requests", [{
        "id": "join-mia", "name": name, "email": email, "address": "서울시 직원 주소",
        "phone": "010-1234-5678", "birth_date": "1990-01-01", "nationality": "대한민국",
        "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])


def disable_delivery_browser_auth(monkeypatch):
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )


def test_delivery_public_error_code_keeps_pc_agent_session_required():
    assert (
        service._delivery_public_error_code("action_required", "PC_AGENT_SESSION_REQUIRED")
        == "PC_AGENT_SESSION_REQUIRED"
    )


def valid_employment_contract(**overrides):
    payload = {
        "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-16", "contract_date": "2026-07-15", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당 지급", "leave_terms": "근로기준법에 따른 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }
    payload.update(overrides)
    return payload


def valid_signature_payload(token, **overrides):
    png = b"\x89PNG\r\n\x1a\n" + (b"signature-test" * 16)
    payload = {
        "token": token,
        "signer_name": "가입 직원",
        "consent": True,
        "consent_version": "yeoljeong-contract-sign-v1",
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "audit_ip": "203.0.113.10",
        "audit_user_agent": "pytest-browser",
    }
    payload.update(overrides)
    return payload


def test_import_card_csv_maps_and_classifies(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = (
        "거래일시,카드종류,승인번호,주문번호,상품명,합계금액,판매자상호,과세금액,부가세\n"
        "2026/06/12 08:49:33,쿠팡와우카드(KB국민),30018576,3100197534848,"
        "자연이삭 백미 보통등급,119800,쿠팡(주),0,0\n"
    )

    result = service.import_file("card.csv", csv_text.encode("utf-8-sig"), "card")

    assert result["import"]["imported_rows"] == 1
    row = result["rows"][0]
    assert row["source_type"] == "card"
    assert row["transaction_date"] == "2026-06-12 08:49:33"
    assert row["amount"] == 119800
    assert row["category"] == "식자재"
    assert row["approval_number"] == "30018576"


def test_import_bank_csv_uses_income_expense_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = "거래일자,적요,입금액,출금액,계좌명\n2026-07-01,배달의민족 정산,55000,,국민은행\n2026-07-02,가스요금,,12000,국민은행\n"

    result = service.import_file("bank.csv", csv_text.encode("utf-8-sig"), "bank")
    rows = result["rows"]

    assert len(rows) == 2
    assert rows[0]["direction"] == "income"
    assert rows[0]["category"] == "배달앱"
    assert rows[1]["direction"] == "expense"
    assert rows[1]["category"] == "공과금"


def test_import_bank_excel_copied_table_uses_tab_delimiter(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    copied = "거래일자\t거래시간\t기재내용\t맡기신금액\t찾으신금액\t계좌번호\n2026.07.03\t11:20:33\t요기요 정산\t88000\t\t110123456789\n"

    result = service.import_file("shinhan.xls.txt", copied.encode("utf-8-sig"), "bank")

    row = result["rows"][0]
    assert result["import"]["imported_rows"] == 1
    assert row["transaction_date"] == "2026-07-03 11:20:33"
    assert row["description"] == "요기요 정산"
    assert row["amount"] == 88000
    assert row["direction"] == "income"
    assert row["account_name"] == "110123456789"


def test_import_transaction_csv_applies_business_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = "거래일자,적요,입금액,출금액,계좌명\n2026-07-01,배달의민족 정산,55000,,신한 중화점\n"

    result = service.import_transaction_csv(
        {
            "service": "shinhan_business",
            "csv_text": csv_text,
            "filename": "bank.csv",
            "business_id": "biz-junghwa",
            "branch": "중화점",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    row = result["transactions"][0]
    assert result["import"]["imported_rows"] == 1
    assert row["source_type"] == "bank"
    assert row["service"] == "shinhan_business"
    assert row["business_id"] == "biz-junghwa"
    assert row["branch"] == "중화점"


def test_duplicate_import_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    csv_text = "거래일자,적요,출금액\n2026-07-02,가스요금,12000\n"

    first = service.import_file("bank.csv", csv_text.encode("utf-8-sig"), "bank")
    second = service.import_file("bank.csv", csv_text.encode("utf-8-sig"), "bank")

    assert first["import"]["imported_rows"] == 1
    assert second["import"]["imported_rows"] == 0
    assert second["import"]["duplicate_rows"] == 1
    assert len(service.list_transactions()) == 1


def test_env_data_dir_does_not_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service.create_transaction({"transaction_date": "2026-07-14", "amount": 1, "description": "테스트"})

    assert os.path.exists(tmp_path / "transactions.json")


def test_save_settings_persists_ui_settings_without_overwriting_automation_config(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"store_name": "열정국밥", "category_rules": [{"category": "식자재"}]}', encoding="utf-8")

    user = {"email": "owner@example.com", "is_admin": True}
    payload = {
        "settings": {
            "businesses": [{"id": "biz-junghwa", "name": "오입력", "registrationNo": "123-45-67890"}],
            "branches": [{"id": "branch-common", "name": "공통", "businessId": "biz-corp"}],
            "accounts": [],
            "staff": [],
            "integrations": [],
            "ignored": [{"id": "not-saved"}],
        }
    }

    saved = service.save_settings(payload, user)
    loaded = service.get_settings(user)

    assert saved["settings"]["businesses"][0]["registrationNo"] == "123-45-67890"
    assert [item["id"] for item in loaded["settings"]["businesses"]] == [
        "biz-junghwa",
        "biz-sungshin",
        "biz-eonni-naengmyeon",
        "biz-mia",
    ]
    assert [item["businessId"] for item in loaded["settings"]["branches"]] == [
        "biz-junghwa",
        "biz-sungshin",
        "biz-eonni-naengmyeon",
        "biz-mia",
    ]
    raw = settings_path.read_text(encoding="utf-8")
    assert "category_rules" in raw
    assert "biz-corp" not in raw
    assert "ignored" not in raw


def test_save_settings_keeps_only_canonical_businesses(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    user = {"email": "owner@example.com", "is_admin": True}
    payload = {
        "settings": {
            "businesses": [
                {"id": "biz-junghwa", "name": "오입력", "registrationNo": "111-11-11111"},
                {"id": "biz-corp", "name": "열정국밥 법인", "registrationNo": "222-22-22222"},
            ],
            "branches": [
                {"id": "branch-common", "name": "공통", "businessId": "biz-corp"},
            ],
            "accounts": [{"id": "acct-corp", "businessId": "biz-corp", "branch": "공통"}],
            "staff": [],
            "integrations": [{"id": "int-corp", "service": "matepos", "businessId": "biz-corp", "branch": "공통"}],
        }
    }

    saved = service.save_settings(payload, user)
    settings = saved["settings"]

    assert [item["name"] for item in settings["businesses"]] == [
        "열정국밥 중화점",
        "열정국밥 성신여대점",
        "언니냉면",
        "열정국밥_미아점",
    ]
    assert {item["id"] for item in settings["businesses"]} == {
        "biz-junghwa",
        "biz-sungshin",
        "biz-eonni-naengmyeon",
        "biz-mia",
    }
    assert {item["businessId"] for item in settings["branches"]} == {
        "biz-junghwa",
        "biz-sungshin",
        "biz-eonni-naengmyeon",
        "biz-mia",
    }
    assert settings["accounts"][0]["businessId"] == "biz-mia"
    assert settings["integrations"][0]["businessId"] == "biz-mia"


def test_save_settings_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    try:
        service.save_settings({"settings": {"businesses": []}}, {"email": "staff@example.com", "is_admin": False})
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("non-admin settings save should fail")


def test_upsert_account_stores_encrypted_password_only(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")

    saved = service.upsert_account(
        {
            "service": "baemin",
            "label": "배민셀프서비스",
            "login_url": "https://biz-member.baemin.com/login",
            "username": "test-user",
            "password": "plain-secret",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    raw = service._read("platform_accounts")

    assert saved["password_masked"] == "********"
    assert "password" not in saved
    assert "password_enc" not in saved
    assert raw[0]["password_enc"] == "encrypted:plain-secret"
    assert "password" not in raw[0]


def test_list_accounts_marks_delivery_account_without_password_as_required(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "status": "credential_registered",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
            }
        ],
    )

    listed = service.list_accounts({"email": "owner@example.com", "is_admin": True})

    assert listed[0]["status"] == "credential_required"
    assert listed[0]["password_masked"] == ""


def test_list_accounts_normalizes_stale_running_delivery_status(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "password_enc": "ciphertext",
                "collection_mode": "portal-csv",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "last_sync_status": "running",
                "portal_status": "running",
            }
        ],
    )

    listed = service.list_accounts({"email": "owner@example.com", "is_admin": True})

    assert listed[0]["status"] == "upload_required"
    assert listed[0]["last_sync_status"] == "upload_required"
    assert listed[0]["portal_status"] == "upload_required"
    raw = service._read("platform_accounts")[0]
    assert raw["last_sync_status"] == "upload_required"
    assert raw["portal_status"] == "upload_required"


def test_queue_delivery_sync_records_queued_status(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    queued = service.queue_delivery_sync(
        {
            "services": ["baemin"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-19",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    statuses = service._read("delivery_collection_status")
    assert queued["queued"] is True
    assert queued["job_id"].startswith("delivery-sync-")
    assert queued["queued_run_ids"]["baemin"] == statuses[0]["id"]
    assert statuses[0]["status"] == "queued"
    assert statuses[0]["business_id"] == "biz-junghwa"
    assert statuses[0]["branch"] == "중화점"


def test_list_collection_status_marks_stale_background_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    old = (datetime.now(service.KST) - timedelta(minutes=16)).isoformat(timespec="seconds")
    service._write(
        "delivery_collection_status",
        [
            {
                "id": "run-stale",
                "job_id": "delivery-sync-stale",
                "service": "baemin",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "status": "running",
                "started_at": old,
                "updated_at": old,
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
            }
        ],
    )

    statuses = service.list_collection_status({"email": "owner@example.com", "is_admin": True}, "biz-junghwa")

    assert statuses[0]["status"] == "failed"
    assert statuses[0]["raw_status"] == "stale"
    assert statuses[0]["error_code"] == "BACKGROUND_SYNC_STALE"
    assert "15분" in statuses[0]["message"]
    raw = service._read("delivery_collection_status")[0]
    assert raw["status"] == "failed"
    assert raw["raw_status"] == "stale"


def test_list_collection_status_marks_stale_queued_without_started_at(tmp_path, monkeypatch):
    """A queued row with only queued_at > 15 min (no started_at) must also become stale."""
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    old = (datetime.now(service.KST) - timedelta(minutes=20)).isoformat(timespec="seconds")
    service._write(
        "delivery_collection_status",
        [
            {
                "id": "run-queued-stale",
                "job_id": "delivery-sync-queued",
                "service": "coupangeats",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "status": "queued",
                "queued_at": old,
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
            }
        ],
    )

    statuses = service.list_collection_status({"email": "owner@example.com", "is_admin": True}, "biz-mia")

    assert statuses[0]["status"] == "failed"
    assert statuses[0]["raw_status"] == "stale"
    assert statuses[0]["error_code"] == "BACKGROUND_SYNC_STALE"
    assert "15분" in statuses[0]["message"]
    assert statuses[0]["finished_at"]
    raw = service._read("delivery_collection_status")[0]
    assert raw["status"] == "failed"
    assert raw["raw_status"] == "stale"


def test_queue_delivery_sync_all_scope_records_registered_branch_services(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin-junghwa",
                "service": "baemin",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "collection_mode": "browser-automation",
            },
            {
                "id": "acct-baemin-mia",
                "service": "baemin",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "collection_mode": "browser-automation",
            },
        ],
    )

    queued = service.queue_delivery_sync(
        {
            "services": ["baemin"],
            "business_id": "all",
            "branch": "전체",
            "date_from": "2026-08-01",
            "date_to": "2026-08-19",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    statuses = service._read("delivery_collection_status")
    assert queued["business_id"] == "all"
    assert queued["branch"] == "전체"
    assert set(queued["queued_run_ids"]) == {
        "biz-junghwa|중화점|baemin",
        "biz-mia|열정국밥_미아점|baemin",
    }
    assert {(row["business_id"], row["branch"], row["service"]) for row in statuses} == {
        ("biz-junghwa", "중화점", "baemin"),
        ("biz-mia", "열정국밥_미아점", "baemin"),
    }


def test_sync_delivery_updates_queued_status_record(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "plain-secret")
    disable_delivery_browser_auth(monkeypatch)
    from app.services import yeoljeong_delivery_collectors as collectors

    monkeypatch.setattr(
        collectors,
        "collect_account",
        lambda account, secret, date_from, date_to: {
            "status": "no_records",
            "error_code": "",
            "records": {},
            "message": "신규 데이터 없음",
        },
    )
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin-junghwa",
                "service": "baemin",
                "username": "owner",
                "password_enc": "ciphertext",
                "collection_mode": "api",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )
    queued = service.queue_delivery_sync(
        {
            "services": ["baemin"],
            "account_id": "acct-baemin-junghwa",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-19",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "account_id": "acct-baemin-junghwa",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-19",
            "sync_job_id": queued["job_id"],
            "queued_run_ids": queued["queued_run_ids"],
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    statuses = service._read("delivery_collection_status")
    assert result["summary"][0]["run_id"] == queued["queued_run_ids"]["baemin"]
    assert statuses[0]["id"] == queued["queued_run_ids"]["baemin"]
    assert statuses[0]["status"] == "partial"
    assert statuses[0]["raw_status"] == "no_records"
    assert len(statuses) == 1


def test_sync_delivery_uses_service_label_for_upload_required_message(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    disable_delivery_browser_auth(monkeypatch)
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-coupangeats-junghwa",
                "service": "coupangeats",
                "username": "owner",
                "collection_mode": "portal-csv",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )

    result = service.sync_delivery(
        {
            "services": ["coupangeats"],
            "account_id": "acct-coupangeats-junghwa",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-19",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "action_required"
    assert "쿠팡이츠 포털 CSV/엑셀" in result["summary"][0]["message"]
    assert "배민 포털" not in result["summary"][0]["message"]


def test_sync_delivery_uses_service_label_for_credential_required_message(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    disable_delivery_browser_auth(monkeypatch)
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-yogiyo-junghwa",
                "service": "yogiyo",
                "username": "owner",
                "collection_mode": "api",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )

    result = service.sync_delivery(
        {
            "services": ["yogiyo"],
            "account_id": "acct-yogiyo-junghwa",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-19",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "action_required"
    assert result["summary"][0]["error_code"] == "MISSING_CREDENTIALS"
    assert "요기요 계정 비밀번호" in result["summary"][0]["message"]
    assert "배민 계정" not in result["summary"][0]["message"]


def test_upsert_financial_account_encrypts_api_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")

    saved = service.upsert_account(
        {
            "service": "shinhan_business",
            "label": "중화점 신한",
            "login_url": "https://bizbank.shinhan.com/",
            "username": "bank-user",
            "api_key": "client-id",
            "client_secret": "client-secret",
            "institution_code": "shinhan",
            "account_no_masked": "110-***-123456",
            "settlement_cycle": "D+1",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "collection_mode": "api",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    raw = service._read("platform_accounts")[0]
    assert saved["password_masked"] == "********"
    assert saved["account_no_masked"] == "110-***-123456"
    assert "api_key" not in saved
    assert "client_secret" not in saved
    assert raw["api_key_enc"] == "encrypted:client-id"
    assert raw["client_secret_enc"] == "encrypted:client-secret"
    assert "api_key" not in raw


def test_upsert_bank_quick_service_encrypts_required_lookup_values(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")

    saved = service.upsert_account(
        {
            "service": "ibk_business",
            "label": "중화점 IBK 빠른조회",
            "username": "quick-user",
            "password": "login-secret",
            "account_no": "12345678901234",
            "account_password": "4321",
            "business_registration_no": "7108604499",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "collection_mode": "bank-quick-service",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    raw = service._read("platform_accounts")[0]
    assert saved["collection_mode"] == "bank-quick-service"
    assert saved["account_no_masked"].endswith("1234")
    assert saved["business_registration_no_masked"].endswith("4499")
    assert raw["password_enc"] == "encrypted:login-secret"
    assert raw["account_no_enc"] == "encrypted:12345678901234"
    assert raw["account_password_enc"] == "encrypted:4321"
    assert raw["business_registration_no_enc"] == "encrypted:7108604499"
    assert "account_no" not in saved
    assert "account_password" not in saved
    assert "business_registration_no" not in saved


def test_upsert_bank_quick_service_requires_account_password_and_business_no(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")

    try:
        service.upsert_account(
            {
                "service": "shinhan_business",
                "username": "quick-user",
                "password": "login-secret",
                "account_no": "110123456789",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "collection_mode": "bank-quick-service",
            },
            {"email": "owner@example.com", "is_admin": True},
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "계좌비밀번호" in str(getattr(exc, "detail", ""))
        assert "사업자번호" in str(getattr(exc, "detail", ""))
    else:
        raise AssertionError("quick service credentials should require account password and business no")


def test_upsert_account_updates_existing_id_and_preserves_bank_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")

    created = service.upsert_account(
        {
            "service": "ibk_business",
            "label": "중화점 IBK 빠른조회",
            "username": "quick-user",
            "password": "login-secret",
            "account_no": "12345678901234",
            "account_password": "4321",
            "business_registration_no": "7108604499",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "collection_mode": "bank-quick-service",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    updated = service.upsert_account(
        {
            "account_id": created["id"],
            "service": "ibk_business",
            "label": "중화점 IBK 빠른조회 수정",
            "username": "quick-user",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "collection_mode": "bank-quick-service",
            "sync_scope": "bank_deposit_match",
            "memo": "수정 저장 확인",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    rows = service._read("platform_accounts")
    assert len(rows) == 1
    assert updated["id"] == created["id"]
    assert updated["label"] == "중화점 IBK 빠른조회 수정"
    assert updated["sync_scope"] == "bank_deposit_match"
    assert rows[0]["password_enc"] == "encrypted:login-secret"
    assert rows[0]["account_no_enc"] == "encrypted:12345678901234"
    assert rows[0]["account_password_enc"] == "encrypted:4321"
    assert rows[0]["business_registration_no_enc"] == "encrypted:7108604499"


def test_sync_financial_transactions_reports_connector_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")
    service.upsert_account(
        {
            "service": "shinhan_business",
            "username": "bank-user",
            "api_key": "client-id",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "collection_mode": "api",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    result = service.sync_financial_transactions(
        {
            "services": ["shinhan_business"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["totals"]["transactions"] == 0
    assert result["summary"][0]["status"] == "connector_not_configured"


@pytest.mark.asyncio
async def test_save_integration_evidence_uploads_file_and_creates_pending_transaction(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    result = await service.save_integration_evidence(
        service="marketbom",
        business_id="biz-mia",
        branch="열정국밥_미아점",
        document_kind="supplier_statement",
        vendor="마켓봄",
        amount=123400,
        memo="7월 거래명세서",
        upload=UploadFile(filename="statement.pdf", file=BytesIO(b"pdf-data")),
        user={"email": "owner@example.com", "is_admin": True},
    )

    evidence = result["evidence"]
    transaction = result["transaction"]

    assert evidence["service"] == "marketbom"
    assert evidence["amount"] == 123400
    assert (service._evidence_upload_dir() / evidence["stored_filename"]).exists()
    assert transaction["evidence_id"] == evidence["id"]
    assert transaction["status"] == "pending"
    assert transaction["direction"] == "expense"


def test_list_accounts_migrates_legacy_plain_password(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_encrypt_secret", lambda value: f"encrypted:{value}")
    service._write(
        "platform_accounts",
        [
            {
                "id": "legacy",
                "service": "baemin",
                "username": "test-user",
                "password": "legacy-secret",
                "branch": "열정국밥_미아점",
            }
        ],
    )

    listed = service.list_accounts({"email": "owner@example.com", "is_admin": True})
    raw = service._read("platform_accounts")

    assert listed[0]["password_masked"] == "********"
    assert "password" not in listed[0]
    assert "password_enc" not in listed[0]
    assert raw[0]["password_enc"] == "encrypted:legacy-secret"
    assert "password" not in raw[0]


def test_upsert_account_rejects_cross_business_branch_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    try:
        service.upsert_account(
            {
                "service": "baemin",
                "username": "scope-test",
                "password": "secret",
                "business_id": "biz-junghwa",
                "branch": "열정국밥_미아점",
            },
            {"email": "owner@example.com", "is_admin": True},
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("cross-business account scope should fail")


def test_delivery_ledger_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))

    try:
        service.list_sales({"email": "staff@example.com", "is_admin": False})
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("employee must not read financial ledgers")


def test_sync_delivery_upserts_records_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "decrypted-secret")
    disable_delivery_browser_auth(monkeypatch)
    service._write(
        "platform_accounts",
        [
            {
                "id": "stale-duplicate",
                "service": "baemin",
                "username": "test-user",
                "password_enc": "stale-ciphertext",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "updated_at": "2026-07-20T13:30:00+09:00",
            },
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "test-user",
                "password_enc": "ciphertext",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "updated_at": "2026-07-20T13:00:00+09:00",
            }
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fake_collect(account, password, date_from, date_to):
        assert password == "decrypted-secret"
        assert account["id"] == "acct-baemin"
        assert account["business_id"] == "biz-mia"
        return {
            "status": "succeeded",
            "error_code": "",
            "records": {
                "sales": [
                    {
                        "id": "sale-1",
                        "business_id": "biz-mia",
                        "branch": "열정국밥_미아점",
                        "service": "baemin",
                        "occurred_on": "2026-07-01",
                        "gross_amount": 12000,
                    }
                ],
                "settlements": [
                    {
                        "id": "settlement-1",
                        "business_id": "biz-mia",
                        "branch": "열정국밥_미아점",
                        "service": "baemin",
                        "record_type": "settlements",
                        "occurred_on": "2026-07-02",
                        "settlement_amount": 11000,
                    }
                ],
                "reviews": [
                    {
                        "id": "review-1",
                        "business_id": "biz-mia",
                        "branch": "열정국밥_미아점",
                        "service": "baemin",
                        "record_type": "reviews",
                        "occurred_on": "2026-07-03",
                        "rating": 5,
                        "review_text": "맛있어요",
                    }
                ],
            },
            "diagnostics": {"sales": "fixture"},
        }

    monkeypatch.setattr(collectors, "collect_account", fake_collect)
    user = {"email": "owner@example.com", "is_admin": True}
    payload = {
        "services": ["baemin"],
        "business_id": "biz-mia",
        "branch": "열정국밥_미아점",
        "date_from": "2026-07-01",
        "date_to": "2026-07-20",
    }

    first = service.sync_delivery(payload, user)
    second = service.sync_delivery(payload, user)

    assert first["totals"]["sales"] == 1
    assert first["totals"]["settlements"] == 1
    assert first["totals"]["reviews"] == 1
    assert len(first["records"]) == 2
    assert len(first["sales"]) == 1
    assert len(first["settlements"]) == 1
    assert len(first["reviews"]) == 1
    assert first["summary"][0]["portal_status"] == "succeeded"
    assert second["totals"]["sales"] == 1
    assert len(service.list_sales(user, "biz-mia")) == 1
    assert len(service.list_settlements(user, "biz-mia")) == 1
    assert len(service.list_reviews(user, "biz-mia")) == 1
    statuses = service.list_collection_status(user, "biz-mia")
    assert len(statuses) == 2
    assert all(row["status"] == "succeeded" for row in statuses)


def test_sync_delivery_prefers_saved_browser_credentials_over_canonical_upload_account(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    disable_delivery_browser_auth(monkeypatch)
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "decrypted-secret")
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "collection_mode": "portal-csv",
                "last_sync_status": "upload_required",
                "updated_at": "2026-08-06T18:18:06+09:00",
            },
            {
                "id": "saved-baemin",
                "service": "baemin",
                "username": "saved-owner",
                "password_enc": "ciphertext",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "collection_mode": "browser-automation",
                "updated_at": "2026-08-06T18:10:00+09:00",
            },
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fake_collect(account, password, date_from, date_to):
        assert account["id"] == "saved-baemin"
        assert password == "decrypted-secret"
        return {
            "status": "succeeded",
            "error_code": "",
            "records": {"sales": [], "settlements": [], "reviews": []},
        }

    monkeypatch.setattr(collectors, "collect_account", fake_collect)

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "allow_server_headless_fallback": True,
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["account_id"] == "saved-baemin"
    assert result["summary"][0]["status"] == "succeeded"


def test_sync_delivery_portal_csv_account_requests_upload_without_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    disable_delivery_browser_auth(monkeypatch)
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "password_enc": "ciphertext",
                "collection_mode": "portal-csv",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
            }
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fail_collect(*args, **kwargs):
        raise AssertionError("portal-csv accounts must not start browser collection")

    monkeypatch.setattr(collectors, "collect_account", fail_collect)
    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-07-01",
            "date_to": "2026-07-20",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "action_required"
    assert result["summary"][0]["error_code"] == "CSV_UPLOAD_REQUIRED"
    assert result["totals"] == {"sales": 0, "settlements": 0, "reviews": 0}
    assert service.list_collection_status({"email": "owner@example.com", "is_admin": True}, "biz-mia")[0]["message"]


def test_sync_delivery_portal_csv_account_uses_storage_state_when_supplied(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    storage_state = tmp_path / "baemin-storage-state.json"
    storage_state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "collection_mode": "portal-csv",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
            }
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fake_collect(account, password, date_from, date_to):
        assert password == ""
        assert account["storage_state_path"] == str(storage_state)
        return {
            "status": "partial",
            "error_code": "AUTHENTICATED_NO_ROWS",
            "records": {},
            "diagnostics": {"auth_mode": "storage_state"},
        }

    monkeypatch.setattr(collectors, "collect_account", fake_collect)
    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-07-01",
            "date_to": "2026-07-20",
            "storage_state_path": str(storage_state),
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "partial"
    assert result["summary"][0]["error_code"] == "AUTHENTICATED_NO_ROWS"
    assert service.list_collection_status({"email": "owner@example.com", "is_admin": True}, "biz-mia")[0]["diagnostics"][
        "auth_mode"
    ] == "storage_state"


def test_sync_delivery_passes_baemin_storage_state_to_collector(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    storage_state = tmp_path / "baemin-storage-state.json"
    storage_state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin-junghwa",
                "service": "baemin",
                "username": "owner",
                "password_enc": "ciphertext",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "secret")

    from app.services import yeoljeong_delivery_collectors as collectors

    def fake_collect(account, password, date_from, date_to):
        assert password == "secret"
        assert account["storage_state_path"] == str(storage_state)
        return {
            "status": "partial",
            "error_code": "AUTHENTICATED_NO_ROWS",
            "records": {},
            "diagnostics": {"auth_mode": "storage_state"},
        }

    monkeypatch.setattr(collectors, "collect_account", fake_collect)

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
            "storage_state_path": str(storage_state),
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "partial"
    assert service.list_collection_status({"email": "owner@example.com", "is_admin": True}, "biz-junghwa")[0]["diagnostics"][
        "auth_mode"
    ] == "storage_state"


def test_sync_delivery_uses_baemin_storage_state_without_password(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    storage_state = tmp_path / "baemin-storage-state.json"
    storage_state.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "collection_mode": "browser-automation",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
            }
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fake_collect(account, password, date_from, date_to):
        assert password == ""
        assert account["storage_state_path"] == str(storage_state)
        return {
            "status": "partial",
            "error_code": "AUTHENTICATED_NO_ROWS",
            "records": {},
            "diagnostics": {"auth_mode": "storage_state"},
        }

    monkeypatch.setattr(collectors, "collect_account", fake_collect)

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
            "storage_state_path": str(storage_state),
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "partial"
    assert result["summary"][0]["error_code"] == "AUTHENTICATED_NO_ROWS"


def test_sync_delivery_uses_baemin_pc_agent_session_without_password(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin",
                "service": "baemin",
                "username": "owner",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )

    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {
            "storage_state_path": "",
            "browser_session_id": str(payload.get("browser_session_id") or ""),
            "browser_bridge_mode": "local_agent",
        },
    )

    def fake_bridge_collect(account, browser_auth):
        assert account["service"] == "baemin"
        assert account.get("password") in (None, "")
        assert browser_auth["browser_session_id"] == "bb-pc-agent"
        return {
            "status": "succeeded",
            "error_code": "",
            "records": {
                "sales": [
                    {
                        "id": "baemin-pc-sale-1",
                        "source_id": "sale-1",
                        "business_id": "biz-junghwa",
                        "branch": "중화점",
                        "service": "baemin",
                        "platform": "baemin",
                        "record_type": "sales",
                        "occurred_on": "2026-08-04",
                        "gross_amount": 31000,
                    }
                ],
                "settlements": [],
                "reviews": [],
            },
            "diagnostics": {"auth_mode": "pc_agent_browser"},
        }

    monkeypatch.setattr(service, "_collect_baemin_from_browser_bridge_session", fake_bridge_collect)

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
            "browser_session_id": "bb-pc-agent",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "succeeded"
    assert result["summary"][0]["counts"]["sales"] == 1
    assert service._read("delivery_collection_status")[0]["diagnostics"]["auth_mode"] == "pc_agent_browser"


def test_sync_delivery_uses_pc_agent_session_for_all_delivery_services(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    services = ["coupangeats", "yogiyo", "ddangyo"]
    service._write(
        "platform_accounts",
        [
            {
                "id": f"acct-{name}-junghwa",
                "service": name,
                "username": "owner",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
            for name in services
        ],
    )
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {
            "storage_state_path": "",
            "browser_session_id": "bb-pc-agent",
            "browser_bridge_mode": "local_agent",
        },
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fail_headless_collect(*args, **kwargs):
        raise AssertionError("PC Agent session must be attempted before server headless collection")

    def fake_bridge_collect(account, browser_auth, date_from, date_to):
        assert account["service"] in services
        assert browser_auth["browser_session_id"] == "bb-pc-agent"
        return {
            "status": "succeeded",
            "error_code": "",
            "records": {
                "sales": [
                    {
                        "id": f"{account['service']}-pc-sale-1",
                        "source_id": "sale-1",
                        "business_id": "biz-junghwa",
                        "branch": "중화점",
                        "service": account["service"],
                        "platform": account["service"],
                        "record_type": "sales",
                        "occurred_on": "2026-08-04",
                        "gross_amount": 31000,
                    }
                ],
                "settlements": [],
                "reviews": [],
            },
            "diagnostics": {"auth_mode": "pc_agent_browser"},
        }

    monkeypatch.setattr(collectors, "collect_account", fail_headless_collect)
    monkeypatch.setattr(service, "_collect_delivery_from_browser_bridge_session", fake_bridge_collect)

    result = service.sync_delivery(
        {
            "services": services,
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert [item["status"] for item in result["summary"]] == ["succeeded", "succeeded", "succeeded"]
    assert result["totals"]["sales"] == 3
    assert {row["diagnostics"]["auth_mode"] for row in service._read("delivery_collection_status")} == {
        "pc_agent_browser"
    }


def test_sync_delivery_all_scope_collects_registered_accounts_across_branches(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "plain-secret")
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin-junghwa",
                "service": "baemin",
                "username": "owner-j",
                "password_enc": "ciphertext",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            },
            {
                "id": "acct-baemin-mia",
                "service": "baemin",
                "username": "owner-m",
                "password_enc": "ciphertext",
                "collection_mode": "browser-automation",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
            },
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    def fake_collect(account, password, date_from, date_to):
        return {
            "status": "succeeded",
            "error_code": "",
            "records": {
                "sales": [
                    {
                        "id": f"{account['business_id']}-{account['service']}-sale",
                        "source_id": "sale-1",
                        "business_id": account["business_id"],
                        "branch": account["branch"],
                        "service": account["service"],
                        "platform": account["service"],
                        "record_type": "sales",
                        "occurred_on": "2026-08-04",
                        "gross_amount": 31000,
                    }
                ],
                "settlements": [],
                "reviews": [],
            },
        }

    monkeypatch.setattr(collectors, "collect_account", fake_collect)

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "all",
            "branch": "전체",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
            "allow_server_headless_fallback": True,
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["business_id"] == "all"
    assert result["branch"] == "전체"
    assert result["totals"]["sales"] == 2
    assert {(item["business_id"], item["branch"], item["status"]) for item in result["summary"]} == {
        ("biz-junghwa", "중화점", "succeeded"),
        ("biz-mia", "열정국밥_미아점", "succeeded"),
    }
    assert len(service.list_sales({"email": "owner@example.com", "is_admin": True})) == 2


def test_delivery_browser_auth_for_account_ensures_service_work_session(monkeypatch):
    class FakeSession:
        session_id = "bb-auto-coupang"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            return FakeSession()

    fake_bridge = FakeBridge()
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)

    auth = service._delivery_browser_auth_for_account(
        {"prefer_pc_agent": True},
        {"service": "coupangeats", "business_id": "biz-junghwa", "branch": "중화점"},
        "coupangeats",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-auto-coupang"
    assert auth["browser_bridge_mode"] == "local_agent"
    assert auth["browser_work_key"].startswith("yeoljeong-delivery-coupangeats-biz-junghwa-")
    assert "중화점" not in auth["browser_work_key"]
    assert auth["browser_target_url"].startswith("https://")
    assert fake_bridge.calls[0]["url"] == "about:blank"


def test_delivery_browser_auth_for_account_passes_configured_pc_agent(monkeypatch):
    class FakeSession:
        session_id = "bb-pinned-coupang"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            return FakeSession()

    fake_bridge = FakeBridge()
    import app.browser_bridge.service as bridge_service

    monkeypatch.setenv("YEOLJEONG_DELIVERY_PC_AGENT_ID", "agent-good")
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)

    auth = service._delivery_browser_auth_for_account(
        {"prefer_pc_agent": True},
        {"service": "coupangeats", "business_id": "biz-junghwa", "branch": "중화점"},
        "coupangeats",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-pinned-coupang"
    assert auth["browser_agent_id"] == "agent-good"
    assert fake_bridge.calls[0]["agent_id"] == "agent-good"


def test_delivery_browser_auth_for_account_recreates_stale_work_session(monkeypatch):
    class FakeSession:
        session_id = "bb-recreated-coupang"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            if not kwargs.get("force_recreate"):
                raise RuntimeError("CDP endpoint 준비 실패")
            return FakeSession()

    fake_bridge = FakeBridge()
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {
            "storage_state_path": "",
            "browser_session_id": "bb-active-session",
            "browser_bridge_mode": "local_agent",
            "browser_session_id_explicit": "",
        },
    )
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)

    auth = service._delivery_browser_auth_for_account(
        {"prefer_pc_agent": True},
        {"service": "coupangeats", "business_id": "biz-junghwa", "branch": "중화점"},
        "coupangeats",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-recreated-coupang"
    assert auth["browser_bridge_mode"] == "local_agent"
    assert auth["browser_bridge_recovered"] == "force_recreate_attempt_2"
    assert fake_bridge.calls[0]["force_recreate"] is False
    assert fake_bridge.calls[1]["force_recreate"] is True
    assert fake_bridge.calls[0]["url"] == "about:blank"
    assert fake_bridge.calls[1]["url"] == "about:blank"


def test_delivery_browser_auth_for_account_force_recreates_portal_work_session(monkeypatch):
    class FakeSession:
        session_id = "bb-refixed-ddangyo"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            return FakeSession()

    fake_bridge = FakeBridge()
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)

    auth = service._delivery_browser_auth_for_account(
        {"prefer_pc_agent": True, "force_recreate_portal_sessions": True},
        {
            "service": "ddangyo",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "portal_home_url": "https://boss.ddangyo.com/",
        },
        "ddangyo",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-refixed-ddangyo"
    assert auth["browser_bridge_mode"] == "local_agent"
    assert auth["browser_bridge_recovered"] == "force_recreate_requested"
    assert auth["browser_session_recreated"] == "1"
    assert auth["browser_work_key"].startswith("yeoljeong-delivery-ddangyo-biz-junghwa-")
    assert fake_bridge.calls == [
        {
            "work_key": auth["browser_work_key"],
            "label": "열정국밥 중화점 땡겨요 자동수집",
            "url": "https://boss.ddangyo.com/",
            "force_recreate": True,
        }
    ]


def test_delivery_browser_auth_for_account_retries_multiple_stale_cdp_sessions(monkeypatch):
    class FakeSession:
        session_id = "bb-recovered-third"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) < 3:
                raise RuntimeError(f"CDP endpoint 준비 실패 {len(self.calls)}")
            return FakeSession()

    fake_bridge = FakeBridge()
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)

    auth = service._delivery_browser_auth_for_account(
        {"prefer_pc_agent": True},
        {"service": "yogiyo", "business_id": "biz-junghwa", "branch": "중화점"},
        "yogiyo",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-recovered-third"
    assert auth["browser_bridge_mode"] == "local_agent"
    assert auth["browser_bridge_recovered"] == "force_recreate_attempt_3"
    assert "CDP endpoint 준비 실패 1" in auth["browser_bridge_errors"]
    assert "CDP endpoint 준비 실패 2" in auth["browser_bridge_errors"]
    assert [call["force_recreate"] for call in fake_bridge.calls] == [False, True, True]


def test_sync_delivery_no_password_does_not_create_pc_agent_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-coupangeats-junghwa",
                "service": "coupangeats",
                "username": "owner",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )

    class FakeBridge:
        async def ensure_work_session(self, **kwargs):
            raise AssertionError("PC Agent should not be opened for missing-password accounts by default")

    import app.browser_bridge.service as bridge_service
    from app.services import yeoljeong_delivery_collectors as collectors

    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: FakeBridge())
    monkeypatch.setattr(
        collectors,
        "collect_account",
        lambda *args, **kwargs: pytest.fail("headless collection requires a saved password"),
    )

    result = service.sync_delivery(
        {
            "services": ["coupangeats"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "action_required"
    assert result["summary"][0]["error_code"] == "MISSING_CREDENTIALS"
    assert "비밀번호" in result["summary"][0]["message"]
    assert service._read("delivery_collection_status")[0]["error_code"] == "MISSING_CREDENTIALS"


def test_sync_delivery_browser_automation_password_requires_pc_agent_session(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "plain-secret")
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-baemin-junghwa",
                "service": "baemin",
                "username": "owner",
                "password_enc": "ciphertext",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(service, "_delivery_browser_auth_for_account", lambda *args, **kwargs: {
        "storage_state_path": "",
        "browser_session_id": "",
        "browser_bridge_mode": "",
            "browser_bridge_error": "pc unavailable",
        })

    from app.services import yeoljeong_delivery_collectors as collectors

    monkeypatch.setattr(collectors, "collect_account", lambda *args, **kwargs: pytest.fail("browser-automation must use PC Agent"))

    result = service.sync_delivery(
        {
            "services": ["baemin"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "action_required"
    assert result["summary"][0]["error_code"] == "PC_AGENT_SESSION_REQUIRED"
    status = service._read("delivery_collection_status")[0]
    assert status["error_code"] == "PC_AGENT_SESSION_REQUIRED"
    assert status["diagnostics"]["browser_bridge_error"] == "pc unavailable"


def test_delivery_browser_auth_options_uses_active_bridge_session(monkeypatch):
    def fake_build_e2e_config(session_id=None):
        assert session_id is None
        return {
            "mode": "local_agent",
            "session_id": "bb-active-pc-agent",
            "headless_fallback": False,
        }

    import app.browser_bridge.e2e_adapter as e2e_adapter

    monkeypatch.setattr(e2e_adapter, "build_e2e_config", fake_build_e2e_config)

    auth = service._delivery_browser_auth_options({})

    assert auth["browser_bridge_mode"] == "local_agent"
    assert auth["browser_session_id"] == "bb-active-pc-agent"
    assert auth["storage_state_path"] == ""


def test_delivery_browser_auth_for_account_creates_service_session_instead_of_reusing_active(monkeypatch):
    def fake_build_e2e_config(session_id=None):
        assert session_id is None
        return {
            "mode": "local_agent",
            "session_id": "bb-active-ddangyo",
            "headless_fallback": False,
        }

    class FakeSession:
        session_id = "bb-work-baemin"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            return FakeSession()

    fake_bridge = FakeBridge()

    import app.browser_bridge.e2e_adapter as e2e_adapter
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(e2e_adapter, "build_e2e_config", fake_build_e2e_config)
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)

    auth = service._delivery_browser_auth_for_account(
        {"prefer_pc_agent": True},
        {"service": "baemin", "business_id": "biz-junghwa", "branch": "중화점"},
        "baemin",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-work-baemin"
    assert auth["ambient_browser_session_id"] == "bb-active-ddangyo"
    assert auth["browser_work_key"].startswith("yeoljeong-delivery-baemin-biz-junghwa-")
    assert "중화점" not in auth["browser_work_key"]
    assert auth["browser_target_url"].startswith("https://")
    assert fake_bridge.calls[0]["url"] == "about:blank"


def test_delivery_browser_auth_for_account_prefers_saved_password_headless(monkeypatch):
    def fake_build_e2e_config(session_id=None):
        assert session_id is None
        return {
            "mode": "local_agent",
            "session_id": "bb-active-pc-agent",
            "headless_fallback": False,
        }

    class FakeBridge:
        async def ensure_work_session(self, **kwargs):
            raise AssertionError("saved-password account should not create a PC Agent session first")

    import app.browser_bridge.e2e_adapter as e2e_adapter
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(e2e_adapter, "build_e2e_config", fake_build_e2e_config)
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: FakeBridge())
    monkeypatch.setattr(service, "_has_secret_value", lambda account, key: key == "password")

    auth = service._delivery_browser_auth_for_account(
        {},
        {
            "service": "coupangeats",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "password_enc": "ciphertext",
        },
        "coupangeats",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == ""
    assert auth["browser_bridge_mode"] == ""
    assert auth["browser_auth_strategy"] == "server_headless_password_first"


def test_delivery_browser_auth_for_browser_automation_account_prefers_pc_agent(monkeypatch):
    def fake_build_e2e_config(session_id=None):
        assert session_id is None
        return {
            "mode": "local_agent",
            "session_id": "",
            "headless_fallback": False,
        }

    class FakeSession:
        session_id = "bb-work-coupangeats"

    class FakeBridge:
        def __init__(self):
            self.calls = []

        async def ensure_work_session(self, **kwargs):
            self.calls.append(kwargs)
            return FakeSession()

    fake_bridge = FakeBridge()

    import app.browser_bridge.e2e_adapter as e2e_adapter
    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(e2e_adapter, "build_e2e_config", fake_build_e2e_config)
    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: fake_bridge)
    monkeypatch.setattr(service, "_has_secret_value", lambda account, key: key == "password")

    auth = service._delivery_browser_auth_for_account(
        {},
        {
            "service": "coupangeats",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "password_enc": "ciphertext",
            "collection_mode": "browser-automation",
        },
        "coupangeats",
        "biz-junghwa",
        "중화점",
    )

    assert auth["browser_session_id"] == "bb-work-coupangeats"
    assert auth["browser_bridge_mode"] == "local_agent"
    assert auth["browser_work_key"].startswith("yeoljeong-delivery-coupangeats-biz-junghwa-")
    assert fake_bridge.calls[0]["url"] == "about:blank"


def test_sync_delivery_blocks_concurrent_runs_without_touching_collectors(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-coupangeats-junghwa",
                "service": "coupangeats",
                "username": "owner",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )

    from app.services import yeoljeong_delivery_collectors as collectors

    monkeypatch.setattr(
        collectors,
        "collect_account",
        lambda *args, **kwargs: pytest.fail("concurrent sync must stop before collector execution"),
    )
    lock_fd = service._try_acquire_delivery_sync_lock()
    assert lock_fd is not None
    try:
        result = service.sync_delivery(
            {
                "services": ["coupangeats"],
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "date_from": "2026-08-01",
                "date_to": "2026-08-04",
            },
            {"email": "owner@example.com", "is_admin": True},
        )
    finally:
        service._release_delivery_sync_lock(lock_fd)

    assert result["summary"][0]["status"] == "action_required"
    assert result["summary"][0]["error_code"] == "COLLECTION_ALREADY_RUNNING"
    assert service._read("delivery_collection_status")[0]["error_code"] == "COLLECTION_ALREADY_RUNNING"


def test_sync_delivery_marks_pc_agent_section_not_found_as_action_required(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-coupangeats-junghwa",
                "service": "coupangeats",
                "username": "owner",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "bb-pc-agent", "browser_bridge_mode": "local_agent"},
    )

    def fake_bridge_collect(account, browser_auth, date_from, date_to):
        return {
            "status": "partial",
            "error_code": "AUTHENTICATED_NO_ROWS",
            "records": {"sales": [], "settlements": [], "reviews": []},
            "diagnostics": {
                "auth_mode": "pc_agent_browser",
                "browser_session_id": browser_auth["browser_session_id"],
                "url": "https://store.coupangeats.com/merchant/",
                "sales": "section_not_found",
                "settlements": "section_not_found",
                "reviews": "section_not_found",
            },
        }

    monkeypatch.setattr(service, "_collect_delivery_from_browser_bridge_session", fake_bridge_collect)

    result = service.sync_delivery(
        {
            "services": ["coupangeats"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-04",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "action_required"
    assert result["summary"][0]["error_code"] == "PORTAL_TABLE_NOT_FOUND"
    assert service._read("delivery_collection_status")[0]["status"] == "action_required"


def test_baemin_dashboard_records_extracts_home_summary(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime

            return datetime(2026, 8, 19, 12, 0, tzinfo=tz)

    monkeypatch.setattr(service, "datetime", FixedDateTime)
    text = """
    입금 예정 금액
    6,779,643원
    어제 주문금액
    1,719,000원
    어제 주문수
    78건
    오늘
    첫주문인데 음식은 대체적으로 맛있게먹었습니다
    열정국밥 중랑구중화점
    """

    records = service._baemin_dashboard_records(text, "biz-junghwa", "중화점")

    assert records["sales"][0]["gross_amount"] == 1719000
    assert records["sales"][0]["order_count"] == 78
    assert records["settlements"][0]["settlement_amount"] == 6779643
    assert records["reviews"][0]["review_text"].startswith("첫주문인데")


@pytest.mark.asyncio
async def test_delivery_bridge_login_uses_dom_fallback_for_portal_spa(monkeypatch):
    monkeypatch.setattr(service, "_has_secret_value", lambda account, key: key == "password")
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "saved-password")

    class EmptyLocator:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

    class FakePage:
        def __init__(self):
            self.evaluate_arg = None
            self.timeout_ms = 0

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_selector(self, *args, **kwargs):
            return None

        def locator(self, selector):
            return EmptyLocator()

        async def evaluate(self, expression, arg=None):
            self.evaluate_arg = arg
            return {"filled": True, "clicked": True, "reason": ""}

        async def wait_for_timeout(self, ms):
            self.timeout_ms = ms

    page = FakePage()

    result = await service._delivery_bridge_login_with_saved_secret(
        page,
        {"service": "ddangyo", "username": "owner", "password_enc": "encrypted"},
        "땡겨요 사장님",
    )

    assert result is None
    assert page.evaluate_arg["username"] == "owner"
    assert page.evaluate_arg["password"] == "saved-password"
    assert "#mf_btn_webLogin" in page.evaluate_arg["submitSelectors"]
    assert page.timeout_ms == 5000


@pytest.mark.asyncio
async def test_ddangyo_pc_agent_login_stops_at_numeric_captcha_with_screenshot(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)

    class FakePage:
        def __init__(self):
            self.url = "https://boss.ddangyo.com/"
            self.text = "땡겨요 사장님 로그인 아이디 비밀번호"

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_timeout(self, *args, **kwargs):
            return None

        async def evaluate(self, expression, arg=None):
            if "window.location.href" in expression:
                return self.url
            if "innerHTML" in expression:
                return f"<main>{self.text}</main>"
            if "innerText" in expression:
                return self.text
            return ""

        async def screenshot(self, **kwargs):
            return b"fake-png"

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]

    class FakeBridge:
        def __init__(self, page):
            self.sessions = {"bb-ddangyo": object()}
            self.page = page

        async def _context_for_session(self, session):
            return FakeContext(self.page)

    page = FakePage()

    async def fake_login(page_arg, account, service_label):
        assert page_arg is page
        assert account["service"] == "ddangyo"
        assert service_label == "땡겨요"
        page.text = "자동입력방지 숫자를 입력해 주세요"
        return None

    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: FakeBridge(page))
    monkeypatch.setattr(service, "_delivery_bridge_login_with_saved_secret", fake_login)

    result = await service._collect_delivery_from_browser_bridge_session_async(
        {
            "service": "ddangyo",
            "username": "owner",
            "password_enc": "encrypted",
            "business_id": "biz-junghwa",
            "branch": "중화점",
        },
        {"browser_session_id": "bb-ddangyo", "browser_bridge_mode": "local_agent"},
        "2026-08-01",
        "2026-08-04",
    )

    assert result["status"] == "portal_action_required"
    assert result["error_code"] == "DDANGYO_NUMERIC_CAPTCHA_REQUIRED"
    assert "숫자 캡챠" in result["message"]
    screenshot_path = result["diagnostics"]["challenge_screenshot_path"]
    assert screenshot_path.startswith(str(tmp_path))
    assert service.Path(screenshot_path).read_bytes() == b"fake-png"


def test_sync_delivery_passes_ddangyo_captcha_value_to_pc_agent_collector(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "plain-secret")
    service._write(
        "platform_accounts",
        [
            {
                "id": "acct-ddangyo-junghwa",
                "service": "ddangyo",
                "username": "owner",
                "password_enc": "ciphertext",
                "collection_mode": "browser-automation",
                "business_id": "biz-junghwa",
                "branch": "중화점",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_options",
        lambda payload: {"storage_state_path": "", "browser_session_id": "", "browser_bridge_mode": ""},
    )
    monkeypatch.setattr(
        service,
        "_delivery_browser_auth_for_account",
        lambda payload, account, service_name, business_id, branch: {
            "storage_state_path": "",
            "browser_session_id": "bb-ddangyo",
            "browser_bridge_mode": "local_agent",
        },
    )

    def fake_bridge_collect(account, browser_auth, date_from, date_to):
        assert account["captcha_value"] == "1234"
        assert browser_auth["browser_session_id"] == "bb-ddangyo"
        return {
            "status": "succeeded",
            "error_code": "",
            "records": {"sales": [], "settlements": [], "reviews": []},
            "diagnostics": {"captcha_input": "accepted"},
        }

    monkeypatch.setattr(service, "_collect_delivery_from_browser_bridge_session", fake_bridge_collect)

    result = service.sync_delivery(
        {
            "services": ["ddangyo"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "captcha_value": "12 34",
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert result["summary"][0]["status"] == "succeeded"
    assert service._read("delivery_collection_status")[0]["diagnostics"]["captcha_input"] == "accepted"


@pytest.mark.asyncio
async def test_ddangyo_pc_agent_enters_confirmed_numeric_captcha(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        @property
        def first(self):
            return self

        async def count(self):
            if self.selector == "#mf_wfm_login_captcha":
                return 1
            return 0

        async def is_visible(self, timeout=0):
            return self.selector == "#mf_wfm_login_captcha"

        async def fill(self, value):
            self.page.captcha_filled = value

        async def press(self, key):
            self.page.login_state = "authenticated"
            self.page.text = "정산내역 주문내역 리뷰관리"

    class FakePage:
        def __init__(self):
            self.url = "https://boss.ddangyo.com/"
            self.text = "자동입력방지 숫자를 입력해 주세요"
            self.captcha_filled = ""
            self.login_state = "challenge"

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_timeout(self, *args, **kwargs):
            return None

        def locator(self, selector):
            return FakeLocator(self, selector)

        async def evaluate(self, expression, arg=None):
            if "window.location.href" in expression:
                return self.url
            if "innerHTML" in expression:
                return f"<main>{self.text}</main>"
            if "innerText" in expression:
                return self.text
            return False

        async def screenshot(self, **kwargs):
            return b"fake-png"

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]

    class FakeBridge:
        def __init__(self, page):
            self.sessions = {"bb-ddangyo": object()}
            self.page = page

        async def _context_for_session(self, session):
            return FakeContext(self.page)

    page = FakePage()

    import app.browser_bridge.service as bridge_service

    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: FakeBridge(page))

    result = await service._collect_delivery_from_browser_bridge_session_async(
        {
            "service": "ddangyo",
            "username": "owner",
            "password_enc": "encrypted",
            "captcha_value": "9876",
            "business_id": "biz-junghwa",
            "branch": "중화점",
        },
        {"browser_session_id": "bb-ddangyo", "browser_bridge_mode": "local_agent"},
        "2026-08-01",
        "2026-08-04",
    )

    assert page.captcha_filled == "9876"
    assert result["status"] == "partial"
    assert result["diagnostics"]["captcha_input"] == "accepted"
    assert result["error_code"] == "AUTHENTICATED_NO_ROWS"


def test_import_settlement_csv_is_scoped_and_idempotent():
    user = {"email": "owner@example.com", "is_admin": True}
    csv_text = (
        "정산번호,정산일,매출액,수수료,부가세,정산금액,상태\n"
        "SET-1,2026-07-10,15000,1000,100,13900,지급완료\n"
    )
    kwargs = {
        "service": "baemin",
        "business_id": "biz-mia",
        "branch": "미아점",
        "filename": "settlement.csv",
    }

    first = service.import_settlement_csv(csv_text, user, **kwargs)
    second = service.import_settlement_csv(csv_text, user, **kwargs)

    assert first["imported"] == 1
    assert first["settlements"][0]["business_id"] == "biz-mia"
    assert first["settlements"][0]["branch"] == "열정국밥_미아점"
    assert first["settlements"][0]["settlement_amount"] == 13900
    assert second["imported"] == 0
    assert second["duplicate_rows"] == 1
    assert len(service.list_settlements(user, "biz-mia")) == 1


def test_save_employment_contract_adds_a4_standard_template_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    seed_approved_employee(name="E2E 테스트직원", email="e2e.employee@example.com")

    saved = service.save_contract(
        valid_employment_contract(),
        {"email": "owner@example.com", "is_admin": True},
    )

    assert saved["document_kind"] == "standard_employment_contract"
    assert saved["template_version"] == "majangbiseo-employment-2026-07-identity-table-v3"
    assert saved["print_title"] == "표준근로계약서"


def test_save_freelancer_contract_adds_a4_service_template_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))
    seed_approved_employee(name="E2E 프리랜서", email="e2e.freelancer@example.com")

    saved = service.save_contract(
        {
            "employee_request_id": "join-mia",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "contract_type": "freelancer",
            "employment_tax_type": "freelancer_33",
            "contract_date": "2026-07-15",
            "start_date": "2026-07-16",
            "wage_type": "case_fee",
            "freelancer_scope": "홍보 콘텐츠 제작 및 배달앱 리뷰 응대 지원",
            "freelancer_settlement_terms": "검수 후 매월 10일 3.3% 공제 지급",
            "wage": 500000,
        },
        {"email": "owner@example.com", "is_admin": True},
    )

    assert saved["document_kind"] == "freelancer_service_contract"
    assert saved["template_version"] == "majangbiseo-freelancer-2026-07-identity-table-v3"
    assert saved["print_title"] == "3.3% 프리랜서 용역계약서"


def test_approved_employees_are_filtered_by_business_and_infer_legacy_scope():
    service._write(
        "employee_join_requests",
        [
            {"id": "join-mia", "name": "미아 직원", "email": "mia@example.com", "branch": "미아점", "status": "approved"},
            {"id": "join-junghwa", "name": "중화 직원", "email": "junghwa@example.com", "branch": "중화점", "status": "approved"},
        ],
    )

    rows = service.list_approved_employees({"email": "owner@example.com", "is_admin": True}, "biz-mia")

    assert [row["id"] for row in rows] == ["join-mia"]
    assert rows[0]["business_id"] == "biz-mia"
    assert rows[0]["branch"] == "열정국밥_미아점"


def test_contract_selected_employee_autofills_reference_data_but_keeps_edits():
    service._write(
        "employee_join_requests",
        [
            {
                "id": "join-mia",
                "name": "가입 직원",
                "email": "member@example.com",
                "address": "서울시 직원 주소",
                "phone": "010-9876-5432",
                "birth_date": "1991-02-03",
                "nationality": "대한민국",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "status": "approved",
            }
        ],
    )
    user = {"email": "owner@example.com", "is_admin": True}

    saved = service.save_contract(
        valid_employment_contract(branch="미아점", workplace=""),
        user,
    )

    assert saved["employee_name"] == "가입 직원"
    assert saved["employee_email"] == "member@example.com"
    assert saved["employee_address"] == "서울시 직원 주소"
    assert saved["employee_phone"] == "010-9876-5432"
    assert saved["employee_birth_date"] == "1991-02-03"
    assert saved["employer_name"] == "열정국밥_미아점"
    assert saved["employer_registration_no"] == "874-21-02160"
    assert saved["employer_representative"] == "최미미"
    assert saved["workplace"] == "열정국밥_미아점"

    edited = service.save_contract(
        {
            **saved,
            "employee_name": "직원 수정명",
            "employer_name": "사용자 수정 상호",
            "workplace": "수정 근무장소",
        },
        user,
    )

    assert edited["employee_name"] == "직원 수정명"
    assert edited["employer_name"] == "사용자 수정 상호"
    assert edited["workplace"] == "수정 근무장소"


def test_contract_autofills_privacy_minimised_onboarding_document_profile():
    service._write(
        "employee_join_requests",
        [{
            "id": "join-mia",
            "name": "가입 직원",
            "email": "member@example.com",
            "phone": "010-1234-5678",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "status": "approved",
        }],
    )
    service._write(
        "onboarding_documents",
        [
            {
                "id": "doc-resident",
                "employee_request_id": "join-mia",
                "employee_email": "member@example.com",
                "employee_name": "가입 직원",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "document_type": "resident_register",
                "document_label": "주민등록등본",
                "status": "approved",
                "issue_date": "2026-07-03",
                "extracted_fields": {
                    "address": "첨부 확인 주소",
                    "birth_date": "1997-07-15",
                    "nationality": "대한민국",
                },
            },
            {
                "id": "doc-bankbook",
                "employee_request_id": "join-mia",
                "employee_email": "member@example.com",
                "employee_name": "가입 직원",
                "business_id": "biz-mia",
                "branch": "열정국밥_미아점",
                "document_type": "bankbook",
                "document_label": "통장사본",
                "status": "approved",
                "issue_date": "2026-07-05",
                "extracted_fields": {
                    "bank_name": "우리은행",
                    "bank_account_holder": "가입 직원",
                    "bank_account_masked": "1002-***-**3886",
                },
            },
        ],
    )
    employee_rows = service.list_approved_employees(
        {"email": "owner@example.com", "is_admin": True},
        "biz-mia",
    )
    assert employee_rows[0]["bank_name"] == "우리은행"
    assert employee_rows[0]["bank_account_masked"] == "1002-***-**3886"
    assert "주민등록등본(2026-07-03, approved)" in employee_rows[0]["onboarding_document_summary"]
    assert "통장사본(2026-07-05, approved)" in employee_rows[0]["onboarding_document_summary"]

    saved = service.save_contract(
        valid_employment_contract(
            employee_address="",
            employee_birth_date="",
            employee_nationality="",
        ),
        {"email": "owner@example.com", "is_admin": True},
    )
    assert saved["employee_address"] == "첨부 확인 주소"
    assert saved["employee_birth_date"] == "1997-07-15"
    assert saved["employee_nationality"] == "대한민국"
    assert saved["bank_name"] == "우리은행"
    assert saved["bank_account_holder"] == "가입 직원"
    assert saved["bank_account_masked"] == "1002-***-**3886"
    assert "통장사본" in saved["onboarding_document_summary"]


def test_contract_rejects_employment_and_freelancer_tax_mismatch():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(valid_employment_contract(employment_tax_type="freelancer_33"), {"email": "owner@example.com", "is_admin": True})
    assert exc.value.status_code == 400
    assert "4대보험" in exc.value.detail


def test_contract_requires_complete_worker_identity_for_real_use():
    service._write("employee_join_requests", [{
        "id": "join-mia", "name": "정보 미완료 직원", "email": "member@example.com",
        "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(
            valid_employment_contract(employee_address="", employee_phone="", employee_birth_date=""),
            {"email": "owner@example.com", "is_admin": True},
        )
    assert "근로자 주소" in exc.value.detail
    assert "근로자 연락처" in exc.value.detail
    assert "근로자 생년월일" in exc.value.detail


def test_contract_rejects_unregistered_employer_placeholders():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(
            valid_employment_contract(employer_registration_no="기초등록 필요", employer_representative="미등록"),
            {"email": "owner@example.com", "is_admin": True},
        )
    assert "사업자등록번호" in exc.value.detail
    assert "대표자" in exc.value.detail


def test_minor_contract_requires_guardian_identity_and_written_consent():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(valid_employment_contract(employee_birth_date="2010-01-01"), {"email": "owner@example.com", "is_admin": True})
    assert "친권자/후견인" in exc.value.detail

    saved = service.save_contract(
        valid_employment_contract(
            employee_birth_date="2010-01-01", minor_guardian_name="보호자",
            minor_guardian_phone="010-1111-2222", minor_guardian_consent="confirmed",
        ),
        {"email": "owner@example.com", "is_admin": True},
    )
    assert saved["minor_guardian_consent"] == "confirmed"


def test_part_time_contract_requires_workday_specific_schedule():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(
            valid_employment_contract(contract_type="part_time", wage_type="hourly", daily_work_schedule=""),
            {"email": "owner@example.com", "is_admin": True},
        )
    assert "근로일별 근로시간" in exc.value.detail


def test_foreign_worker_contract_requires_residence_fields():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(
            valid_employment_contract(foreign_worker=True, visa_status=""),
            {"email": "owner@example.com", "is_admin": True},
        )
    assert "체류자격" in exc.value.detail
    assert "외국인등록번호" in exc.value.detail


def test_contract_rejects_unconfirmed_wage_and_required_terms():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(valid_employment_contract(wage=0, holidays="", leave_terms=""), {"email": "owner@example.com", "is_admin": True})
    assert exc.value.status_code == 400
    assert "확정 임금" in exc.value.detail
    assert "휴일/주휴" in exc.value.detail


def test_regular_contract_accepts_qualified_200k_meal_allowance_breakdown():
    seed_approved_employee()
    saved = service.save_contract(
        valid_employment_contract(wage=3000000, meal_provision="cash_no_meal", base_salary=2800000, non_tax_meal_allowance=200000, taxable_allowance=0),
        {"email": "owner@example.com", "is_admin": True},
    )
    assert saved["base_salary"] == 2800000
    assert saved["non_tax_meal_allowance"] == 200000


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"wage": 3000000, "base_salary": 2700000, "non_tax_meal_allowance": 200000}, "합계"),
        ({"wage": 3000000, "base_salary": 2750000, "non_tax_meal_allowance": 250000, "meal_provision": "cash_no_meal"}, "200,000원"),
        ({"wage": 3000000, "base_salary": 2800000, "non_tax_meal_allowance": 200000, "meal_provision": "employer_meal"}, "식사를 제공"),
    ],
)
def test_regular_contract_rejects_invalid_meal_allowance_breakdown(overrides, detail):
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(valid_employment_contract(**overrides), {"email": "owner@example.com", "is_admin": True})
    assert exc.value.status_code == 400
    assert detail in exc.value.detail


def test_regular_under_five_contract_rejects_2026_minimum_wage_shortfall():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(
            valid_employment_contract(
                contract_date="2026-07-22", wage=2500000, workplace_size_category="under_5", weekly_hours="주 52시간 30분",
                meal_provision="cash_no_meal", base_salary=2300000, non_tax_meal_allowance=200000, taxable_allowance=0,
            ),
            {"email": "owner@example.com", "is_admin": True},
        )
    assert exc.value.status_code == 400
    assert "최저임금" in exc.value.detail


def test_payroll_keeps_taxable_and_qualified_non_tax_meal_components():
    saved = service.save_payroll(
        {
            "employee_name": "급여 테스트",
            "employee_email": "payroll@example.com",
            "gross_pay": 3000000,
            "taxable_pay": 2800000,
            "non_tax_meal_allowance": 200000,
            "meal_provision": "cash_no_meal",
        },
        {"email": "owner@example.com", "is_admin": True},
    )
    assert saved["taxable_pay"] == 2800000
    assert saved["non_tax_meal_allowance"] == 200000


def test_payroll_rejects_non_tax_meal_when_employer_provides_meal():
    with pytest.raises(service.HTTPException) as exc:
        service.save_payroll(
            {
                "employee_name": "급여 테스트",
                "employee_email": "payroll@example.com",
                "gross_pay": 3000000,
                "taxable_pay": 2800000,
                "non_tax_meal_allowance": 200000,
                "meal_provision": "employer_meal",
            },
            {"email": "owner@example.com", "is_admin": True},
        )
    assert exc.value.status_code == 400
    assert "식사를 제공" in exc.value.detail


def test_edit_after_signature_request_returns_contract_to_draft():
    seed_approved_employee()
    user = {"email": "owner@example.com", "is_admin": True}
    saved = service.save_contract(valid_employment_contract(), user)
    requested = service.request_contract_signature(saved["id"], user)
    edited = service.save_contract({**requested, "job_description": "수정된 매장 운영"}, user)
    assert edited["status"] == "draft"
    assert "sign_token" not in edited
    assert "requested_at" not in edited


def test_incomplete_legacy_contract_cannot_be_requested_for_signature():
    service._write("contracts", [{"id": "legacy-incomplete", "status": "draft", "employee_name": "기존 직원"}])
    with pytest.raises(service.HTTPException) as exc:
        service.request_contract_signature("legacy-incomplete", {"email": "owner@example.com", "is_admin": True})
    assert exc.value.status_code == 400


def test_signed_contract_is_hashed_and_cannot_be_changed_or_deleted():
    seed_approved_employee()
    admin = {"email": "owner@example.com", "is_admin": True}
    employee = {"email": "member@example.com", "is_admin": False}
    saved = service.save_contract(valid_employment_contract(), admin)
    requested = service.request_contract_signature(saved["id"], admin)
    token = requested["sign_token"]
    signed = service.sign_contract(valid_signature_payload(token), employee)
    assert signed["status"] == "signed"
    assert signed["signed_snapshot"]["employee_email"] == "member@example.com"
    assert len(signed["signed_snapshot_sha256"]) == 64
    assert len(signed["signature_sha256"]) == 64
    assert signed["signature_consent"]["accepted"] is True
    assert signed["signature_audit"]["client_ip"] == "203.0.113.10"
    assert signed["signature_audit"]["user_agent"] == "pytest-browser"
    assert len(signed["sign_token_hash"]) == 64
    assert "sign_token" not in signed
    with pytest.raises(service.HTTPException) as edit_exc:
        service.save_contract({**signed, "wage": 1}, admin)
    assert edit_exc.value.status_code == 409
    with pytest.raises(service.HTTPException) as delete_exc:
        service.delete_contract(signed["id"], admin)
    assert delete_exc.value.status_code == 409


def test_contract_signing_requires_target_employee_and_blocks_admin():
    seed_approved_employee()
    admin = {"email": "owner@example.com", "is_admin": True}
    employee = {"email": "member@example.com", "is_admin": False}
    other_employee = {"email": "other@example.com", "is_admin": False}
    saved = service.save_contract(valid_employment_contract(), admin)
    requested = service.request_contract_signature(saved["id"], admin)
    token = requested["sign_token"]

    assert service.get_contract_by_token(token, employee)["id"] == saved["id"]
    with pytest.raises(service.HTTPException) as wrong_view:
        service.get_contract_by_token(token, other_employee)
    assert wrong_view.value.status_code == 403
    with pytest.raises(service.HTTPException) as admin_sign:
        service.sign_contract(valid_signature_payload(token), admin)
    assert admin_sign.value.status_code == 403


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"consent": False}, "동의"),
        ({"signer_name": "다른 이름"}, "이름"),
        ({"signature_data_uri": "data:text/plain;base64,dGVzdA=="}, "PNG"),
    ],
)
def test_contract_signing_rejects_missing_proof(overrides, message):
    seed_approved_employee()
    admin = {"email": "owner@example.com", "is_admin": True}
    employee = {"email": "member@example.com", "is_admin": False}
    saved = service.save_contract(valid_employment_contract(), admin)
    requested = service.request_contract_signature(saved["id"], admin)

    with pytest.raises(service.HTTPException) as exc:
        service.sign_contract(valid_signature_payload(requested["sign_token"], **overrides), employee)
    assert exc.value.status_code == 400
    assert message in exc.value.detail


def test_contract_rejects_employee_from_another_business():
    service._write(
        "employee_join_requests",
        [
            {
                "id": "join-junghwa",
                "name": "중화 직원",
                "email": "junghwa@example.com",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "status": "approved",
            }
        ],
    )

    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(
            {
                "employee_request_id": "join-junghwa",
                "business_id": "biz-mia",
                "branch": "미아점",
                "contract_type": "regular",
            },
            {"email": "owner@example.com", "is_admin": True},
        )

    assert exc.value.status_code == 400
    assert "해당 사업자 소속" in exc.value.detail


def test_platform_account_db_payload_never_contains_secret_fields():
    payload = service._db_payload_record(
        "platform_accounts",
        {"id": "acct-1", "service": "baemin", "password": "plain", "password_enc": "cipher"},
    )

    assert payload == {"id": "acct-1", "service": "baemin"}


def test_platform_account_db_read_restores_secret_from_protected_file_only():
    db_rows = [{"id": "acct-baemin", "service": "baemin", "username": "owner"}]
    file_rows = [
        {
            "id": "acct-baemin",
            "service": "baemin",
            "username": "owner",
            "password_enc": "ciphertext",
        }
    ]

    merged = service._attach_local_account_secrets(db_rows, file_rows)

    assert merged[0]["password_enc"] == "ciphertext"
    assert "password_enc" not in db_rows[0]


def test_hydrate_delivery_account_passwords_from_agent_vault_matches_origin_and_username(monkeypatch):
    rows = [
        {
            "id": "acct-yogiyo-mia",
            "service": "yogiyo",
            "username": "mia-owner",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "portal_status": "action_required",
        }
    ]

    monkeypatch.setattr(service, "_db_available", lambda: True)
    monkeypatch.setattr(
        service,
        "_run_db",
        lambda coro: (
            coro.close(),
            [
                {
                    "id": "vault-yogiyo",
                    "origin": "https://ceo.yogiyo.co.kr",
                    "username_enc": "enc-mia-owner",
                    "password_enc": "enc-vault-password",
                }
            ],
        )[1],
    )
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "mia-owner" if value == "enc-mia-owner" else "")

    changed = service._hydrate_delivery_account_passwords_from_agent_vault(rows)

    assert changed == 1
    assert rows[0]["password_enc"] == "enc-vault-password"
    assert rows[0]["password_source"] == "agent_vault"
    assert rows[0]["agent_vault_credential_id"] == "vault-yogiyo"
    assert rows[0]["portal_status"] == "credential_registered"


def test_hydrate_delivery_account_passwords_from_agent_vault_requires_username_match(monkeypatch):
    rows = [{"id": "acct-ddangyo-mia", "service": "ddangyo", "username": "mia-owner"}]

    monkeypatch.setattr(service, "_db_available", lambda: True)
    monkeypatch.setattr(
        service,
        "_run_db",
        lambda coro: (
            coro.close(),
            [
                {
                    "id": "vault-ddangyo",
                    "origin": "https://boss.ddangyo.com",
                    "username_enc": "enc-other-owner",
                    "password_enc": "enc-vault-password",
                }
            ],
        )[1],
    )
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "other-owner" if value == "enc-other-owner" else "")

    changed = service._hydrate_delivery_account_passwords_from_agent_vault(rows)

    assert changed == 0
    assert "password_enc" not in rows[0]


def test_hydrate_delivery_account_passwords_from_agent_vault_uses_explicit_scope_metadata(monkeypatch):
    rows = [
        {
            "id": "acct-ddangyo-mia",
            "service": "ddangyo",
            "username": "mia-owner",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
        }
    ]

    monkeypatch.setattr(service, "_db_available", lambda: True)
    monkeypatch.setattr(
        service,
        "_run_db",
        lambda coro: (
            coro.close(),
            [
                {
                    "id": "vault-ddangyo-mia",
                    "origin": "https://boss.ddangyo.com",
                    "username_enc": "enc-other-owner",
                    "password_enc": "enc-vault-password",
                    "metadata": {"service": "ddangyo", "business_id": "biz-mia", "branch": "열정국밥_미아점"},
                }
            ],
        )[1],
    )
    monkeypatch.setattr(service, "_decrypt_secret", lambda value: "other-owner" if value == "enc-other-owner" else "")

    changed = service._hydrate_delivery_account_passwords_from_agent_vault(rows)

    assert changed == 1
    assert rows[0]["password_enc"] == "enc-vault-password"
    assert rows[0]["agent_vault_credential_id"] == "vault-ddangyo-mia"


def test_onboarding_documents_include_missing_required_rows_for_approved_employee(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    service._write(
        "employee_join_requests",
        [
            {
                "id": "join-1",
                "name": "하영훈",
                "email": "du-test@example.com",
                "email_masked": "du***@example.com",
                "branch": "중화점",
                "status": "approved",
                "reviewed_at": "2026-07-16T10:22:49+09:00",
            }
        ],
    )
    service._write("onboarding_documents", [])

    rows = service.list_onboarding_documents({"email": "owner@example.com", "is_admin": True})

    missing = [row for row in rows if row["employee_name"] == "하영훈" and row["status"] == "missing"]
    assert {row["document_type"] for row in missing} == {
        "resident_register",
        "id_card",
        "bankbook",
        "health_certificate",
    }
    assert all(row["missing_document"] is True for row in missing)


def test_onboarding_documents_do_not_include_pending_rows_for_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    service._write(
        "employee_join_requests",
        [
            {
                "id": "join-pending",
                "name": "대기직원",
                "email": "pending@example.com",
                "branch": "중화점",
                "status": "pending",
                "requested_at": "2026-07-16T10:22:49+09:00",
            }
        ],
    )
    service._write("onboarding_documents", [])

    rows = service.list_onboarding_documents({"email": "owner@example.com", "is_admin": True})

    assert all(row["employee_email"] != "pending@example.com" for row in rows)


def test_onboarding_documents_filter_legacy_rows_by_inferred_business():
    service._write(
        "onboarding_documents",
        [
            {"id": "doc-j", "employee_email": "j@example.com", "branch": "중화점", "document_type": "bankbook", "status": "uploaded"},
            {"id": "doc-m", "employee_email": "m@example.com", "branch": "강북미아점", "document_type": "bankbook", "status": "uploaded"},
        ],
    )

    rows = service.list_onboarding_documents(
        {"email": "owner@example.com", "is_admin": True},
        "biz-junghwa",
    )

    assert [row["id"] for row in rows] == ["doc-j"]
    assert rows[0]["business_id"] == "biz-junghwa"


@pytest.mark.asyncio
async def test_registered_employee_upload_keeps_join_request_business_scope():
    service._write(
        "employee_join_requests",
        [
            {
                "id": "join-j",
                "name": "가입 직원",
                "email": "employee@example.com",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "status": "approved",
            }
        ],
    )
    upload = UploadFile(filename="bankbook.pdf", file=BytesIO(b"pdf"))

    saved = await service.save_onboarding_document(
        employee_name="가입 직원",
        employee_email="employee@example.com",
        branch="중화점",
        document_type="bankbook",
        issue_date="2026-07-21",
        memo="",
        upload=upload,
        user={"email": "employee@example.com", "is_admin": False},
    )

    assert saved["employee_request_id"] == "join-j"
    assert saved["business_id"] == "biz-junghwa"
    assert saved["branch"] == "중화점"
