import base64
import os
import importlib.util
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
        "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])


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
    assert [item["id"] for item in loaded["settings"]["businesses"]] == ["biz-junghwa", "biz-sungshin", "biz-mia"]
    assert [item["businessId"] for item in loaded["settings"]["branches"]] == ["biz-junghwa", "biz-sungshin", "biz-mia"]
    raw = settings_path.read_text(encoding="utf-8")
    assert "category_rules" in raw
    assert "biz-corp" not in raw
    assert "ignored" not in raw


def test_save_settings_keeps_only_three_canonical_businesses(tmp_path, monkeypatch):
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
        "열정국밥_미아점",
    ]
    assert {item["id"] for item in settings["businesses"]} == {"biz-junghwa", "biz-sungshin", "biz-mia"}
    assert {item["businessId"] for item in settings["branches"]} == {"biz-junghwa", "biz-sungshin", "biz-mia"}
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
                "settlements": [],
                "reviews": [],
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
    assert second["totals"]["sales"] == 1
    assert len(service.list_sales(user, "biz-mia")) == 1
    statuses = service.list_collection_status(user, "biz-mia")
    assert len(statuses) == 2
    assert all(row["status"] == "succeeded" for row in statuses)


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
    assert saved["template_version"] == "majangbiseo-employment-2026-07-a4"
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
    assert saved["template_version"] == "majangbiseo-freelancer-2026-07-a4"
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


def test_contract_rejects_employment_and_freelancer_tax_mismatch():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(valid_employment_contract(employment_tax_type="freelancer_33"), {"email": "owner@example.com", "is_admin": True})
    assert exc.value.status_code == 400
    assert "4대보험" in exc.value.detail


def test_contract_rejects_unconfirmed_wage_and_required_terms():
    seed_approved_employee()
    with pytest.raises(service.HTTPException) as exc:
        service.save_contract(valid_employment_contract(wage=0, holidays="", leave_terms=""), {"email": "owner@example.com", "is_admin": True})
    assert exc.value.status_code == 400
    assert "확정 임금" in exc.value.detail
    assert "휴일/주휴" in exc.value.detail


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
