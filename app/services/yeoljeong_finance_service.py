"""JSON-backed store for the Yeoljeong store assistant app.

The app is a static SPA, so this service keeps the first operational HR flow
small and explicit: employee join requests, onboarding documents, contracts,
payroll statements, and delivery account status.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, UploadFile

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("YEOLJEONG_FINANCE_DATA_DIR", "app/data/yeoljeong_finance"))
UPLOAD_DIR = DATA_DIR / "uploads" / "onboarding"
EVIDENCE_UPLOAD_DIR = DATA_DIR / "uploads" / "evidence"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_SIGNATURE_BYTES = 256 * 1024
DELIVERY_SYNC_STALE_AFTER = timedelta(minutes=15)
CONTRACT_SIGNATURE_CONSENT_VERSION = "yeoljeong-contract-sign-v1"

DOCUMENT_TYPES: list[dict[str, str]] = [
    {
        "type": "resident_register",
        "label": "주민등록등본",
        "requirement": "필수",
        "notice": "주민등록번호 뒷자리와 가족정보는 마스킹본을 원칙으로 합니다.",
    },
    {"type": "id_card", "label": "신분증", "requirement": "필수", "notice": "주민등록번호 뒷자리는 마스킹합니다."},
    {"type": "bankbook", "label": "통장사본", "requirement": "필수", "notice": "급여 입금 계좌 확인용입니다."},
    {"type": "health_certificate", "label": "보건증", "requirement": "필수", "notice": "식품위생 업종 제출 서류입니다."},
    {"type": "employment_contract_info", "label": "계약 정보 확인서", "requirement": "선택", "notice": "근무조건 확인용입니다."},
    {"type": "foreign_registration", "label": "외국인등록증", "requirement": "외국인 조건부", "notice": "체류자격과 취업 가능 여부를 확인합니다."},
    {"type": "visa_status_certificate", "label": "체류자격 증빙", "requirement": "외국인 조건부", "notice": "비자 유형별 취업 가능 범위를 확인합니다."},
    {"type": "work_permission_confirmation", "label": "취업활동 가능 확인", "requirement": "외국인 조건부", "notice": "고용허가/시간제취업 허가 등을 확인합니다."},
    {"type": "foreign_employment_report", "label": "외국인 고용 신고/변동 확인", "requirement": "외국인 조건부", "notice": "신고 대상 여부와 처리일을 기록합니다."},
    {"type": "other", "label": "기타 서류", "requirement": "선택", "notice": "수집 사유를 메모에 남깁니다."},
]

CONNECTOR_LABELS = {
    "baemin": "배민셀프서비스",
    "coupangeats": "쿠팡이츠",
    "yogiyo": "요기요",
    "ddangyo": "땡겨요",
    "matepos": "메이트포스",
    "shinhan_business": "신한은행 기업",
    "ibk_business": "기업은행 기업",
    "coupang_supplier": "쿠팡 매입처",
    "marketbom": "마켓봄",
    "newtong": "뉴통",
    "baljugo": "발주고",
    "supplier_custom_portal": "기타 매입처 주문프로그램",
    "supplier_statement_upload": "거래내역서/영수증 업로드",
    "hometax": "홈택스",
    "tax_invoice_upload": "계산서/증빙 업로드",
    "card_pg": "카드사/PG 매출",
    "utility_bills": "공과금 고지서",
    "accountant_tax_agent": "세무대리인/회계프로그램",
}
PLATFORM_LABELS = {
    key: CONNECTOR_LABELS[key]
    for key in ("baemin", "coupangeats", "yogiyo", "ddangyo")
}
DELIVERY_UPLOAD_COLLECTION_MODES = {"portal-csv", "csv-upload", "statement-upload", "upload_queue", "manual"}
FINANCIAL_TRANSACTION_SERVICES = {
    "shinhan_business",
    "ibk_business",
    "card_pg",
}
TRANSACTION_SOURCE_BY_SERVICE = {
    "shinhan_business": "bank",
    "ibk_business": "bank",
    "card_pg": "card",
}
BANK_QUICK_SERVICE_CONFIG = {
    "shinhan_business": {
        "label": "신한은행 간편서비스",
        "login_url": "https://bank.shinhan.com/rib/easy/index.jsp",
        "enrollment": "기업뱅킹에서 간편조회 허용 계좌 등록 후 간편서비스 계좌조회로 거래내역을 확인합니다.",
    },
    "ibk_business": {
        "label": "IBK기업은행 빠른서비스",
        "login_url": "https://mybank.ibk.co.kr/uib/jsp/guest/qcs/qcs10/qcs1020/PQCS102000_i.jsp",
        "enrollment": "기업뱅킹의 빠른조회서비스 신청/해제에서 대상 계좌를 등록한 뒤 빠른조회로 거래내역을 확인합니다.",
    },
}

DEFAULT_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("식자재", ("쌀", "백미", "고춧가루", "소스", "김치", "대파", "양파", "고기", "식자재")),
    ("배달앱", ("배민", "배달의민족", "요기요", "쿠팡이츠", "정산")),
    ("임차료", ("월세", "임대료", "관리비")),
    ("인건비", ("급여", "4대보험", "고용", "알바", "직원")),
    ("공과금", ("전기", "가스", "수도", "통신", "인터넷")),
    ("카드수수료", ("카드수수료", "수수료")),
    ("비품", ("비품", "소모품", "주방", "용기", "포장")),
)

SETTINGS_TABLES = ("yeoljeong_businesses", "yeoljeong_branches", "yeoljeong_settings")
HR_LEDGER_TABLES = (
    "yeoljeong_employee_join_requests",
    "yeoljeong_onboarding_documents",
    "yeoljeong_contracts",
    "yeoljeong_payroll_statements",
)
DELIVERY_LEDGER_TABLES = (
    "yeoljeong_platform_accounts",
    "yeoljeong_delivery_sales",
    "yeoljeong_delivery_settlements",
    "yeoljeong_delivery_reviews",
    "yeoljeong_delivery_collection_status",
)
JSON_LEDGER_FILES = (
    "employee_join_requests",
    "onboarding_documents",
    "contracts",
    "payroll_statements",
)
DB_LEDGER_TABLE_BY_NAME = {
    "employee_join_requests": "yeoljeong_employee_join_requests",
    "onboarding_documents": "yeoljeong_onboarding_documents",
    "contracts": "yeoljeong_contracts",
    "payroll_statements": "yeoljeong_payroll_statements",
    "platform_accounts": "yeoljeong_platform_accounts",
    "delivery_sales": "yeoljeong_delivery_sales",
    "delivery_settlements": "yeoljeong_delivery_settlements",
    "delivery_reviews": "yeoljeong_delivery_reviews",
    "delivery_collection_status": "yeoljeong_delivery_collection_status",
}
GENERIC_DB_LEDGER_NAMES = {
    "platform_accounts",
    "delivery_sales",
    "delivery_settlements",
    "delivery_reviews",
    "delivery_collection_status",
}

CONTRACT_TEMPLATE_META = {
    "freelancer": {
        "document_kind": "freelancer_service_contract",
        "template_version": "majangbiseo-freelancer-2026-07-identity-table-v3",
        "print_title": "3.3% 프리랜서 용역계약서",
    },
    "confidentiality": {
        "document_kind": "confidentiality_agreement",
        "template_version": "majangbiseo-confidentiality-2026-07-identity-table-v3",
        "print_title": "보안 및 개인정보 보호 서약서",
    },
    "default": {
        "document_kind": "standard_employment_contract",
        "template_version": "majangbiseo-employment-2026-07-identity-table-v3",
        "print_title": "표준근로계약서",
    },
}

EMPLOYMENT_CONTRACT_TYPES = {"part_time", "regular", "manager"}
VALID_CONTRACT_TYPES = EMPLOYMENT_CONTRACT_TYPES | {"freelancer", "confidentiality"}
CONTRACT_SNAPSHOT_EXCLUDED_FIELDS = {
    "sign_token",
    "sign_token_hash",
    "signed_snapshot",
    "signed_snapshot_sha256",
    "signature_data_uri",
    "updated_at",
}

CANONICAL_BUSINESSES: list[dict[str, Any]] = [
    {
        "id": "biz-junghwa",
        "entityType": "corporation",
        "name": "열정국밥 중화점",
        "registrationNo": "710-86-04499",
        "representative": "오윤희",
        "taxType": "일반과세",
        "openedAt": "2026-07-01",
        "address": "서울특별시 중랑구 봉화산로27길 8, 1층(중화동)",
        "memo": "법인사업자 / 법인등록번호 110111-0961922 / 주류판매신고번호 146-5-11334",
    },
    {
        "id": "biz-sungshin",
        "entityType": "individual",
        "name": "열정국밥 성신여대점",
        "registrationNo": "기초등록 필요",
        "representative": "미등록",
        "taxType": "일반과세",
        "openedAt": "",
        "address": "",
        "memo": "개인사업자 2",
    },
    {
        "id": "biz-mia",
        "entityType": "individual",
        "name": "열정국밥_미아점",
        "registrationNo": "874-21-02160",
        "representative": "최미미",
        "taxType": "일반과세",
        "openedAt": "2025-04-01",
        "address": "서울특별시 강북구 도봉로76길 42, 1층 점포일부(좌측)",
        "memo": "개인사업자 3 / 주류판매신고번호 210-5-62608",
    },
]

CANONICAL_BRANCHES: list[dict[str, Any]] = [
    {"id": "branch-junghwa", "name": "중화점", "businessId": "biz-junghwa", "status": "active", "phone": "", "address": "서울특별시 중랑구 봉화산로27길 8, 1층(중화동)"},
    {"id": "branch-sungshin", "name": "성신여대점", "businessId": "biz-sungshin", "status": "active", "phone": "", "address": ""},
    {"id": "branch-gangbuk-mia", "name": "열정국밥_미아점", "businessId": "biz-mia", "status": "active", "phone": "", "address": "서울특별시 강북구 도봉로76길 42, 1층 점포일부(좌측)"},
]

CANONICAL_BUSINESS_IDS = {item["id"] for item in CANONICAL_BUSINESSES}
CANONICAL_BRANCH_NAMES = {item["name"] for item in CANONICAL_BRANCHES}
MIA_BUSINESS_ID = "biz-mia"
MIA_BRANCH_NAME = "열정국밥_미아점"
BRANCH_ALIASES = {
    "열정국밥 강북미아점": MIA_BRANCH_NAME,
    "강북미아점": MIA_BRANCH_NAME,
    "미아점": MIA_BRANCH_NAME,
}
BUSINESS_BY_BRANCH = {item["name"]: item["businessId"] for item in CANONICAL_BRANCHES}


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _evidence_upload_dir().mkdir(parents=True, exist_ok=True)


def _evidence_upload_dir() -> Path:
    return DATA_DIR / "uploads" / "evidence"


def _path(name: str) -> Path:
    _ensure_dirs()
    return DATA_DIR / f"{name}.json"


def _read_file_rows(name: str) -> list[dict[str, Any]]:
    path = _path(name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"{name} 저장소 JSON이 손상되었습니다")
    if isinstance(data, dict):
        data = data.get(name) or data.get("items") or data.get("records") or []
    return data if isinstance(data, list) else []


def _write_file_rows(name: str, rows: list[dict[str, Any]]) -> None:
    path = _path(name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json_object(name: str) -> dict[str, Any]:
    path = _path(name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"{name} 저장소 JSON이 손상되었습니다")
    return data if isinstance(data, dict) else {}


def _write_json_object(name: str, data: dict[str, Any]) -> None:
    path = _path(name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _db_url() -> str:
    url = os.getenv("YEOLJEONG_FINANCE_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    return url.replace("postgresql://", "postgres://") if url else ""


def _db_available() -> bool:
    return bool(_db_url())


def _run_db(coro: Any) -> Any:
    if not _db_available():
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None
    try:
        return asyncio.run(coro)
    except Exception:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None
    try:
        return asyncio.run(coro)
    except Exception:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None


async def _db_table_exists(table: str) -> bool:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"))
    finally:
        await conn.close()


def _table_ready(table: str) -> bool:
    return bool(_run_db(_db_table_exists(table)))


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat(timespec="seconds")
        except TypeError:
            return value.isoformat()
    return str(value)


def _pg_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.replace(tzinfo=KST)
            except ValueError:
                continue
    return None


def _payload_dict(value: Any) -> dict[str, Any]:
    parsed = _jsonb_object(value)
    return parsed if isinstance(parsed, dict) else {}


def _db_row_to_record(name: str, row: Any) -> dict[str, Any]:
    item = dict(row)
    if name in GENERIC_DB_LEDGER_NAMES:
        payload = _payload_dict(item.get("payload"))
        return {
            **payload,
            "id": str(payload.get("id") or item.get("row_id") or ""),
            "business_id": payload.get("business_id") or item.get("business_id") or "",
            "branch": payload.get("branch") or item.get("branch") or "",
            "created_at": payload.get("created_at") or _iso(item.get("created_at")),
            "updated_at": payload.get("updated_at") or _iso(item.get("updated_at")),
        }
    if name == "employee_join_requests":
        payload = _payload_dict(item.get("request_payload"))
        email = str(payload.get("email") or item.get("employee_email") or "").strip().lower()
        return {
            **payload,
            "id": str(payload.get("id") or item.get("id") or ""),
            "email": email,
            "email_masked": payload.get("email_masked") or item.get("employee_email_masked") or _mask_email(email),
            "name": payload.get("name") or item.get("employee_name") or "",
            "phone": payload.get("phone") or item.get("phone") or "",
            "business_id": payload.get("business_id") or item.get("business_id") or "",
            "branch": payload.get("branch") or item.get("branch") or "",
            "role": payload.get("role") or item.get("role") or "employee",
            "status": payload.get("status") or item.get("status") or "pending",
            "review_memo": payload.get("review_memo") or item.get("review_memo") or "",
            "requested_by": payload.get("requested_by") or item.get("requested_by") or "",
            "reviewed_by": payload.get("reviewed_by") or item.get("reviewed_by") or "",
            "requested_at": payload.get("requested_at") or _iso(item.get("requested_at")),
            "reviewed_at": payload.get("reviewed_at") or _iso(item.get("reviewed_at")),
            "created_at": payload.get("created_at") or _iso(item.get("created_at")),
            "updated_at": payload.get("updated_at") or _iso(item.get("updated_at")),
        }
    if name == "onboarding_documents":
        payload = _payload_dict(item.get("metadata"))
        email = str(payload.get("employee_email") or item.get("employee_email") or "").strip().lower()
        return {
            **payload,
            "id": str(payload.get("id") or item.get("id") or ""),
            "employee_request_id": payload.get("employee_request_id") or item.get("employee_request_id") or "",
            "employee_email": email,
            "employee_email_masked": payload.get("employee_email_masked") or item.get("employee_email_masked") or _mask_email(email),
            "employee_name": payload.get("employee_name") or item.get("employee_name") or "",
            "business_id": payload.get("business_id") or item.get("business_id") or "",
            "branch": payload.get("branch") or item.get("branch") or "",
            "document_type": payload.get("document_type") or item.get("document_type") or "",
            "document_label": payload.get("document_label") or item.get("document_label") or "",
            "requirement": payload.get("requirement") or item.get("requirement") or "",
            "status": payload.get("status") or item.get("status") or "uploaded",
            "original_filename": payload.get("original_filename") or item.get("original_filename") or "",
            "stored_filename": payload.get("stored_filename") or item.get("stored_filename") or "",
            "content_type": payload.get("content_type") or item.get("content_type") or "",
            "size_bytes": payload.get("size_bytes") if payload.get("size_bytes") is not None else int(item.get("size_bytes") or 0),
            "issue_date": payload.get("issue_date") or item.get("issue_date") or "",
            "memo": payload.get("memo") or item.get("memo") or "",
            "review_memo": payload.get("review_memo") or item.get("review_memo") or "",
            "uploaded_by": payload.get("uploaded_by") or item.get("uploaded_by") or "",
            "reviewed_by": payload.get("reviewed_by") or item.get("reviewed_by") or "",
            "uploaded_at": payload.get("uploaded_at") or _iso(item.get("uploaded_at")),
            "reviewed_at": payload.get("reviewed_at") or _iso(item.get("reviewed_at")),
            "created_at": payload.get("created_at") or _iso(item.get("created_at")),
            "updated_at": payload.get("updated_at") or _iso(item.get("updated_at")),
        }
    if name == "contracts":
        payload = _payload_dict(item.get("contract_payload"))
        email = str(payload.get("employee_email") or item.get("employee_email") or "").strip().lower()
        return {
            **payload,
            "id": str(payload.get("id") or item.get("id") or ""),
            "employee_email": email,
            "employee_email_masked": payload.get("employee_email_masked") or item.get("employee_email_masked") or _mask_email(email),
            "employee_name": payload.get("employee_name") or item.get("employee_name") or "",
            "business_id": payload.get("business_id") or item.get("business_id") or "",
            "branch": payload.get("branch") or item.get("branch") or "",
            "contract_type": payload.get("contract_type") or item.get("contract_type") or "part_time",
            "document_kind": payload.get("document_kind") or item.get("document_kind") or "standard_employment_contract",
            "template_version": payload.get("template_version") or item.get("template_version") or "",
            "print_title": payload.get("print_title") or item.get("print_title") or "",
            "status": payload.get("status") or item.get("status") or "draft",
            "requested_at": payload.get("requested_at") or _iso(item.get("requested_at")),
            "signed_at": payload.get("signed_at") or _iso(item.get("signed_at")),
            "created_at": payload.get("created_at") or _iso(item.get("created_at")),
            "updated_at": payload.get("updated_at") or _iso(item.get("updated_at")),
        }
    if name == "payroll_statements":
        payload = _payload_dict(item.get("statement_payload"))
        email = str(payload.get("employee_email") or item.get("employee_email") or "").strip().lower()
        return {
            **payload,
            "id": str(payload.get("id") or item.get("id") or ""),
            "employee_email": email,
            "employee_email_masked": payload.get("employee_email_masked") or item.get("employee_email_masked") or _mask_email(email),
            "employee_name": payload.get("employee_name") or item.get("employee_name") or "",
            "business_id": payload.get("business_id") or item.get("business_id") or "",
            "branch": payload.get("branch") or item.get("branch") or "",
            "payroll_month": payload.get("payroll_month") or item.get("payroll_month") or "",
            "gross_pay": int(payload.get("gross_pay") if payload.get("gross_pay") is not None else item.get("gross_pay") or 0),
            "tax_withholding": int(payload.get("tax_withholding") if payload.get("tax_withholding") is not None else item.get("tax_withholding") or 0),
            "insurance_deduction": int(payload.get("insurance_deduction") if payload.get("insurance_deduction") is not None else item.get("insurance_deduction") or 0),
            "other_deduction": int(payload.get("other_deduction") if payload.get("other_deduction") is not None else item.get("other_deduction") or 0),
            "net_pay": int(payload.get("net_pay") if payload.get("net_pay") is not None else item.get("net_pay") or 0),
            "status": payload.get("status") or item.get("status") or "draft",
            "created_at": payload.get("created_at") or _iso(item.get("created_at")),
            "updated_at": payload.get("updated_at") or _iso(item.get("updated_at")),
        }
    return item


async def _db_fetch_ledger(name: str) -> list[dict[str, Any]] | None:
    import asyncpg

    table = DB_LEDGER_TABLE_BY_NAME.get(name)
    if not table:
        return None
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        ready = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
        if not ready:
            return None
        key = "row_id" if name in GENERIC_DB_LEDGER_NAMES else "id"
        rows = await conn.fetch(f"SELECT * FROM {table} WHERE deleted_at IS NULL ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, {key} DESC")
        return [_db_row_to_record(name, row) for row in rows]
    finally:
        await conn.close()


def _db_payload_record(name: str, record: dict[str, Any]) -> dict[str, Any]:
    if name == "platform_accounts":
        return {key: value for key, value in record.items() if key not in _ACCOUNT_SECRET_FIELDS}
    return record


def _attach_local_account_secrets(
    db_rows: list[dict[str, Any]], file_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Restore account secrets from the protected file without copying them to DB."""
    file_by_id = {str(row.get("id") or ""): row for row in file_rows if row.get("id")}
    merged_rows: list[dict[str, Any]] = []
    for db_row in db_rows:
        merged = {**db_row}
        local = file_by_id.get(str(db_row.get("id") or ""), {})
        for field in _ACCOUNT_SECRET_FIELDS:
            if local.get(field):
                merged[field] = local[field]
        merged_rows.append(merged)
    return merged_rows


async def _db_upsert_ledger(name: str, record: dict[str, Any]) -> bool:
    import asyncpg

    table = DB_LEDGER_TABLE_BY_NAME.get(name)
    if not table:
        return False
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        ready = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
        if not ready:
            return False
        payload = json.dumps(_db_payload_record(name, record), ensure_ascii=False)
        record_id = str(record.get("id") or uuid4())
        now = _now()
        if name in GENERIC_DB_LEDGER_NAMES:
            await conn.execute(
                f"""
                INSERT INTO {table} (row_id, business_id, branch, created_at, updated_at, payload)
                VALUES ($1, $2, $3, $4::timestamptz, $5::timestamptz, $6::jsonb)
                ON CONFLICT (row_id) DO UPDATE SET
                    business_id = EXCLUDED.business_id,
                    branch = EXCLUDED.branch,
                    updated_at = EXCLUDED.updated_at,
                    deleted_at = NULL,
                    payload = EXCLUDED.payload
                """,
                record_id,
                str(record.get("business_id") or ""),
                str(record.get("branch") or ""),
                _pg_ts(record.get("created_at") or now),
                _pg_ts(record.get("updated_at") or now),
                payload,
            )
            return True
        if name == "employee_join_requests":
            await conn.execute(
                """
                INSERT INTO yeoljeong_employee_join_requests
                    (id, employee_email, employee_email_masked, employee_name, phone, business_id, branch, role, status,
                     request_payload, review_memo, requested_by, reviewed_by, requested_at, reviewed_at, updated_at, deleted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14::timestamptz, $15::timestamptz, $16::timestamptz, NULL)
                ON CONFLICT (id) DO UPDATE SET
                    employee_email = EXCLUDED.employee_email,
                    employee_email_masked = EXCLUDED.employee_email_masked,
                    employee_name = EXCLUDED.employee_name,
                    phone = EXCLUDED.phone,
                    business_id = EXCLUDED.business_id,
                    branch = EXCLUDED.branch,
                    role = EXCLUDED.role,
                    status = EXCLUDED.status,
                    request_payload = EXCLUDED.request_payload,
                    review_memo = EXCLUDED.review_memo,
                    requested_by = EXCLUDED.requested_by,
                    reviewed_by = EXCLUDED.reviewed_by,
                    requested_at = EXCLUDED.requested_at,
                    reviewed_at = EXCLUDED.reviewed_at,
                    updated_at = EXCLUDED.updated_at,
                    deleted_at = NULL
                """,
                record_id,
                str(record.get("email") or record.get("employee_email") or "").strip().lower(),
                str(record.get("email_masked") or record.get("employee_email_masked") or ""),
                str(record.get("name") or record.get("employee_name") or ""),
                str(record.get("phone") or ""),
                str(record.get("business_id") or ""),
                str(record.get("branch") or ""),
                str(record.get("role") or "employee"),
                str(record.get("status") or "pending"),
                payload,
                str(record.get("review_memo") or ""),
                str(record.get("requested_by") or ""),
                str(record.get("reviewed_by") or ""),
                _pg_ts(record.get("requested_at") or record.get("created_at") or now),
                _pg_ts(record.get("reviewed_at")),
                _pg_ts(record.get("updated_at") or now),
            )
            return True
        if name == "onboarding_documents":
            await conn.execute(
                """
                INSERT INTO yeoljeong_onboarding_documents
                    (id, employee_request_id, employee_email, employee_email_masked, employee_name, business_id, branch,
                     document_type, document_label, requirement, status, original_filename, stored_filename, content_type,
                     size_bytes, issue_date, memo, review_memo, uploaded_by, reviewed_by, uploaded_at, reviewed_at, updated_at, metadata, deleted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                        $21::timestamptz, $22::timestamptz, $23::timestamptz, $24::jsonb, NULL)
                ON CONFLICT (id) DO UPDATE SET
                    employee_request_id = EXCLUDED.employee_request_id,
                    employee_email = EXCLUDED.employee_email,
                    employee_email_masked = EXCLUDED.employee_email_masked,
                    employee_name = EXCLUDED.employee_name,
                    business_id = EXCLUDED.business_id,
                    branch = EXCLUDED.branch,
                    document_type = EXCLUDED.document_type,
                    document_label = EXCLUDED.document_label,
                    requirement = EXCLUDED.requirement,
                    status = EXCLUDED.status,
                    original_filename = EXCLUDED.original_filename,
                    stored_filename = EXCLUDED.stored_filename,
                    content_type = EXCLUDED.content_type,
                    size_bytes = EXCLUDED.size_bytes,
                    issue_date = EXCLUDED.issue_date,
                    memo = EXCLUDED.memo,
                    review_memo = EXCLUDED.review_memo,
                    uploaded_by = EXCLUDED.uploaded_by,
                    reviewed_by = EXCLUDED.reviewed_by,
                    uploaded_at = EXCLUDED.uploaded_at,
                    reviewed_at = EXCLUDED.reviewed_at,
                    updated_at = EXCLUDED.updated_at,
                    metadata = EXCLUDED.metadata,
                    deleted_at = NULL
                """,
                record_id,
                str(record.get("employee_request_id") or ""),
                str(record.get("employee_email") or "").strip().lower(),
                str(record.get("employee_email_masked") or ""),
                str(record.get("employee_name") or ""),
                str(record.get("business_id") or ""),
                str(record.get("branch") or ""),
                str(record.get("document_type") or ""),
                str(record.get("document_label") or ""),
                str(record.get("requirement") or ""),
                str(record.get("status") or "uploaded"),
                str(record.get("original_filename") or ""),
                str(record.get("stored_filename") or ""),
                str(record.get("content_type") or ""),
                int(record.get("size_bytes") or 0),
                str(record.get("issue_date") or ""),
                str(record.get("memo") or ""),
                str(record.get("review_memo") or ""),
                str(record.get("uploaded_by") or ""),
                str(record.get("reviewed_by") or ""),
                _pg_ts(record.get("uploaded_at") or record.get("created_at") or now),
                _pg_ts(record.get("reviewed_at")),
                _pg_ts(record.get("updated_at") or now),
                payload,
            )
            return True
        if name == "contracts":
            token = str(record.get("sign_token") or "")
            token_hash = str(record.get("sign_token_hash") or "")
            await conn.execute(
                """
                INSERT INTO yeoljeong_contracts
                    (id, employee_email, employee_email_masked, employee_name, business_id, branch, contract_type,
                     document_kind, template_version, print_title, status, sign_token_hash, requested_at, signed_at,
                     created_by, requested_by, signer_email, signer_name, contract_payload, updated_at, deleted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::timestamptz, $14::timestamptz,
                        $15, $16, $17, $18, $19::jsonb, $20::timestamptz, NULL)
                ON CONFLICT (id) DO UPDATE SET
                    employee_email = EXCLUDED.employee_email,
                    employee_email_masked = EXCLUDED.employee_email_masked,
                    employee_name = EXCLUDED.employee_name,
                    business_id = EXCLUDED.business_id,
                    branch = EXCLUDED.branch,
                    contract_type = EXCLUDED.contract_type,
                    document_kind = EXCLUDED.document_kind,
                    template_version = EXCLUDED.template_version,
                    print_title = EXCLUDED.print_title,
                    status = EXCLUDED.status,
                    sign_token_hash = EXCLUDED.sign_token_hash,
                    requested_at = EXCLUDED.requested_at,
                    signed_at = EXCLUDED.signed_at,
                    created_by = EXCLUDED.created_by,
                    requested_by = EXCLUDED.requested_by,
                    signer_email = EXCLUDED.signer_email,
                    signer_name = EXCLUDED.signer_name,
                    contract_payload = EXCLUDED.contract_payload,
                    updated_at = EXCLUDED.updated_at,
                    deleted_at = NULL
                """,
                record_id,
                str(record.get("employee_email") or "").strip().lower(),
                str(record.get("employee_email_masked") or ""),
                str(record.get("employee_name") or ""),
                str(record.get("business_id") or ""),
                str(record.get("branch") or ""),
                str(record.get("contract_type") or "part_time"),
                str(record.get("document_kind") or "standard_employment_contract"),
                str(record.get("template_version") or ""),
                str(record.get("print_title") or ""),
                str(record.get("status") or "draft"),
                hashlib.sha256(token.encode("utf-8")).hexdigest() if token else token_hash,
                _pg_ts(record.get("requested_at")),
                _pg_ts(record.get("signed_at")),
                str(record.get("created_by") or ""),
                str(record.get("requested_by") or ""),
                str(record.get("signer_email") or ""),
                str(record.get("signer_name") or ""),
                payload,
                _pg_ts(record.get("updated_at") or now),
            )
            return True
        if name == "payroll_statements":
            await conn.execute(
                """
                INSERT INTO yeoljeong_payroll_statements
                    (id, employee_email, employee_email_masked, employee_name, business_id, branch, payroll_month,
                     gross_pay, tax_withholding, insurance_deduction, other_deduction, net_pay, status, created_by,
                     confirmed_by, confirmed_at, statement_payload, updated_at, deleted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16::timestamptz,
                        $17::jsonb, $18::timestamptz, NULL)
                ON CONFLICT (id) DO UPDATE SET
                    employee_email = EXCLUDED.employee_email,
                    employee_email_masked = EXCLUDED.employee_email_masked,
                    employee_name = EXCLUDED.employee_name,
                    business_id = EXCLUDED.business_id,
                    branch = EXCLUDED.branch,
                    payroll_month = EXCLUDED.payroll_month,
                    gross_pay = EXCLUDED.gross_pay,
                    tax_withholding = EXCLUDED.tax_withholding,
                    insurance_deduction = EXCLUDED.insurance_deduction,
                    other_deduction = EXCLUDED.other_deduction,
                    net_pay = EXCLUDED.net_pay,
                    status = EXCLUDED.status,
                    created_by = EXCLUDED.created_by,
                    confirmed_by = EXCLUDED.confirmed_by,
                    confirmed_at = EXCLUDED.confirmed_at,
                    statement_payload = EXCLUDED.statement_payload,
                    updated_at = EXCLUDED.updated_at,
                    deleted_at = NULL
                """,
                record_id,
                str(record.get("employee_email") or "").strip().lower(),
                str(record.get("employee_email_masked") or ""),
                str(record.get("employee_name") or ""),
                str(record.get("business_id") or ""),
                str(record.get("branch") or ""),
                str(record.get("payroll_month") or ""),
                int(record.get("gross_pay") or 0),
                int(record.get("tax_withholding") or 0),
                int(record.get("insurance_deduction") or 0),
                int(record.get("other_deduction") or 0),
                int(record.get("net_pay") or 0),
                str(record.get("status") or "draft"),
                str(record.get("created_by") or ""),
                str(record.get("confirmed_by") or ""),
                _pg_ts(record.get("confirmed_at")),
                payload,
                _pg_ts(record.get("updated_at") or now),
            )
            return True
        return False
    finally:
        await conn.close()


async def _db_delete_ledger(name: str, row_id: str) -> bool:
    import asyncpg

    table = DB_LEDGER_TABLE_BY_NAME.get(name)
    if not table:
        return False
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        ready = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
        if not ready:
            return False
        key = "row_id" if name in GENERIC_DB_LEDGER_NAMES else "id"
        await conn.execute(f"UPDATE {table} SET deleted_at = NOW() WHERE {key} = $1", str(row_id))
        return True
    finally:
        await conn.close()


def _read(name: str) -> list[dict[str, Any]]:
    file_rows = _read_file_rows(name)
    if name not in DB_LEDGER_TABLE_BY_NAME:
        return file_rows
    db_rows = _run_db(_db_fetch_ledger(name))
    if isinstance(db_rows, list):
        if name == "platform_accounts":
            db_rows = _attach_local_account_secrets(db_rows, file_rows)
        if db_rows:
            db_ids = {str(row.get("id") or "") for row in db_rows}
            missing_rows = [row for row in file_rows if str(row.get("id") or "") and str(row.get("id") or "") not in db_ids]
            if missing_rows:
                for row in missing_rows:
                    _run_db(_db_upsert_ledger(name, row))
                merged = _run_db(_db_fetch_ledger(name))
                if isinstance(merged, list) and merged:
                    if name == "platform_accounts":
                        merged = _attach_local_account_secrets(merged, file_rows)
                    return merged
            return db_rows
        if file_rows:
            for row in file_rows:
                _run_db(_db_upsert_ledger(name, row))
            seeded = _run_db(_db_fetch_ledger(name))
            if isinstance(seeded, list) and seeded:
                if name == "platform_accounts":
                    seeded = _attach_local_account_secrets(seeded, file_rows)
                return seeded
    return file_rows


def _write(name: str, rows: list[dict[str, Any]]) -> None:
    _write_file_rows(name, rows)
    if name not in DB_LEDGER_TABLE_BY_NAME:
        return
    for row in rows:
        _run_db(_db_upsert_ledger(name, row))


def _delete(name: str, row_id: str) -> None:
    _write_file_rows(name, [row for row in _read_file_rows(name) if str(row.get("id") or "") != str(row_id)])
    if name in DB_LEDGER_TABLE_BY_NAME:
        _run_db(_db_delete_ledger(name, row_id))


def _merge_by_id(current_items: Any, default_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = {str(item.get("id")): item for item in current_items if isinstance(item, dict)} if isinstance(current_items, list) else {}
    merged: list[dict[str, Any]] = []
    for default_item in default_items:
        item = {**default_item, **current.get(default_item["id"], {})}
        item["id"] = default_item["id"]
        merged.append(item)
    return merged


def _canonicalize_ui_settings(settings: dict[str, Any]) -> dict[str, Any]:
    businesses = _merge_by_id(settings.get("businesses"), CANONICAL_BUSINESSES)
    canonical_names = {item["id"]: item["name"] for item in CANONICAL_BUSINESSES}
    for item in businesses:
        item["entityType"] = item.get("entityType") or "individual"
        item["name"] = canonical_names[item["id"]]

    branches = _merge_by_id(settings.get("branches"), CANONICAL_BRANCHES)
    canonical_branch_names = {item["id"]: item["name"] for item in CANONICAL_BRANCHES}
    canonical_branch_businesses = {item["id"]: item["businessId"] for item in CANONICAL_BRANCHES}
    for item in branches:
        item["name"] = canonical_branch_names[item["id"]]
        item["businessId"] = canonical_branch_businesses[item["id"]]
        item["status"] = item.get("status") or "active"

    def normalize_business_ref(item: dict[str, Any]) -> dict[str, Any]:
        next_item = {**item}
        business_id = str(next_item.get("businessId") or next_item.get("business_id") or "").strip()
        if business_id not in CANONICAL_BUSINESS_IDS:
            business_id = MIA_BUSINESS_ID
        next_item["businessId"] = business_id
        if "business_id" in next_item:
            next_item["business_id"] = business_id
        branch = str(next_item.get("branch") or "").strip()
        if branch and branch not in CANONICAL_BRANCH_NAMES:
            next_item["branch"] = MIA_BRANCH_NAME
        service = str(next_item.get("service") or "").strip()
        if service in CONNECTOR_LABELS:
            next_item["service"] = service
            next_item["label"] = str(next_item.get("label") or CONNECTOR_LABELS[service]).strip()
            next_item["category"] = str(next_item.get("category") or "").strip()
            next_item["collectionMode"] = str(
                next_item.get("collectionMode") or next_item.get("collection_mode") or "browser-automation"
            ).strip()
            next_item["dataScope"] = str(next_item.get("dataScope") or next_item.get("data_scope") or "").strip()
            next_item["requiredProof"] = str(
                next_item.get("requiredProof") or next_item.get("required_proof") or ""
            ).strip()
            next_item["loginUrl"] = str(next_item.get("loginUrl") or next_item.get("login_url") or "").strip()
            next_item["username"] = str(next_item.get("username") or "").strip()
            next_item["businessRegistrationNoMasked"] = str(
                next_item.get("businessRegistrationNoMasked")
                or next_item.get("business_registration_no_masked")
                or ""
            ).strip()
            next_item["status"] = str(next_item.get("status") or "ready").strip()
        return next_item

    return {
        "businesses": businesses,
        "branches": branches,
        "accounts": [normalize_business_ref(item) for item in settings.get("accounts", []) if isinstance(item, dict)],
        "staff": [item for item in settings.get("staff", []) if isinstance(item, dict)],
        "integrations": [normalize_business_ref(item) for item in settings.get("integrations", []) if isinstance(item, dict)],
    }


def _jsonb_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _encrypt_secret(value: str) -> str:
    try:
        from app.core.credential_vault import encrypt_value

        return encrypt_value(value)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="계정 비밀번호 암호화에 실패했습니다") from exc


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        from app.core.credential_vault import decrypt_value

        return decrypt_value(value)
    except Exception:
        return ""


_ACCOUNT_SECRET_FIELD_MAP: dict[str, str] = {
    "password": "password_enc",
    "api_key": "api_key_enc",
    "client_secret": "client_secret_enc",
    "certificate_password": "certificate_password_enc",
    "account_no": "account_no_enc",
    "account_password": "account_password_enc",
    "business_registration_no": "business_registration_no_enc",
}


def _migrate_platform_account_secrets(rows: list[dict[str, Any]]) -> bool:
    changed = False
    for row in rows:
        for plaintext_field, encrypted_field in _ACCOUNT_SECRET_FIELD_MAP.items():
            plaintext = str(row.get(plaintext_field) or "")
            if not plaintext:
                continue
            if not row.get(encrypted_field):
                row[encrypted_field] = _encrypt_secret(plaintext)
            row.pop(plaintext_field, None)
            changed = True
    return changed


def _has_account_secret(row: dict[str, Any]) -> bool:
    return any(bool(row.get(field)) for field in _ACCOUNT_SECRET_FIELDS)


def _masked_digits(value: Any, *, visible_tail: int = 4) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if not digits:
        return ""
    if len(digits) <= visible_tail:
        return "*" * len(digits)
    return f"{'*' * max(3, len(digits) - visible_tail)}{digits[-visible_tail:]}"


def _has_secret_value(row: dict[str, Any], plaintext_field: str) -> bool:
    encrypted_field = _ACCOUNT_SECRET_FIELD_MAP.get(plaintext_field, "")
    return bool(encrypted_field and row.get(encrypted_field))


def _public_platform_account_status(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "ready").strip()
    sync_status = str(row.get("last_sync_status") or row.get("portal_status") or "").strip()
    service = str(row.get("service") or "").strip()
    collection_mode = str(row.get("collection_mode") or row.get("collectionMode") or "").strip()
    has_browser_state = bool(
        str(row.get("storage_state_path") or row.get("browser_storage_state_path") or row.get("baemin_storage_state_path") or "").strip()
    )
    if sync_status == "running":
        started_text = str(row.get("last_sync_at") or row.get("updated_at") or "").strip()
        try:
            started_at = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        except ValueError:
            started_at = None
        if started_at and datetime.now(started_at.tzinfo or KST) - started_at < timedelta(seconds=60):
            return "running"
        if service in PLATFORM_LABELS and collection_mode in DELIVERY_UPLOAD_COLLECTION_MODES and not has_browser_state:
            return "upload_required"
        return "credential_required" if service in PLATFORM_LABELS and not has_browser_state else "blocked"
    if service in PLATFORM_LABELS and not _has_secret_value(row, "password") and not has_browser_state:
        return "credential_required"
    if service in PLATFORM_LABELS and collection_mode in DELIVERY_UPLOAD_COLLECTION_MODES:
        return "upload_required"
    return status


def _missing_connector_requirements(row: dict[str, Any]) -> list[str]:
    service = str(row.get("service") or "").strip()
    collection_mode = str(row.get("collection_mode") or row.get("collectionMode") or "").strip()
    missing: list[str] = []
    if service in PLATFORM_LABELS:
        if not str(row.get("username") or "").strip():
            missing.append("아이디")
        has_browser_state = bool(
            str(row.get("storage_state_path") or row.get("browser_storage_state_path") or row.get("baemin_storage_state_path") or "").strip()
        )
        if not _has_secret_value(row, "password") and not has_browser_state:
            missing.append("비밀번호 또는 PC Agent 로그인 세션")
        if collection_mode in DELIVERY_UPLOAD_COLLECTION_MODES and not has_browser_state:
            missing.append("브라우저 자동화 방식 선택 또는 포털 CSV/엑셀 업로드")
    if service in BANK_QUICK_SERVICE_CONFIG and collection_mode == "bank-quick-service":
        for label, key in (
            ("로그인 비밀번호", "password"),
            ("조회용 계좌번호", "account_no"),
            ("계좌비밀번호", "account_password"),
            ("사업자번호", "business_registration_no"),
        ):
            if not _has_secret_value(row, key):
                missing.append(label)
    return list(dict.fromkeys(missing))


def _mark_platform_account_sync_state(account: dict[str, Any], *, status: str, message: str, synced_at: str) -> None:
    account["last_sync_status"] = status
    account["portal_status"] = status
    account["portal_message"] = message
    account["last_sync_at"] = synced_at
    account["updated_at"] = synced_at
    account_id = str(account.get("id") or "")
    if not account_id:
        return
    rows = _read("platform_accounts")
    for row in rows:
        if str(row.get("id") or "") == account_id:
            row.update(
                {
                    "last_sync_status": status,
                    "portal_status": status,
                    "portal_message": message,
                    "last_sync_at": synced_at,
                    "updated_at": synced_at,
                }
            )
            _write("platform_accounts", rows)
            break


# Fields that must never appear in API responses or logs.
_ACCOUNT_SECRET_FIELDS: frozenset[str] = frozenset(
    set(_ACCOUNT_SECRET_FIELD_MAP) | set(_ACCOUNT_SECRET_FIELD_MAP.values())
)


def _normalize_delivery_scope(business_id: Any, branch: Any) -> tuple[str, str]:
    normalized_branch = BRANCH_ALIASES.get(str(branch or "").strip(), str(branch or "").strip())
    normalized_business = str(business_id or "").strip()
    expected_business = BUSINESS_BY_BRANCH.get(normalized_branch)
    if normalized_business not in CANONICAL_BUSINESS_IDS:
        raise HTTPException(status_code=400, detail="등록되지 않은 사업자입니다")
    if not expected_business or expected_business != normalized_business:
        raise HTTPException(status_code=400, detail="사업자와 지점 연결이 일치하지 않습니다")
    return normalized_business, normalized_branch


def _normalize_connector_scope(service: str, business_id: Any, branch: Any) -> tuple[str, str]:
    if service in PLATFORM_LABELS:
        return _normalize_delivery_scope(business_id, branch)
    normalized_business = str(business_id or "").strip()
    normalized_branch = BRANCH_ALIASES.get(str(branch or "").strip(), str(branch or "").strip())
    if normalized_business not in CANONICAL_BUSINESS_IDS:
        raise HTTPException(status_code=400, detail="등록되지 않은 사업자입니다")
    if normalized_branch:
        expected_business = BUSINESS_BY_BRANCH.get(normalized_branch)
        if not expected_business or expected_business != normalized_business:
            raise HTTPException(status_code=400, detail="사업자와 지점 연결이 일치하지 않습니다")
    return normalized_business, normalized_branch


def _get_pool_or_none() -> Any | None:
    try:
        from app.core.db_pool import get_pool

        return get_pool()
    except Exception:
        return None


async def get_storage_status(user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="저장소 상태 확인 권한이 없습니다")
    ledger_files = sorted(set(JSON_LEDGER_FILES) | GENERIC_DB_LEDGER_NAMES)
    json_ledgers = []
    for name in ledger_files:
        path = _path(name)
        file_rows = _read_file_rows(name) if path.exists() else []
        json_ledgers.append(
            {
                "name": name,
                "path": str(path),
                "exists": path.exists(),
                "records": len(file_rows),
            }
        )

    pool = _get_pool_or_none()
    db_tables = {name: False for name in (*SETTINGS_TABLES, *HR_LEDGER_TABLES, *DELIVERY_LEDGER_TABLES)}
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_schema = 'public'
                       AND table_name = ANY($1::text[])
                    """,
                    list(db_tables),
                )
            existing = {row["table_name"] for row in rows}
            db_tables = {name: name in existing for name in db_tables}
        except Exception:
            pass
    elif _db_available():
        try:
            import asyncpg

            conn = await asyncpg.connect(_db_url(), timeout=5)
            try:
                rows = await conn.fetch(
                    """
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_schema = 'public'
                       AND table_name = ANY($1::text[])
                    """,
                    list(db_tables),
                )
            finally:
                await conn.close()
            existing = {row["table_name"] for row in rows}
            db_tables = {name: name in existing for name in db_tables}
        except Exception:
            pass

    settings_db_ready = all(db_tables[name] for name in SETTINGS_TABLES)
    hr_db_ready = all(db_tables[name] for name in HR_LEDGER_TABLES)
    delivery_db_ready = all(db_tables[name] for name in DELIVERY_LEDGER_TABLES)
    return {
        "checked_at": _now(),
        "mode": "database+json-fallback" if any([settings_db_ready, hr_db_ready, delivery_db_ready]) else "json-only",
        "settings": {
            "source": "database" if settings_db_ready else "json",
            "tables": {name: db_tables[name] for name in SETTINGS_TABLES},
        },
        "hr_ledgers": {
            "source": "database" if hr_db_ready else "json",
            "tables": {name: db_tables[name] for name in HR_LEDGER_TABLES},
            "json_files": json_ledgers,
        },
        "delivery_ledgers": {
            "source": "database" if delivery_db_ready else "json",
            "tables": {name: db_tables[name] for name in DELIVERY_LEDGER_TABLES},
        },
        "migration": {
            "settings": "113_yeoljeong_finance_settings.sql",
            "hr_ledgers": "115_yeoljeong_finance_hr_ledgers.sql",
            "delivery_ledgers": "116_yeoljeong_finance_delivery_ledgers.sql",
            "hr_db_ready": hr_db_ready,
            "delivery_db_ready": delivery_db_ready,
            "note": "각 DB 테이블이 실제 적용되기 전까지 해당 원장은 기존 JSON 파일을 유지합니다.",
        },
    }


def _mask_email(email: str) -> str:
    email = str(email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[:1] + "*"
    else:
        masked = local[:2] + "*" * max(1, len(local) - 2)
    return f"{masked}@{domain}"


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if len(digits) < 7:
        return phone
    return f"{digits[:3]}-****-{digits[-4:]}"


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    name = re.sub(r"[^A-Za-z0-9_.가-힣 -]+", "_", name).strip(" .")
    return name or "upload.bin"


def _find(rows: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((row for row in rows if str(row.get("id")) == str(item_id)), None)


def _is_admin(user: dict[str, Any]) -> bool:
    email = _email(user)
    user_role = str(user.get("user_role") or "").strip().lower()
    privileged_principal = bool(user.get("is_internal_admin")) or user_role in {"ceo", "admin", "system"}
    if email and not privileged_principal:
        employee_record = next(
            (row for row in _read("employee_join_requests") if str(row.get("email") or "").strip().lower() == email),
            None,
        )
        if employee_record:
            return False
    tenant_role = str(user.get("tenant_role") or "").strip().lower()
    membership_role = str((user.get("current_membership") or {}).get("role") or "").strip().lower()
    return bool(
        user.get("is_admin")
        or user.get("is_internal_admin")
        or tenant_role in {"owner", "admin"}
        or membership_role in {"owner", "admin"}
        or user_role in {"ceo", "admin", "system"}
    )


def _email(user: dict[str, Any]) -> str:
    return str(user.get("email") or "").strip().lower()


def _filter_user(rows: list[dict[str, Any]], user: dict[str, Any], *email_keys: str) -> list[dict[str, Any]]:
    if _is_admin(user):
        return rows
    email = _email(user)
    return [row for row in rows if any(str(row.get(key) or "").strip().lower() == email for key in email_keys)]


def _document_meta(document_type: str) -> dict[str, str]:
    return next((item for item in DOCUMENT_TYPES if item["type"] == document_type), DOCUMENT_TYPES[-1])


def _required_document_types() -> list[dict[str, str]]:
    return [item for item in DOCUMENT_TYPES if item.get("requirement") == "필수"]


def list_document_types() -> list[dict[str, str]]:
    return DOCUMENT_TYPES


def session_for_user(user: dict[str, Any]) -> dict[str, Any]:
    email = _email(user)
    joins = _read("employee_join_requests")
    own = next((row for row in joins if str(row.get("email") or "").strip().lower() == email), None)
    admin = _is_admin(user)
    if admin:
        permissions = {
            "role": "owner",
            "role_label": "총괄 운영관리자",
            "can_view": True,
            "can_edit_local_data": True,
            "can_manage_settings": True,
            "can_manage_automation": True,
            "can_import_settlements": True,
            "can_manage_onboarding": True,
            "can_upload_own_documents": True,
        }
    elif own:
        status = str(own.get("status") or "pending")
        permissions = {
            "role": "employee" if status == "approved" else f"employee_{status}",
            "role_label": "직원" if status == "approved" else ("직원 가입 반려" if status == "rejected" else "직원 가입요청"),
            "employee_request_status": status,
            "employee_request_id": own.get("id"),
            "can_view": True,
            "can_edit_local_data": False,
            "can_manage_settings": False,
            "can_manage_automation": False,
            "can_import_settlements": False,
            "can_manage_onboarding": False,
            "can_upload_own_documents": status != "rejected",
        }
    else:
        permissions = {
            "role": "member",
            "role_label": "운영관리자",
            "can_view": True,
            "can_edit_local_data": True,
            "can_manage_settings": False,
            "can_manage_automation": False,
            "can_import_settlements": False,
            "can_manage_onboarding": False,
            "can_upload_own_documents": True,
        }
    return {
        "user": {
            "id": user.get("user_id"),
            "email": email,
            "name": user.get("name") or "",
            "tenant_id": user.get("tenant_id") or "",
            "is_admin": admin,
        },
        "tenant": user.get("current_tenant") or {"id": user.get("tenant_id")},
        "permissions": permissions,
    }


def list_invites(user: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_admin(user):
        return []
    return sorted(_read("employee_invites"), key=lambda row: row.get("created_at", ""), reverse=True)


def create_invite(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="직원 초대 권한이 없습니다")
    rows = _read("employee_invites")
    now = _now()
    token = secrets.token_urlsafe(24)
    invite = {
        "id": str(uuid4()),
        "token": token,
        "phone": str(payload.get("phone") or "").strip(),
        "phone_masked": _mask_phone(str(payload.get("phone") or "")),
        "name": str(payload.get("name") or "").strip(),
        "branch": str(payload.get("branch") or "").strip(),
        "role": str(payload.get("role") or "member"),
        "status": "pending",
        "memo": str(payload.get("memo") or ""),
        "created_by": _email(user),
        "created_at": now,
        "expires_at": (datetime.now(KST) + timedelta(hours=int(payload.get("expires_in_hours") or 72))).isoformat(timespec="seconds"),
    }
    rows.insert(0, invite)
    _write("employee_invites", rows)
    return invite


def resolve_invite(token: str) -> dict[str, Any]:
    invite = next((row for row in _read("employee_invites") if row.get("token") == token), None)
    if not invite:
        raise HTTPException(status_code=404, detail="초대를 찾을 수 없습니다")
    return invite


def accept_invite(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    invite = resolve_invite(str(payload.get("token") or ""))
    rows = _read("employee_invites")
    target = _find(rows, invite["id"])
    if target:
        target["status"] = "accepted"
        target["accepted_at"] = _now()
        target["accepted_email"] = _email(user)
        _write("employee_invites", rows)
    request = upsert_join_request(
        {
            "name": payload.get("name") or invite.get("name") or _email(user),
            "email": _email(user),
            "branch": payload.get("branch") or invite.get("branch") or "",
            "phone": payload.get("phone") or invite.get("phone") or "",
            "memo": payload.get("memo") or "전화번호 초대 링크로 회원가입",
            "invite_id": invite.get("id"),
        },
        user,
    )
    return request


def list_join_requests(user: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _read("employee_join_requests")
    return sorted(_filter_user(rows, user, "email"), key=lambda row: row.get("requested_at", ""), reverse=True)


def upsert_join_request(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    rows = _read("employee_join_requests")
    email = str(payload.get("email") or _email(user)).strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="직원 이메일이 필요합니다")
    existing = next((row for row in rows if str(row.get("email") or "").strip().lower() == email), None)
    now = _now()
    record = existing or {"id": str(uuid4()), "requested_at": now}
    branch = BRANCH_ALIASES.get(
        str(payload.get("branch") or record.get("branch") or "").strip(),
        str(payload.get("branch") or record.get("branch") or "").strip(),
    )
    business_id = str(payload.get("business_id") or record.get("business_id") or BUSINESS_BY_BRANCH.get(branch) or "").strip()
    if branch and (business_id not in CANONICAL_BUSINESS_IDS or BUSINESS_BY_BRANCH.get(branch) != business_id):
        raise HTTPException(status_code=400, detail="직원의 사업자와 지점 연결이 일치하지 않습니다")
    record.update(
        {
            "name": str(payload.get("name") or record.get("name") or "").strip(),
            "email": email,
            "email_masked": _mask_email(email),
            "phone": str(payload.get("phone") or record.get("phone") or "").strip(),
            "phone_masked": _mask_phone(str(payload.get("phone") or record.get("phone") or "")),
            "address": str(payload.get("address") or record.get("address") or "").strip(),
            "birth_date": str(payload.get("birth_date") or record.get("birth_date") or "").strip(),
            "nationality": str(payload.get("nationality") or record.get("nationality") or "대한민국").strip(),
            "business_id": business_id,
            "branch": branch,
            "memo": str(payload.get("memo") or record.get("memo") or ""),
            "invite_id": payload.get("invite_id") or record.get("invite_id") or "",
            "status": record.get("status") if existing else "pending",
            "updated_at": now,
        }
    )
    if not existing:
        rows.insert(0, record)
    _write("employee_join_requests", rows)
    return record


def review_join_request(request_id: str, action: str, memo: str, user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="가입요청 승인 권한이 없습니다")
    rows = _read("employee_join_requests")
    record = _find(rows, request_id)
    if not record:
        raise HTTPException(status_code=404, detail="가입요청을 찾을 수 없습니다")
    if action not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="action은 approved 또는 rejected여야 합니다")
    record["status"] = action
    record["review_memo"] = memo
    record["reviewed_by"] = _email(user)
    record["reviewed_at"] = _now()
    record["updated_at"] = record["reviewed_at"]
    _write("employee_join_requests", rows)
    return record


def _record_business_id(record: dict[str, Any]) -> str:
    branch = BRANCH_ALIASES.get(str(record.get("branch") or "").strip(), str(record.get("branch") or "").strip())
    explicit = str(record.get("business_id") or record.get("businessId") or "").strip()
    return explicit if explicit in CANONICAL_BUSINESS_IDS else str(BUSINESS_BY_BRANCH.get(branch) or "")


ONBOARDING_PROFILE_FIELDS = (
    "address",
    "birth_date",
    "nationality",
    "bank_name",
    "bank_account_holder",
    "bank_account_masked",
    "health_certificate_issue_date",
    "health_certificate_valid_until",
)


def _employee_onboarding_profile(
    documents: list[dict[str, Any]],
    *,
    employee_email: str,
    employee_request_id: str,
) -> dict[str, Any]:
    """Return privacy-minimised fields verified from an employee's uploaded documents."""
    email = str(employee_email or "").strip().lower()
    request_id = str(employee_request_id or "").strip()
    matched = [
        row
        for row in documents
        if str(row.get("status") or "").strip().lower() != "missing"
        and (
            (request_id and str(row.get("employee_request_id") or "").strip() == request_id)
            or (email and str(row.get("employee_email") or "").strip().lower() == email)
        )
    ]
    profile: dict[str, Any] = {}
    summaries: list[dict[str, str]] = []
    for row in matched:
        extracted = row.get("extracted_fields")
        if not isinstance(extracted, dict):
            extracted = {}
        for field in ONBOARDING_PROFILE_FIELDS:
            value = extracted.get(field)
            if value and not profile.get(field):
                profile[field] = value
        summaries.append(
            {
                "document_type": str(row.get("document_type") or ""),
                "document_label": str(row.get("document_label") or ""),
                "status": str(row.get("status") or "uploaded"),
                "issue_date": str(row.get("issue_date") or ""),
                "valid_until": str(
                    extracted.get("health_certificate_valid_until")
                    or row.get("valid_until")
                    or ""
                ),
            }
        )
    summaries.sort(key=lambda item: (item["document_label"], item["issue_date"]))
    profile["onboarding_documents"] = summaries
    summary_items: list[str] = []
    for item in summaries:
        valid_until = f"~{item['valid_until']}" if item["valid_until"] else ""
        summary_items.append(
            f"{item['document_label'] or item['document_type']}"
            f"({item['issue_date'] or '발급일 미입력'}{valid_until}, {item['status']})"
        )
    profile["onboarding_document_summary"] = ", ".join(summary_items)
    return profile


def list_approved_employees(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        return []
    docs = _read("onboarding_documents")
    contracts = _read("contracts")
    payroll = _read("payroll_statements")

    result = []
    for row in _read("employee_join_requests"):
        if row.get("status") != "approved":
            continue
        employee_business_id = _record_business_id(row)
        if business_id and employee_business_id != business_id:
            continue
        email = str(row.get("email") or "").strip().lower()
        employee = dict(row)
        employee["business_id"] = employee_business_id
        employee["branch"] = BRANCH_ALIASES.get(str(employee.get("branch") or ""), str(employee.get("branch") or ""))
        employee["email_masked"] = employee.get("email_masked") or _mask_email(email)
        document_profile = _employee_onboarding_profile(
            docs,
            employee_email=email,
            employee_request_id=str(employee.get("id") or ""),
        )
        for field in ONBOARDING_PROFILE_FIELDS:
            if not employee.get(field) and document_profile.get(field):
                employee[field] = document_profile[field]
        employee["onboarding_documents"] = document_profile["onboarding_documents"]
        employee["onboarding_document_summary"] = document_profile["onboarding_document_summary"]
        employee["onboarding_document_count"] = sum(1 for item in docs if str(item.get("employee_email") or "").strip().lower() == email)
        employee["contract_count"] = sum(1 for item in contracts if str(item.get("employee_email") or "").strip().lower() == email)
        employee["payroll_statement_count"] = sum(1 for item in payroll if str(item.get("employee_email") or "").strip().lower() == email)
        employee["needs_onboarding_documents"] = employee["onboarding_document_count"] == 0
        employee["needs_contract"] = employee["contract_count"] == 0
        employee["needs_payroll"] = employee["payroll_statement_count"] == 0
        result.append(employee)
    return sorted(result, key=lambda row: row.get("reviewed_at") or row.get("updated_at") or row.get("requested_at") or "", reverse=True)


async def save_onboarding_document(
    *,
    employee_name: str,
    employee_email: str,
    branch: str,
    document_type: str,
    issue_date: str,
    memo: str,
    upload: UploadFile,
    user: dict[str, Any],
) -> dict[str, Any]:
    email = str(employee_email or _email(user)).strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="직원 이메일이 필요합니다")
    if not _is_admin(user) and email != _email(user):
        raise HTTPException(status_code=403, detail="본인 서류만 업로드할 수 있습니다")
    employee = next(
        (
            row
            for row in _read("employee_join_requests")
            if str(row.get("email") or "").strip().lower() == email
            and str(row.get("status") or "pending").strip().lower() != "rejected"
        ),
        None,
    )
    employee_branch = str((employee or {}).get("branch") or "").strip()
    normalized_branch = BRANCH_ALIASES.get(employee_branch or branch.strip(), employee_branch or branch.strip())
    business_id = _record_business_id(employee or {"branch": normalized_branch})
    meta = _document_meta(document_type)
    original = _safe_filename(upload.filename or "document.bin")
    suffix = Path(original).suffix.lower()
    doc_id = str(uuid4())
    stored_name = f"{doc_id}{suffix or '.bin'}"
    destination = UPLOAD_DIR / stored_name
    _ensure_dirs()
    size = 0
    with destination.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="파일은 최대 15MB까지 업로드할 수 있습니다")
            out.write(chunk)
    now = _now()
    record = {
        "id": doc_id,
        "employee_request_id": str((employee or {}).get("id") or ""),
        "employee_name": employee_name.strip() or str((employee or {}).get("name") or ""),
        "employee_email": email,
        "employee_email_masked": _mask_email(email),
        "business_id": business_id,
        "branch": normalized_branch,
        "document_type": document_type,
        "document_label": meta["label"],
        "requirement": meta["requirement"],
        "issue_date": issue_date,
        "memo": memo,
        "original_filename": original,
        "stored_filename": stored_name,
        "content_type": upload.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream",
        "size_bytes": size,
        "status": "uploaded",
        "uploaded_by": _email(user),
        "uploaded_at": now,
        "updated_at": now,
    }
    rows = _read("onboarding_documents")
    rows.insert(0, record)
    _write("onboarding_documents", rows)
    return record


def _evidence_kind_label(kind: str) -> str:
    return {
        "bank_statement": "은행 거래내역",
        "supplier_statement": "매입처 거래내역서",
        "receipt_photo": "영수증 사진",
        "tax_invoice": "세금계산서",
        "utility_bill": "공과금 고지서",
        "card_pg_report": "카드/PG 리포트",
        "other": "기타 증빙",
    }.get(kind, kind or "기타 증빙")


def list_integration_evidence(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="연동 증빙 조회 권한이 없습니다")
    rows = _read("integration_evidence")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return sorted(rows, key=lambda row: str(row.get("uploaded_at") or row.get("created_at") or ""), reverse=True)


async def save_integration_evidence(
    *,
    service: str,
    business_id: str,
    branch: str,
    document_kind: str,
    vendor: str,
    amount: int,
    memo: str,
    upload: UploadFile,
    user: dict[str, Any],
) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="연동 증빙 업로드 권한이 없습니다")
    normalized_service = str(service or "").strip()
    if normalized_service not in CONNECTOR_LABELS:
        raise HTTPException(status_code=400, detail="지원하지 않는 연동 서비스입니다")
    normalized_business, normalized_branch = _normalize_connector_scope(normalized_service, business_id, branch)
    original = _safe_filename(upload.filename or "evidence.bin")
    suffix = Path(original).suffix.lower()
    evidence_id = str(uuid4())
    stored_name = f"{evidence_id}{suffix or '.bin'}"
    destination = _evidence_upload_dir() / stored_name
    _ensure_dirs()
    size = 0
    with destination.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="파일은 최대 15MB까지 업로드할 수 있습니다")
            out.write(chunk)
    now = _now()
    content_type = upload.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream"
    record = {
        "id": evidence_id,
        "service": normalized_service,
        "service_label": CONNECTOR_LABELS.get(normalized_service, normalized_service),
        "business_id": normalized_business,
        "branch": normalized_branch,
        "document_kind": document_kind or "other",
        "document_label": _evidence_kind_label(document_kind or "other"),
        "vendor": vendor.strip() or CONNECTOR_LABELS.get(normalized_service, normalized_service),
        "amount": int(amount or 0),
        "memo": memo,
        "original_filename": original,
        "stored_filename": stored_name,
        "content_type": content_type,
        "size_bytes": size,
        "status": "pending_review",
        "uploaded_by": _email(user),
        "uploaded_at": now,
        "created_at": now,
        "updated_at": now,
    }
    evidence_rows = _read("integration_evidence")
    evidence_rows.insert(0, record)
    _write("integration_evidence", evidence_rows)

    transaction = create_transaction(
        {
            "transaction_date": now[:10],
            "source_type": "integration_evidence",
            "source_file": original,
            "description": f"{record['service_label']} {record['document_label']} 업로드",
            "amount": record["amount"],
            "direction": "expense" if normalized_service not in PLATFORM_LABELS else "income",
            "category": _transaction_category(" ".join([record["service_label"], record["document_label"], record["vendor"], memo])),
            "approval_number": "",
            "order_number": "",
            "account_name": record["vendor"],
            "business_id": normalized_business,
            "branch": normalized_branch,
            "evidence_id": evidence_id,
            "status": "pending",
            "memo": memo or "업로드 증빙 확인 필요",
        }
    )
    return {"evidence": record, "transaction": transaction}


def get_integration_evidence(evidence_id: str, user: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="연동 증빙 다운로드 권한이 없습니다")
    document = _find(_read("integration_evidence"), evidence_id)
    if not document:
        raise HTTPException(status_code=404, detail="연동 증빙을 찾을 수 없습니다")
    stored = _safe_filename(str(document.get("stored_filename") or ""))
    path = _evidence_upload_dir() / stored
    if not path.exists():
        raise HTTPException(status_code=404, detail="연동 증빙 파일이 없습니다")
    return document, path


def _onboarding_missing_document_rows(
    *,
    existing_rows: list[dict[str, Any]],
    user: dict[str, Any],
    business_id: str | None = None,
) -> list[dict[str, Any]]:
    email_filter = "" if _is_admin(user) else _email(user)
    existing_keys = {
        (
            str(row.get("employee_email") or "").strip().lower(),
            str(row.get("document_type") or "").strip(),
        )
        for row in existing_rows
    }
    rows: list[dict[str, Any]] = []
    admin_view = _is_admin(user)
    for employee in _read("employee_join_requests"):
        status = str(employee.get("status") or "pending").strip().lower()
        if status == "rejected":
            continue
        if admin_view and status != "approved":
            continue
        employee_email = str(employee.get("email") or "").strip().lower()
        if not employee_email:
            continue
        if email_filter and employee_email != email_filter:
            continue
        employee_business_id = _record_business_id(employee)
        if business_id and employee_business_id != business_id:
            continue
        employee_id = str(employee.get("id") or employee_email)
        for meta in _required_document_types():
            document_type = meta["type"]
            if (employee_email, document_type) in existing_keys:
                continue
            rows.append(
                {
                    "id": f"missing-{employee_id}-{document_type}",
                    "employee_request_id": employee_id,
                    "employee_name": employee.get("name") or "",
                    "employee_email": employee_email,
                    "employee_email_masked": employee.get("email_masked") or _mask_email(employee_email),
                    "business_id": employee_business_id,
                    "branch": BRANCH_ALIASES.get(str(employee.get("branch") or ""), str(employee.get("branch") or "")),
                    "document_type": document_type,
                    "document_label": meta["label"],
                    "requirement": meta["requirement"],
                    "status": "missing",
                    "status_label": "작성 필요",
                    "original_filename": "",
                    "size_bytes": 0,
                    "uploaded_at": "",
                    "updated_at": employee.get("reviewed_at") or employee.get("updated_at") or employee.get("requested_at") or "",
                    "is_placeholder": True,
                    "missing_document": True,
                    "employee_request_status": status,
                }
            )
    return rows


def list_onboarding_documents(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    stored_rows = _read("onboarding_documents")
    visible_rows = _filter_user(stored_rows, user, "employee_email", "uploaded_by")
    normalized_rows = []
    for item in visible_rows:
        row = dict(item)
        row["business_id"] = _record_business_id(row)
        row["branch"] = BRANCH_ALIASES.get(str(row.get("branch") or ""), str(row.get("branch") or ""))
        if business_id and _is_admin(user) and row["business_id"] != business_id:
            continue
        normalized_rows.append(row)
    rows = normalized_rows + _onboarding_missing_document_rows(
        existing_rows=stored_rows,
        user=user,
        business_id=business_id if _is_admin(user) else None,
    )
    return sorted(rows, key=lambda row: row.get("uploaded_at") or row.get("updated_at") or "", reverse=True)


def get_onboarding_document(document_id: str, user: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    record = _find(_read("onboarding_documents"), document_id)
    if not record:
        raise HTTPException(status_code=404, detail="입사서류를 찾을 수 없습니다")
    if not _is_admin(user) and str(record.get("employee_email") or "").strip().lower() != _email(user):
        raise HTTPException(status_code=403, detail="본인 서류만 열람할 수 있습니다")
    path = UPLOAD_DIR / str(record.get("stored_filename") or "")
    if not path.exists():
        raise HTTPException(status_code=404, detail="업로드 파일이 없습니다")
    return record, path


def review_onboarding_document(document_id: str, status: str, memo: str, user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="서류 검수 권한이 없습니다")
    rows = _read("onboarding_documents")
    record = _find(rows, document_id)
    if not record:
        raise HTTPException(status_code=404, detail="입사서류를 찾을 수 없습니다")
    if status not in {"approved", "rejected", "needs_fix", "uploaded"}:
        raise HTTPException(status_code=400, detail="올바르지 않은 서류 상태입니다")
    record["status"] = status
    record["review_memo"] = memo
    record["reviewed_by"] = _email(user)
    record["reviewed_at"] = _now()
    record["updated_at"] = record["reviewed_at"]
    _write("onboarding_documents", rows)
    return record


def delete_onboarding_document(document_id: str, user: dict[str, Any]) -> None:
    rows = _read("onboarding_documents")
    record = _find(rows, document_id)
    if not record:
        raise HTTPException(status_code=404, detail="입사서류를 찾을 수 없습니다")
    if not _is_admin(user) and str(record.get("employee_email") or "").strip().lower() != _email(user):
        raise HTTPException(status_code=403, detail="본인 서류만 삭제할 수 있습니다")
    _delete("onboarding_documents", document_id)


def _contract_business(payload: dict[str, Any], employee: dict[str, Any] | None) -> tuple[str, str]:
    employee = employee or {}
    employee_branch = BRANCH_ALIASES.get(str(employee.get("branch") or "").strip(), str(employee.get("branch") or "").strip())
    employee_business_id = _record_business_id(employee)
    branch = BRANCH_ALIASES.get(
        str(payload.get("branch") or employee_branch or "").strip(),
        str(payload.get("branch") or employee_branch or "").strip(),
    )
    business_id = str(payload.get("business_id") or payload.get("businessId") or BUSINESS_BY_BRANCH.get(branch) or employee_business_id).strip()
    if branch and (business_id not in CANONICAL_BUSINESS_IDS or BUSINESS_BY_BRANCH.get(branch) != business_id):
        raise HTTPException(status_code=400, detail="계약서의 사업자와 지점 연결이 일치하지 않습니다")
    if employee and employee_business_id != business_id:
        raise HTTPException(status_code=400, detail="선택 직원은 해당 사업자 소속이 아닙니다")
    return business_id, branch


def _fill_contract_reference_data(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    request_id = str(result.get("employee_request_id") or result.get("employeeRequestId") or "").strip()
    employee = None
    if request_id:
        employee = _find(_read("employee_join_requests"), request_id)
        if not employee or str(employee.get("status") or "").lower() != "approved":
            raise HTTPException(status_code=400, detail="승인된 가입 직원만 계약서에 선택할 수 있습니다")

    business_id, branch = _contract_business(result, employee)
    result["business_id"] = business_id
    result["branch"] = branch
    if employee:
        document_profile = _employee_onboarding_profile(
            _read("onboarding_documents"),
            employee_email=str(employee.get("email") or ""),
            employee_request_id=request_id,
        )
        employee_defaults = {
            "employee_request_id": request_id,
            "employee_name": employee.get("name") or "",
            "employee_email": employee.get("email") or "",
            "employee_address": employee.get("address") or document_profile.get("address") or "",
            "employee_phone": employee.get("phone") or "",
            "employee_birth_date": employee.get("birth_date") or document_profile.get("birth_date") or "",
            "employee_nationality": employee.get("nationality") or document_profile.get("nationality") or "대한민국",
            "bank_name": document_profile.get("bank_name") or "",
            "bank_account_holder": document_profile.get("bank_account_holder") or "",
            "bank_account_masked": document_profile.get("bank_account_masked") or "",
            "health_certificate_issue_date": document_profile.get("health_certificate_issue_date") or "",
            "health_certificate_valid_until": document_profile.get("health_certificate_valid_until") or "",
            "onboarding_document_summary": document_profile.get("onboarding_document_summary") or "",
        }
        for key, value in employee_defaults.items():
            if not str(result.get(key) or "").strip() and value:
                result[key] = value

    settings = get_settings(user).get("settings") or {}
    businesses = settings.get("businesses") if isinstance(settings.get("businesses"), list) else []
    business = next((item for item in businesses if item.get("id") == business_id), None)
    if not business:
        business = next((item for item in CANONICAL_BUSINESSES if item.get("id") == business_id), {})
    business_defaults = {
        "employer_name": business.get("name") or "",
        "employer_registration_no": business.get("registrationNo") or "",
        "employer_representative": business.get("representative") or "",
        "employer_address": business.get("address") or "",
        "employer_phone": business.get("phone") or "",
        "workplace": branch,
    }
    for key, value in business_defaults.items():
        if not str(result.get(key) or "").strip() and value:
            result[key] = value
    return result


def _contract_payload_value(payload: dict[str, Any], snake_key: str, camel_key: str = "") -> Any:
    value = payload.get(snake_key)
    if value is None and camel_key:
        value = payload.get(camel_key)
    return value


def _missing_contract_value(value: Any) -> bool:
    return str(value or "").strip() in {"", "-", "미등록", "기초등록 필요"}


def _validate_contract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate legal/operational invariants before a contract is persisted."""
    result = dict(payload)
    contract_type = str(_contract_payload_value(result, "contract_type", "contractType") or "").strip()
    if contract_type not in VALID_CONTRACT_TYPES:
        raise HTTPException(status_code=400, detail="지원하지 않는 계약 유형입니다")

    employee_name = str(_contract_payload_value(result, "employee_name", "employeeName") or "").strip()
    employee_email = str(_contract_payload_value(result, "employee_email", "employeeEmail") or "").strip().lower()
    employee_request_id = str(_contract_payload_value(result, "employee_request_id", "employeeRequestId") or "").strip()
    business_id = str(_contract_payload_value(result, "business_id", "businessId") or "").strip()
    branch = str(result.get("branch") or "").strip()
    contract_date = str(_contract_payload_value(result, "contract_date", "contractDate") or "").strip()
    employee_address = str(_contract_payload_value(result, "employee_address", "employeeAddress") or "").strip()
    employee_phone = str(_contract_payload_value(result, "employee_phone", "employeePhone") or "").strip()
    employee_birth_date = str(_contract_payload_value(result, "employee_birth_date", "employeeBirthDate") or "").strip()

    missing: list[str] = []
    for label, value in (
        ("승인 직원", employee_request_id),
        ("직원명", employee_name),
        ("직원 이메일", employee_email),
        ("근로자 주소", employee_address),
        ("근로자 연락처", employee_phone),
        ("근로자 생년월일", employee_birth_date),
        ("사업자", business_id),
        ("근무 지점", branch),
        ("계약 작성일", contract_date),
        ("사용자 상호", _contract_payload_value(result, "employer_name", "employerName")),
        ("사업자등록번호", _contract_payload_value(result, "employer_registration_no", "employerRegistrationNo")),
        ("대표자", _contract_payload_value(result, "employer_representative", "employerRepresentative")),
        ("사용자 주소", _contract_payload_value(result, "employer_address", "employerAddress")),
    ):
        if _missing_contract_value(value):
            missing.append(label)

    if employee_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", employee_email):
        raise HTTPException(status_code=400, detail="직원 이메일 형식이 올바르지 않습니다")
    if employee_phone and len(re.sub(r"\D", "", employee_phone)) < 9:
        raise HTTPException(status_code=400, detail="근로자 연락처 형식이 올바르지 않습니다")
    employer_registration_no = str(
        _contract_payload_value(result, "employer_registration_no", "employerRegistrationNo") or ""
    ).strip()
    if not _missing_contract_value(employer_registration_no) and not re.fullmatch(
        r"\d{3}-?\d{2}-?\d{5}", employer_registration_no
    ):
        raise HTTPException(status_code=400, detail="사업자등록번호 형식이 올바르지 않습니다")

    start_date = str(_contract_payload_value(result, "start_date", "startDate") or "").strip()
    end_date = str(_contract_payload_value(result, "end_date", "endDate") or "").strip()
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="계약 종료일은 입사일보다 빠를 수 없습니다")

    birth_date_value: date | None = None
    if employee_birth_date:
        try:
            birth_date_value = date.fromisoformat(employee_birth_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="근로자 생년월일 형식이 올바르지 않습니다") from exc
        reference_text = start_date or contract_date
        try:
            reference_date = date.fromisoformat(reference_text)
        except ValueError:
            reference_date = datetime.now(KST).date()
        if birth_date_value > reference_date:
            raise HTTPException(status_code=400, detail="근로자 생년월일은 계약일보다 늦을 수 없습니다")
        age = reference_date.year - birth_date_value.year - (
            (reference_date.month, reference_date.day) < (birth_date_value.month, birth_date_value.day)
        )
        if age < 18:
            for label, key, camel in (
                ("친권자/후견인 성명", "minor_guardian_name", "minorGuardianName"),
                ("친권자/후견인 연락처", "minor_guardian_phone", "minorGuardianPhone"),
            ):
                if _missing_contract_value(_contract_payload_value(result, key, camel)):
                    missing.append(label)
            consent = str(
                _contract_payload_value(result, "minor_guardian_consent", "minorGuardianConsent") or ""
            ).strip()
            if consent != "confirmed":
                missing.append("친권자/후견인 동의서 확인")

    employment_tax_type = str(
        _contract_payload_value(result, "employment_tax_type", "employmentTaxType") or ""
    ).strip()
    wage_type = str(_contract_payload_value(result, "wage_type", "wageType") or "").strip()
    try:
        wage = float(result.get("wage") or 0)
    except (TypeError, ValueError):
        wage = 0

    if contract_type in EMPLOYMENT_CONTRACT_TYPES:
        if employment_tax_type != "four_insurance":
            raise HTTPException(status_code=400, detail="근로계약서는 4대보험 가입 근로자 구분으로 작성해야 합니다")
        for label, key, camel in (
            ("입사일", "start_date", "startDate"),
            ("근무장소", "workplace", "workplace"),
            ("업무내용", "job_description", "jobDescription"),
            ("근무시간", "work_time", "workTime"),
            ("휴게시간", "rest_time", "restTime"),
            ("주 소정근로시간", "weekly_hours", "weeklyHours"),
            ("근무일/요일", "work_days", "workDays"),
            ("휴일/주휴", "holidays", "holidays"),
            ("급여지급일", "pay_date", "payDate"),
            ("지급방법", "pay_method", "payMethod"),
            ("임금 구성/공제", "wage_composition", "wageComposition"),
            ("연장·야간·휴일근로", "overtime_terms", "overtimeTerms"),
            ("연차/휴가/결근", "leave_terms", "leaveTerms"),
            ("4대보험/세무 처리", "insurance_terms", "insuranceTerms"),
        ):
            if _missing_contract_value(_contract_payload_value(result, key, camel)):
                missing.append(label)
        if contract_type == "part_time" and _missing_contract_value(
            _contract_payload_value(result, "daily_work_schedule", "dailyWorkSchedule")
        ):
            missing.append("근로일별 근로시간")
        if wage_type not in {"hourly", "monthly", "daily"}:
            raise HTTPException(status_code=400, detail="근로계약서의 임금 산정 방식을 확인하십시오")
        if wage <= 0:
            missing.append("확정 임금")
        component_values = []
        for snake, camel in (
            ("base_salary", "baseSalary"),
            ("non_tax_meal_allowance", "nonTaxMealAllowance"),
            ("taxable_allowance", "taxableAllowance"),
        ):
            raw_value = _contract_payload_value(result, snake, camel)
            try:
                component_values.append(float(raw_value or 0))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="급여 구성 금액을 숫자로 입력하십시오")
        base_salary, non_tax_meal, taxable_allowance = component_values
        component_total = base_salary + non_tax_meal + taxable_allowance
        if component_total and abs(component_total - wage) >= 1:
            raise HTTPException(status_code=400, detail="기본급·비과세 식대·기타 과세수당 합계가 월 총액과 일치해야 합니다")
        if non_tax_meal < 0 or non_tax_meal > 200000:
            raise HTTPException(status_code=400, detail="비과세 식대는 월 200,000원 이내로 입력하십시오")
        meal_provision = str(_contract_payload_value(result, "meal_provision", "mealProvision") or "").strip()
        if non_tax_meal > 0 and meal_provision != "cash_no_meal":
            raise HTTPException(status_code=400, detail="사용자가 식사를 제공하는 경우 현금 식대를 비과세로 분류할 수 없습니다")
        workplace_size = str(
            _contract_payload_value(result, "workplace_size_category", "workplaceSizeCategory") or ""
        ).strip()
        weekly_hours_text = str(_contract_payload_value(result, "weekly_hours", "weeklyHours") or "")
        weekly_match = re.search(r"주\s*(\d+)시간(?:\s*(\d+)분)?", weekly_hours_text)
        contract_date_year = contract_date[:4]
        if (
            contract_type == "regular"
            and workplace_size == "under_5"
            and weekly_match
            and base_salary > 0
            and contract_date_year == "2026"
        ):
            weekly_hours = float(weekly_match.group(1)) + float(weekly_match.group(2) or 0) / 60
            monthly_paid_hours = (weekly_hours + min(8, weekly_hours / 5)) * 365 / 7 / 12
            conservative_hourly = base_salary / monthly_paid_hours
            if conservative_hourly < 10320:
                raise HTTPException(
                    status_code=400,
                    detail=f"과세 기본급 기준 환산시급 {int(conservative_hourly):,}원은 2026년 최저임금 10,320원보다 낮습니다",
                )
        foreign_worker = str(
            _contract_payload_value(result, "foreign_worker", "foreignWorker") or ""
        ).strip().lower() in {"true", "1", "yes"}
        if foreign_worker:
            for label, key, camel in (
                ("국적", "employee_nationality", "employeeNationality"),
                ("체류자격", "visa_status", "visaStatus"),
                ("외국인등록번호(마스킹)", "foreign_registration_no_masked", "foreignRegistrationNoMasked"),
            ):
                if _missing_contract_value(_contract_payload_value(result, key, camel)):
                    missing.append(label)
    elif contract_type == "freelancer":
        if employment_tax_type != "freelancer_33":
            raise HTTPException(status_code=400, detail="프리랜서 용역계약서는 3.3% 원천징수 구분으로 작성해야 합니다")
        if wage_type != "case_fee":
            raise HTTPException(status_code=400, detail="프리랜서 용역계약서는 건별/용역비 방식으로 작성해야 합니다")
        for label, key, camel in (
            ("용역 시작일", "start_date", "startDate"),
            ("용역 업무범위/산출물", "freelancer_scope", "freelancerScope"),
            ("용역비 정산/해지", "freelancer_settlement_terms", "freelancerSettlementTerms"),
        ):
            if _missing_contract_value(_contract_payload_value(result, key, camel)):
                missing.append(label)
        if wage <= 0:
            missing.append("확정 용역비")

    if missing:
        unique_missing = list(dict.fromkeys(missing))
        raise HTTPException(status_code=400, detail=f"계약서 필수 입력값을 확인하십시오: {', '.join(unique_missing)}")
    return result


def _signed_contract_snapshot(contract: dict[str, Any]) -> tuple[dict[str, Any], str]:
    snapshot = {key: value for key, value in contract.items() if key not in CONTRACT_SNAPSHOT_EXCLUDED_FIELDS}
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return snapshot, hashlib.sha256(encoded).hexdigest()


def _contract_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    contract_id = str(payload.get("id") or uuid4())
    employee_email = str(payload.get("employee_email") or payload.get("employeeEmail") or "").strip().lower()
    contract_type = str(payload.get("contract_type") or payload.get("contractType") or "part_time")
    meta = CONTRACT_TEMPLATE_META.get(contract_type, CONTRACT_TEMPLATE_META["default"])
    contract = {
        **payload,
        "id": contract_id,
        "contract_type": contract_type,
        "document_kind": str(payload.get("document_kind") or payload.get("documentKind") or meta["document_kind"]),
        "template_version": str(payload.get("template_version") or payload.get("templateVersion") or meta["template_version"]),
        "print_title": str(payload.get("print_title") or payload.get("printTitle") or meta["print_title"]),
        "employee_email": employee_email,
        "employee_email_masked": _mask_email(employee_email),
        "employee_name": str(payload.get("employee_name") or payload.get("employeeName") or "").strip(),
        "status": str(payload.get("status") or "draft"),
        "created_at": payload.get("created_at") or now,
        "updated_at": now,
    }
    return contract


def list_contracts(user: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(_filter_user(_read("contracts"), user, "employee_email"), key=lambda row: row.get("updated_at", ""), reverse=True)


def save_contract(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="계약서 작성 권한이 없습니다")
    rows = _read("contracts")
    requested_id = str(payload.get("id") or "").strip()
    existing = _find(rows, requested_id) if requested_id else None
    if existing and str(existing.get("status") or "") == "signed":
        raise HTTPException(status_code=409, detail="서명 완료 계약서는 수정할 수 없습니다. 정정 계약서를 새로 작성하십시오")
    contract = _contract_defaults(_validate_contract_payload(_fill_contract_reference_data(payload, user)))
    existing = _find(rows, contract["id"])
    if existing:
        if str(existing.get("status") or "") == "requested":
            contract["status"] = "draft"
            contract.pop("sign_token", None)
            contract.pop("requested_at", None)
        existing.update(contract)
        if existing.get("status") == "draft":
            existing.pop("sign_token", None)
            existing.pop("requested_at", None)
        saved = existing
    else:
        contract["status"] = "draft"
        rows.insert(0, contract)
        saved = contract
    _write("contracts", rows)
    return saved


def request_contract_signature(contract_id: str, user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="서명 요청 권한이 없습니다")
    rows = _read("contracts")
    contract = _find(rows, contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="계약서를 찾을 수 없습니다")
    if str(contract.get("status") or "") == "signed":
        raise HTTPException(status_code=409, detail="서명 완료 계약서는 다시 서명 요청할 수 없습니다")
    _validate_contract_payload(contract)
    contract["status"] = "requested"
    contract["sign_token"] = contract.get("sign_token") or secrets.token_urlsafe(24)
    contract["requested_at"] = _now()
    contract["updated_at"] = contract["requested_at"]
    _write("contracts", rows)
    return contract


def _contract_signer_email(contract: dict[str, Any], user: dict[str, Any] | None) -> str:
    if not user or not _email(user):
        raise HTTPException(status_code=401, detail="직원 계정 로그인이 필요합니다")
    if _is_admin(user):
        raise HTTPException(status_code=403, detail="관리자는 직원 대신 계약서에 서명할 수 없습니다")
    signer_email = _email(user)
    employee_email = str(contract.get("employee_email") or "").strip().lower()
    if not employee_email or signer_email != employee_email:
        raise HTTPException(status_code=403, detail="서명 대상 직원 계정이 아닙니다")
    return signer_email


def _validated_signature_image(data_uri: Any) -> tuple[str, str]:
    value = str(data_uri or "").strip()
    prefix = "data:image/png;base64,"
    if not value.startswith(prefix):
        raise HTTPException(status_code=400, detail="자필서명은 PNG 이미지 형식이어야 합니다")
    try:
        raw = base64.b64decode(value[len(prefix) :], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="자필서명 이미지가 올바르지 않습니다") from None
    if len(raw) < 100 or len(raw) > MAX_SIGNATURE_BYTES or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="자필서명 이미지 크기 또는 형식을 확인하십시오")
    return value, hashlib.sha256(raw).hexdigest()


def get_contract_by_token(token: str, user: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = next((row for row in _read("contracts") if row.get("sign_token") == token), None)
    if not contract:
        raise HTTPException(status_code=404, detail="서명 요청 계약서를 찾을 수 없습니다")
    _contract_signer_email(contract, user)
    if str(contract.get("status") or "") != "requested":
        raise HTTPException(status_code=409, detail="서명 요청된 계약서가 아닙니다")
    return contract


def sign_contract(payload: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    token = str(payload.get("token") or "")
    rows = _read("contracts")
    contract = next((row for row in rows if row.get("sign_token") == token), None)
    if not contract:
        raise HTTPException(status_code=404, detail="서명 요청 계약서를 찾을 수 없습니다")
    signer_email = _contract_signer_email(contract, user)
    if str(contract.get("status") or "") == "signed":
        raise HTTPException(status_code=409, detail="이미 서명 완료된 계약서입니다")
    if str(contract.get("status") or "") != "requested":
        raise HTTPException(status_code=409, detail="서명 요청된 계약서만 서명할 수 있습니다")
    if payload.get("consent") is not True:
        raise HTTPException(status_code=400, detail="계약 내용 확인 및 전자서명 동의가 필요합니다")
    consent_version = str(payload.get("consent_version") or "").strip()
    if consent_version != CONTRACT_SIGNATURE_CONSENT_VERSION:
        raise HTTPException(status_code=400, detail="전자서명 동의 문구를 새로 확인하십시오")
    signer_name = str(payload.get("signer_name") or "").strip()
    employee_name = str(contract.get("employee_name") or "").strip()
    if not signer_name or re.sub(r"\s+", "", signer_name) != re.sub(r"\s+", "", employee_name):
        raise HTTPException(status_code=400, detail="계약 대상 직원 이름을 정확히 입력하십시오")
    signature_data_uri, signature_sha256 = _validated_signature_image(payload.get("signature_data_uri"))
    _validate_contract_payload(contract)
    contract["status"] = "signed"
    contract["signed_at"] = _now()
    contract["signer_name"] = signer_name
    contract["signer_email"] = signer_email
    contract["signature_data_uri"] = signature_data_uri
    contract["signature_sha256"] = signature_sha256
    contract["signature_consent"] = {
        "accepted": True,
        "version": consent_version,
        "accepted_at": contract["signed_at"],
    }
    contract["signature_audit"] = {
        "authenticated_email": signer_email,
        "client_ip": str(payload.get("audit_ip") or "")[:64],
        "user_agent": str(payload.get("audit_user_agent") or "")[:512],
        "signed_at": contract["signed_at"],
    }
    contract["sign_token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
    contract.pop("sign_token", None)
    contract["updated_at"] = contract["signed_at"]
    snapshot, snapshot_sha256 = _signed_contract_snapshot(contract)
    contract["signed_snapshot"] = snapshot
    contract["signed_snapshot_sha256"] = snapshot_sha256
    _write("contracts", rows)
    return contract


def delete_contract(contract_id: str, user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="계약서 삭제 권한이 없습니다")
    contract = _find(_read("contracts"), contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="계약서를 찾을 수 없습니다")
    if str(contract.get("status") or "") == "signed":
        raise HTTPException(status_code=409, detail="서명 완료 계약서는 삭제할 수 없습니다")
    _delete("contracts", contract_id)


def list_payroll(user: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(_filter_user(_read("payroll_statements"), user, "employee_email"), key=lambda row: row.get("updated_at", ""), reverse=True)


def save_payroll(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="급여내역서 작성 권한이 없습니다")
    rows = _read("payroll_statements")
    gross = int(float(payload.get("gross_pay") or 0))
    taxable_pay = int(float(payload.get("taxable_pay") or 0))
    non_tax_meal = int(float(payload.get("non_tax_meal_allowance") or 0))
    if taxable_pay or non_tax_meal:
        if taxable_pay + non_tax_meal != gross:
            raise HTTPException(status_code=400, detail="과세급여와 비과세 식대 합계가 총지급액과 일치해야 합니다")
        if non_tax_meal < 0 or non_tax_meal > 200000:
            raise HTTPException(status_code=400, detail="비과세 식대는 월 200,000원 이내로 입력하십시오")
        if non_tax_meal > 0 and str(payload.get("meal_provision") or "") != "cash_no_meal":
            raise HTTPException(status_code=400, detail="사용자가 식사를 제공하는 경우 현금 식대를 비과세로 분류할 수 없습니다")
    deductions = int(float(payload.get("tax_withholding") or 0)) + int(float(payload.get("insurance_deduction") or 0)) + int(float(payload.get("other_deduction") or 0))
    now = _now()
    statement_id = str(payload.get("id") or uuid4())
    email = str(payload.get("employee_email") or "").strip().lower()
    statement = {
        **payload,
        "id": statement_id,
        "employee_email": email,
        "employee_email_masked": _mask_email(email),
        "gross_pay": gross,
        "taxable_pay": taxable_pay,
        "non_tax_meal_allowance": non_tax_meal,
        "tax_withholding": int(float(payload.get("tax_withholding") or 0)),
        "insurance_deduction": int(float(payload.get("insurance_deduction") or 0)),
        "other_deduction": int(float(payload.get("other_deduction") or 0)),
        "net_pay": max(0, gross - deductions),
        "created_at": payload.get("created_at") or now,
        "updated_at": now,
    }
    existing = _find(rows, statement_id)
    if existing:
        existing.update(statement)
        saved = existing
    else:
        rows.insert(0, statement)
        saved = statement
    _write("payroll_statements", rows)
    return saved


def delete_payroll(statement_id: str, user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="급여내역서 삭제 권한이 없습니다")
    _delete("payroll_statements", statement_id)


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(status_code=400, detail="CSV 문자 인코딩을 확인할 수 없습니다")


def _csv_delimiter(text: str) -> str:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if first_line.count("\t") > first_line.count(","):
        return "\t"
    if first_line.count(";") > first_line.count(","):
        return ";"
    return ","


def _first_present(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _amount(value: Any) -> int:
    cleaned = re.sub(r"[^0-9.-]", "", str(value or ""))
    if cleaned in {"", "-", ".", "-."}:
        return 0
    try:
        return int(round(float(cleaned)))
    except ValueError:
        return 0


def _transaction_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("/", "-").replace(".", "-")
    normalized = re.sub(r"\s+", " ", normalized)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.strftime("%Y-%m-%d %H:%M:%S") if "%H" in fmt else parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _transaction_category(text: str) -> str:
    lowered = str(text or "").lower()
    for category, keywords in DEFAULT_CATEGORY_RULES:
        if any(keyword.lower() in lowered for keyword in keywords):
            return category
    return "미분류"


def list_transactions() -> list[dict[str, Any]]:
    return sorted(_read("transactions"), key=lambda row: str(row.get("transaction_date") or ""), reverse=True)


def list_transactions_for_user(user: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="거래 원장 조회 권한이 없습니다")
    return list_transactions()


def create_transaction(payload: dict[str, Any]) -> dict[str, Any]:
    rows = _read("transactions")
    now = _now()
    record = {
        **payload,
        "id": str(payload.get("id") or uuid4()),
        "transaction_date": _transaction_date(payload.get("transaction_date")),
        "amount": _amount(payload.get("amount")),
        "created_at": payload.get("created_at") or now,
        "updated_at": now,
    }
    rows.insert(0, record)
    _write("transactions", rows)
    return record


def import_file(
    filename: str,
    content: bytes,
    source_type: str,
    *,
    business_id: str = "",
    branch: str = "",
    service: str = "",
    source_account_id: str = "",
) -> dict[str, Any]:
    normalized_source = str(source_type or "other").strip().lower()
    if normalized_source not in {"bank", "card", "other"}:
        raise HTTPException(status_code=400, detail="source_type은 bank, card, other 중 하나여야 합니다")
    normalized_business = str(business_id or "").strip()
    normalized_branch = BRANCH_ALIASES.get(str(branch or "").strip(), str(branch or "").strip())
    normalized_service = str(service or "").strip()
    decoded = _decode_csv(content)
    reader = csv.DictReader(decoded.splitlines(), delimiter=_csv_delimiter(decoded))
    existing = _read("transactions")
    existing_ids = {str(row.get("id") or "") for row in existing}
    imported: list[dict[str, Any]] = []
    duplicate_rows = 0
    now = _now()
    for source_row in reader:
        raw = {str(key or "").strip(): str(value or "").strip() for key, value in source_row.items()}
        if not any(raw.values()):
            continue
        fingerprint = json.dumps(
            {
                "source_type": normalized_source,
                "service": normalized_service,
                "business_id": normalized_business,
                "branch": normalized_branch,
                "row": raw,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        record_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        if record_id in existing_ids:
            duplicate_rows += 1
            continue
        incoming = _amount(_first_present(raw, "입금액", "입금", "입금금액", "맡기신금액", "받으신금액"))
        outgoing = _amount(_first_present(raw, "출금액", "출금", "출금금액", "찾으신금액", "지급금액"))
        amount = incoming or outgoing or _amount(
            raw.get("합계금액") or raw.get("결제금액") or raw.get("거래금액") or raw.get("금액")
        )
        direction = "income" if incoming > 0 else "expense"
        description = (
            raw.get("상품명")
            or raw.get("적요")
            or raw.get("거래내용")
            or raw.get("내용")
            or raw.get("기재내용")
            or raw.get("보낸분/받는분")
            or raw.get("보낸분")
            or raw.get("받는분")
            or raw.get("거래처")
            or raw.get("판매자상호")
            or ""
        )
        transaction_datetime = (
            raw.get("거래일시")
            or " ".join(
                item
                for item in (raw.get("거래일자") or raw.get("거래일") or raw.get("일자") or "", raw.get("거래시간") or "")
                if item
            )
        )
        searchable = " ".join([description, *raw.values()])
        record = {
            "id": record_id,
            "source_type": normalized_source,
            "service": normalized_service,
            "business_id": normalized_business,
            "branch": normalized_branch,
            "source_account_id": str(source_account_id or ""),
            "source_file": Path(filename or "upload.csv").name,
            "transaction_date": _transaction_date(transaction_datetime),
            "description": description,
            "amount": amount,
            "direction": direction,
            "category": _transaction_category(searchable),
            "approval_number": raw.get("승인번호") or "",
            "order_number": raw.get("주문번호") or "",
            "account_name": raw.get("계좌명") or raw.get("계좌번호") or raw.get("계좌") or "",
            "created_at": now,
            "updated_at": now,
        }
        imported.append(record)
        existing_ids.add(record_id)
    if imported:
        _write("transactions", imported + existing)
    return {
        "import": {
            "filename": Path(filename or "upload.csv").name,
            "source_type": normalized_source,
            "imported_rows": len(imported),
            "duplicate_rows": duplicate_rows,
        },
        "rows": imported,
    }


def list_accounts(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        return []
    rows = _read("platform_accounts")
    changed = _migrate_platform_account_secrets(rows)
    now = _now()
    for row in rows:
        public_status = _public_platform_account_status(row)
        raw_sync_status = str(row.get("last_sync_status") or row.get("portal_status") or "").strip()
        if raw_sync_status == "running" and public_status != "running":
            row["last_sync_status"] = public_status
            row["portal_status"] = public_status
            row["portal_message"] = (
                row.get("portal_message")
                or "이전 연동 실행이 완료 응답 없이 종료되어 연결 상태 확인이 필요합니다."
            )
            row["updated_at"] = now
            changed = True
    if changed:
        _write("platform_accounts", rows)
    result = []
    for row in rows:
        row_business = str(row.get("business_id") or "")
        if business_id and row_business != business_id:
            continue
        item = {k: v for k, v in row.items() if k not in _ACCOUNT_SECRET_FIELDS}
        item["branch"] = BRANCH_ALIASES.get(str(item.get("branch") or ""), str(item.get("branch") or ""))
        item["password_masked"] = "********" if _has_account_secret(row) else ""
        item["status"] = _public_platform_account_status(row)
        item["credential_requirements"] = _missing_connector_requirements(row)
        result.append(item)
    return result


def get_settings(user: dict[str, Any]) -> dict[str, Any]:
    data = _read_json_object("settings")
    ui_settings = data.get("ui_settings")
    if not isinstance(ui_settings, dict):
        ui_settings = {}
    ui_settings = _canonicalize_ui_settings(ui_settings)
    return {
        "settings": ui_settings,
        "meta": {
            "updated_at": data.get("ui_settings_updated_at") or "",
            "updated_by": data.get("ui_settings_updated_by") or "",
            "source": "server-file",
        },
    }


def save_settings(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="기초설정 저장 권한이 없습니다")
    settings = payload.get("settings") if isinstance(payload, dict) else payload
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings 객체가 필요합니다")
    allowed = {"businesses", "branches", "accounts", "staff", "integrations"}
    raw = {
        key: value
        for key, value in settings.items()
        if key in allowed and isinstance(value, list)
    }
    cleaned = _canonicalize_ui_settings(raw)
    data = _read_json_object("settings")
    now = _now()
    data["ui_settings"] = cleaned
    data["ui_settings_updated_at"] = now
    data["ui_settings_updated_by"] = _email(user)
    _write_json_object("settings", data)
    return {"settings": cleaned, "meta": {"updated_at": now, "updated_by": _email(user), "source": "server-file"}}


async def get_settings_persisted(user: dict[str, Any]) -> dict[str, Any]:
    pool = _get_pool_or_none()
    if pool is None:
        return get_settings(user)
    try:
        async with pool.acquire() as conn:
            ready = await conn.fetchval("SELECT to_regclass('public.yeoljeong_businesses') IS NOT NULL")
            if not ready:
                return get_settings(user)
            business_rows = await conn.fetch(
                """
                SELECT id, entity_type, name, registration_no, representative, tax_type,
                       opened_at, address, memo
                  FROM yeoljeong_businesses
                 WHERE deleted_at IS NULL
                 ORDER BY sort_order, id
                """
            )
            branch_rows = await conn.fetch(
                """
                SELECT id, business_id, name, status, phone, address
                  FROM yeoljeong_branches
                 WHERE deleted_at IS NULL
                 ORDER BY sort_order, id
                """
            )
            extra_row = await conn.fetchrow(
                "SELECT data, updated_at, updated_by FROM yeoljeong_settings WHERE scope = 'ui'"
            )
        extra = _jsonb_object(extra_row["data"]) if extra_row else {}
        settings = {
            "businesses": [
                {
                    "id": row["id"],
                    "entityType": row["entity_type"],
                    "name": row["name"],
                    "registrationNo": row["registration_no"],
                    "representative": row["representative"],
                    "taxType": row["tax_type"],
                    "openedAt": row["opened_at"] or "",
                    "address": row["address"] or "",
                    "memo": row["memo"] or "",
                }
                for row in business_rows
            ],
            "branches": [
                {
                    "id": row["id"],
                    "businessId": row["business_id"],
                    "name": row["name"],
                    "status": row["status"],
                    "phone": row["phone"] or "",
                    "address": row["address"] or "",
                }
                for row in branch_rows
            ],
            "accounts": extra.get("accounts") if isinstance(extra.get("accounts"), list) else [],
            "staff": extra.get("staff") if isinstance(extra.get("staff"), list) else [],
            "integrations": extra.get("integrations") if isinstance(extra.get("integrations"), list) else [],
        }
        return {
            "settings": _canonicalize_ui_settings(settings),
            "meta": {
                "updated_at": extra_row["updated_at"].isoformat(timespec="seconds") if extra_row and extra_row["updated_at"] else "",
                "updated_by": extra_row["updated_by"] if extra_row else "",
                "source": "database",
            },
        }
    except Exception:
        return get_settings(user)


async def save_settings_persisted(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    file_result = save_settings(payload, user)
    pool = _get_pool_or_none()
    if pool is None:
        return file_result
    settings = file_result["settings"]
    now = _now()
    updated_by = _email(user)
    try:
        async with pool.acquire() as conn:
            ready = await conn.fetchval("SELECT to_regclass('public.yeoljeong_businesses') IS NOT NULL")
            if not ready:
                return file_result
            async with conn.transaction():
                for sort_order, item in enumerate(settings["businesses"], start=1):
                    await conn.execute(
                        """
                        INSERT INTO yeoljeong_businesses
                            (id, entity_type, name, registration_no, representative, tax_type,
                             opened_at, address, memo, sort_order, updated_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                        ON CONFLICT (id) DO UPDATE
                           SET entity_type = EXCLUDED.entity_type,
                               name = EXCLUDED.name,
                               registration_no = EXCLUDED.registration_no,
                               representative = EXCLUDED.representative,
                               tax_type = EXCLUDED.tax_type,
                               opened_at = EXCLUDED.opened_at,
                               address = EXCLUDED.address,
                               memo = EXCLUDED.memo,
                               sort_order = EXCLUDED.sort_order,
                               updated_by = EXCLUDED.updated_by,
                               updated_at = NOW(),
                               deleted_at = NULL
                        """,
                        item["id"],
                        item.get("entityType") or "individual",
                        item["name"],
                        item.get("registrationNo") or "",
                        item.get("representative") or "",
                        item.get("taxType") or "",
                        item.get("openedAt") or "",
                        item.get("address") or "",
                        item.get("memo") or "",
                        sort_order,
                        updated_by,
                    )
                for sort_order, item in enumerate(settings["branches"], start=1):
                    await conn.execute(
                        """
                        INSERT INTO yeoljeong_branches
                            (id, business_id, name, status, phone, address, sort_order, updated_by)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (id) DO UPDATE
                           SET business_id = EXCLUDED.business_id,
                               name = EXCLUDED.name,
                               status = EXCLUDED.status,
                               phone = EXCLUDED.phone,
                               address = EXCLUDED.address,
                               sort_order = EXCLUDED.sort_order,
                               updated_by = EXCLUDED.updated_by,
                               updated_at = NOW(),
                               deleted_at = NULL
                        """,
                        item["id"],
                        item["businessId"],
                        item["name"],
                        item.get("status") or "active",
                        item.get("phone") or "",
                        item.get("address") or "",
                        sort_order,
                        updated_by,
                    )
                extra = {
                    "accounts": settings["accounts"],
                    "staff": settings["staff"],
                    "integrations": settings["integrations"],
                }
                await conn.execute(
                    """
                    INSERT INTO yeoljeong_settings (scope, data, updated_by)
                    VALUES ('ui', $1::jsonb, $2)
                    ON CONFLICT (scope) DO UPDATE
                       SET data = EXCLUDED.data,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = NOW()
                    """,
                    json.dumps(extra, ensure_ascii=False),
                    updated_by,
                )
        return {"settings": settings, "meta": {"updated_at": now, "updated_by": updated_by, "source": "database"}}
    except Exception:
        return file_result


def upsert_account(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="계정 등록 권한이 없습니다")
    rows = _read("platform_accounts")
    account_id = str(payload.get("account_id") or payload.get("server_account_id") or "").strip()
    service = str(payload.get("service") or "").strip()
    username = str(payload.get("username") or "").strip()
    if not service or not username:
        raise HTTPException(status_code=400, detail="연동 서비스와 아이디가 필요합니다")
    if service not in CONNECTOR_LABELS:
        raise HTTPException(status_code=400, detail="지원하지 않는 연동 서비스입니다")
    business_id, branch = _normalize_connector_scope(service, payload.get("business_id"), payload.get("branch"))
    existing = None
    if account_id:
        existing = next((row for row in rows if str(row.get("id") or "") == account_id), None)
        if not existing:
            raise HTTPException(status_code=404, detail="수정할 연동 계정을 찾지 못했습니다")
    if existing is None:
        existing = next(
            (
                row
                for row in rows
                if row.get("service") == service
                and row.get("username") == username
                and BRANCH_ALIASES.get(str(row.get("branch") or ""), str(row.get("branch") or "")) == branch
                and str(row.get("business_id") or "") == business_id
            ),
            None,
        )
    now = _now()
    record = existing or {"id": str(uuid4()), "created_at": now}
    collection_mode = str(payload.get("collection_mode") or "browser-automation").strip()
    if service in BANK_QUICK_SERVICE_CONFIG and collection_mode in {"bank-openbanking", "browser-automation"}:
        collection_mode = "bank-quick-service"
    secret_payload = False
    for plaintext_field, encrypted_field in _ACCOUNT_SECRET_FIELD_MAP.items():
        incoming_secret = str(payload.get(plaintext_field) or "")
        if incoming_secret:
            record[encrypted_field] = _encrypt_secret(incoming_secret)
            record.pop(plaintext_field, None)
            secret_payload = True
    if not secret_payload:
        _migrate_platform_account_secrets([record])
    if service in BANK_QUICK_SERVICE_CONFIG and collection_mode == "bank-quick-service":
        missing = [
            label
            for label, key in (
                ("로그인 비밀번호", "password"),
                ("조회용 계좌번호", "account_no"),
                ("계좌비밀번호", "account_password"),
                ("사업자번호", "business_registration_no"),
            )
            if not _has_secret_value(record, key)
        ]
        if missing:
            raise HTTPException(status_code=400, detail=f"은행 간편/빠른조회 필수값을 확인하십시오: {', '.join(missing)}")
    bank_quick_config = BANK_QUICK_SERVICE_CONFIG.get(service) or {}
    account_no_masked = (
        str(payload.get("account_no_masked") or "").strip()
        or _masked_digits(payload.get("account_no"))
        or str(record.get("account_no_masked") or "").strip()
    )
    business_no_masked = (
        str(payload.get("business_registration_no_masked") or "").strip()
        or _masked_digits(payload.get("business_registration_no"))
        or str(record.get("business_registration_no_masked") or "").strip()
    )
    record.update(
        {
            "service": service,
            "label": payload.get("label") or CONNECTOR_LABELS.get(service, service),
            "login_url": payload.get("login_url") or bank_quick_config.get("login_url") or "",
            "username": username,
            "business_id": business_id,
            "branch": branch,
            "institution_code": str(payload.get("institution_code") or "").strip(),
            "account_no_masked": account_no_masked,
            "business_registration_no_masked": business_no_masked,
            "merchant_no": str(payload.get("merchant_no") or "").strip(),
            "settlement_cycle": str(payload.get("settlement_cycle") or "").strip(),
            "collection_mode": collection_mode,
            "category": str(payload.get("category") or "").strip(),
            "data_scope": str(payload.get("data_scope") or "").strip(),
            "required_proof": str(payload.get("required_proof") or "").strip(),
            "auth_owner": str(payload.get("auth_owner") or "").strip(),
            "mfa_method": str(payload.get("mfa_method") or "").strip(),
            "credential_expires_at": str(payload.get("credential_expires_at") or "").strip(),
            "fallback_auth": str(payload.get("fallback_auth") or "").strip(),
            "sync_scope": str(payload.get("sync_scope") or "").strip(),
            "permission_scope": str(payload.get("permission_scope") or "").strip(),
            "failure_fallback": str(payload.get("failure_fallback") or "").strip(),
            "status": "credential_registered",
            "last_sync_status": record.get("last_sync_status") or "not_started",
            "auto_sync": bool(payload.get("auto_sync")),
            "memo": payload.get("memo") or bank_quick_config.get("enrollment") or "",
            "updated_at": now,
        }
    )
    if not existing:
        rows.insert(0, record)
    _write("platform_accounts", rows)
    public = {k: v for k, v in record.items() if k not in _ACCOUNT_SECRET_FIELDS}
    public["password_masked"] = "********" if _has_account_secret(record) else ""
    return public


def list_settlements(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="정산 원장 조회 권한이 없습니다")
    rows = _read("delivery_settlements")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def list_sales(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="매출 원장 조회 권한이 없습니다")
    rows = _read("delivery_sales")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def list_reviews(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="리뷰 원장 조회 권한이 없습니다")
    rows = _read("delivery_reviews")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def list_collection_status(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="수집 상태 조회 권한이 없습니다")
    rows = _read("delivery_collection_status")
    if _normalize_stale_delivery_collection_statuses(rows):
        stale_rows = [row for row in rows if str(row.get("error_code") or "") == "BACKGROUND_SYNC_STALE"]
        _write_delivery_collection_statuses(rows)
        for row in stale_rows:
            _run_db(_db_upsert_ledger("delivery_collection_status", row))
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def _normalize_stale_delivery_collection_statuses(rows: list[dict[str, Any]]) -> bool:
    now_dt = datetime.now(KST)
    now_text = now_dt.isoformat(timespec="seconds")
    changed = False
    for row in rows:
        if str(row.get("status") or "").strip() not in {"queued", "running"}:
            continue
        started_at = (
            _pg_ts(row.get("started_at"))
            or _pg_ts(row.get("queued_at"))
            or _pg_ts(row.get("updated_at"))
            or _pg_ts(row.get("created_at"))
        )
        if not started_at or now_dt - started_at < DELIVERY_SYNC_STALE_AFTER:
            continue
        row["status"] = "stale"
        row["error_code"] = "BACKGROUND_SYNC_STALE"
        row["message"] = "백그라운드 수집 작업이 15분 이상 완료 갱신 없이 멈춰 상태를 정리했습니다. 다시 수집 실행이 필요합니다."
        row["finished_at"] = now_text
        row["updated_at"] = now_text
        row.setdefault("counts", {"sales": 0, "settlements": 0, "reviews": 0})
        changed = True
    return changed


def _delivery_sync_window(payload: dict[str, Any]) -> tuple[date, date]:
    today = datetime.now(KST).date()
    default_from = today.replace(day=1).isoformat()
    date_from_text = str(payload.get("date_from") or default_from)
    date_to_text = str(payload.get("date_to") or today.isoformat())
    try:
        date_from = date.fromisoformat(date_from_text)
        date_to = date.fromisoformat(date_to_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="수집 기간은 YYYY-MM-DD 형식이어야 합니다") from exc
    if date_from > date_to or (date_to - date_from).days > 62:
        raise HTTPException(status_code=400, detail="수집 기간은 시작일 이후 최대 63일입니다")
    return date_from, date_to


def _delivery_requested_services(payload: dict[str, Any]) -> list[str]:
    from app.services.yeoljeong_delivery_collectors import PORTAL_CONFIG

    services = [str(item) for item in (payload.get("services") or []) if str(item).strip()]
    requested_services = services or sorted(PORTAL_CONFIG)
    unsupported = sorted(set(requested_services) - set(PORTAL_CONFIG))
    if unsupported:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 플랫폼: {', '.join(unsupported)}")
    return requested_services


def _delivery_platform_label(service: str) -> str:
    return PLATFORM_LABELS.get(service, service or "배달플랫폼")


def _delivery_all_scope_requested(payload: dict[str, Any]) -> bool:
    markers = {"all", "*", "__all__", "전체"}
    business = str(payload.get("business_id") or payload.get("businessId") or "").strip().lower()
    branch = str(payload.get("branch") or "").strip().lower()
    return bool(payload.get("all_businesses")) or business in markers or branch in markers


def _delivery_sync_scopes(
    payload: dict[str, Any],
    requested_services: list[str],
    all_accounts: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    if not _delivery_all_scope_requested(payload):
        return [
            _normalize_delivery_scope(
                str(payload.get("business_id") or MIA_BUSINESS_ID),
                str(payload.get("branch") or MIA_BRANCH_NAME),
            )
        ]

    canonical_scopes = [(str(item["businessId"]), str(item["name"])) for item in CANONICAL_BRANCHES]
    account_scopes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    requested = set(requested_services)
    for row in all_accounts:
        service = str(row.get("service") or "").strip()
        if service not in requested:
            continue
        branch = BRANCH_ALIASES.get(str(row.get("branch") or "").strip(), str(row.get("branch") or "").strip())
        business_id = str(row.get("business_id") or BUSINESS_BY_BRANCH.get(branch) or "").strip()
        if business_id not in CANONICAL_BUSINESS_IDS or BUSINESS_BY_BRANCH.get(branch) != business_id:
            continue
        scope = (business_id, branch)
        if scope not in seen:
            seen.add(scope)
            account_scopes.append(scope)
    return account_scopes or canonical_scopes


def _delivery_run_key(service: str, business_id: str, branch: str) -> str:
    return f"{business_id}|{branch}|{service}"


def _write_delivery_collection_statuses(rows: list[dict[str, Any]], current: dict[str, Any] | None = None) -> None:
    _write_file_rows("delivery_collection_status", rows)
    if current and current.get("id"):
        _run_db(_db_upsert_ledger("delivery_collection_status", current))


def queue_delivery_sync(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="자동 수집 실행 권한이 없습니다")

    requested_services = _delivery_requested_services(payload)
    date_from, date_to = _delivery_sync_window(payload)
    all_accounts = _read("platform_accounts")
    scopes = _delivery_sync_scopes(payload, requested_services, all_accounts)
    queued_at = _now()
    job_id = str(payload.get("sync_job_id") or f"delivery-sync-{uuid4().hex[:12]}")
    statuses = _read("delivery_collection_status")
    queued_run_ids: dict[str, str] = {}
    summary: list[dict[str, Any]] = []
    queued_records: list[dict[str, Any]] = []

    for business_id, branch in scopes:
        for service in requested_services:
            run_id = str(uuid4())
            run_key = _delivery_run_key(service, business_id, branch)
            queued_run_ids[run_key] = run_id
            if len(scopes) == 1:
                queued_run_ids[service] = run_id
            message = f"{branch} {_delivery_platform_label(service)} 백그라운드 수집 대기 중입니다."
            statuses.insert(
                0,
                status_record := {
                    "id": run_id,
                    "job_id": job_id,
                    "service": service,
                    "business_id": business_id,
                    "branch": branch,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "status": "queued",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                    "error_code": "",
                    "message": message,
                    "queued_at": queued_at,
                    "created_at": queued_at,
                    "updated_at": queued_at,
                },
            )
            queued_records.append(status_record)
            summary.append(
                {
                    "service": service,
                    "status": "queued",
                    "portal_status": "queued",
                    "error_code": "",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                    "run_id": run_id,
                    "job_id": job_id,
                    "account_id": str(payload.get("account_id") or payload.get("server_account_id") or ""),
                    "business_id": business_id,
                    "branch": branch,
                    "message": message,
                    "portal_message": message,
                }
            )

    _write_delivery_collection_statuses(statuses)
    for status_record in queued_records:
        _run_db(_db_upsert_ledger("delivery_collection_status", status_record))
    return {
        "queued": True,
        "job_id": job_id,
        "queued_run_ids": queued_run_ids,
        "synced_at": queued_at,
        "queued_at": queued_at,
        "business_id": scopes[0][0] if len(scopes) == 1 else "all",
        "branch": scopes[0][1] if len(scopes) == 1 else "전체",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": summary,
        "records": [],
        "sales": [],
        "settlements": [],
        "reviews": [],
        "totals": {"sales": 0, "settlements": 0, "reviews": 0},
    }


def _delivery_entry_record(record: dict[str, Any]) -> dict[str, Any]:
    service = str(record.get("service") or "")
    record_type = str(record.get("record_type") or "")
    amount = int(record.get("gross_amount") or record.get("settlement_amount") or 0)
    label = CONNECTOR_LABELS.get(service, service or "배달플랫폼")
    date_value = str(record.get("occurred_on") or datetime.now(KST).date().isoformat())
    if record_type == "settlements":
        entry_type = "bank"
        vendor = f"{label} 정산입금"
        memo = str(record.get("settlement_status") or record.get("settlement_id") or "")
    else:
        entry_type = "sales"
        vendor = f"{label} 매출"
        memo = str(record.get("order_status") or record.get("order_id") or "")
    return {
        "id": f"entry-{record.get('id') or uuid4()}",
        "source_record_id": record.get("id") or "",
        "source_type": "delivery",
        "type": entry_type,
        "service": service,
        "business_id": record.get("business_id") or "",
        "branch": record.get("branch") or "",
        "date": date_value,
        "vendor": vendor,
        "amount": amount,
        "status": "confirmed",
        "memo": memo,
    }


def automation_status(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "browser-automation",
        "status": "available",
        "message": "계정 기반 포털 수집과 CSV 정산서 가져오기를 사용할 수 있습니다. CAPTCHA·2차 인증은 사용자 조치가 필요합니다.",
        "checked_at": _now(),
    }


def _matching_accounts(
    *,
    services: list[str],
    business_id: str,
    branch: str,
    account_id: str = "",
) -> dict[str, dict[str, Any]]:
    rows = _read("platform_accounts")
    if _migrate_platform_account_secrets(rows):
        _write("platform_accounts", rows)
    requested_account_id = str(account_id or "").strip()
    candidates = [
        row
        for row in rows
        if str(row.get("service") or "") in services
        and (not requested_account_id or str(row.get("id") or "") == requested_account_id)
        and str(row.get("business_id") or "") == business_id
        and (
            not branch
            or not str(row.get("branch") or "").strip()
            or BRANCH_ALIASES.get(str(row.get("branch") or ""), str(row.get("branch") or "")) == branch
        )
    ]
    candidates.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
    accounts_by_service: dict[str, dict[str, Any]] = {}
    for service in services:
        service_rows = [row for row in candidates if str(row.get("service") or "") == service]
        if service_rows:
            accounts_by_service[service] = next((row for row in service_rows if _has_account_secret(row)), service_rows[0])
    return accounts_by_service


def import_transaction_csv(
    payload: dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="거래내역 가져오기 권한이 없습니다")
    service = str(payload.get("service") or "").strip()
    if service not in FINANCIAL_TRANSACTION_SERVICES:
        raise HTTPException(status_code=400, detail="은행/카드 거래 연동 서비스만 가져올 수 있습니다")
    business_id, branch = _normalize_connector_scope(service, payload.get("business_id"), payload.get("branch"))
    csv_text = str(payload.get("csv_text") or "")
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="거래내역 CSV 내용이 필요합니다")
    result = import_file(
        str(payload.get("filename") or "transactions.csv"),
        csv_text.encode("utf-8-sig"),
        TRANSACTION_SOURCE_BY_SERVICE[service],
        business_id=business_id,
        branch=branch,
        service=service,
        source_account_id=str(payload.get("source_account_id") or ""),
    )
    return {
        **result,
        "business_id": business_id,
        "branch": branch,
        "service": service,
        "transactions": result["rows"],
    }


def sync_financial_transactions(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="은행/카드 자동연동 실행 권한이 없습니다")
    services = [str(item) for item in (payload.get("services") or []) if str(item).strip()]
    requested_services = services or sorted(FINANCIAL_TRANSACTION_SERVICES)
    unsupported = sorted(set(requested_services) - FINANCIAL_TRANSACTION_SERVICES)
    if unsupported:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 은행/카드 연동: {', '.join(unsupported)}")
    today = datetime.now(KST).date()
    default_from = today.replace(day=1).isoformat()
    date_from_text = str(payload.get("date_from") or default_from)
    date_to_text = str(payload.get("date_to") or today.isoformat())
    try:
        date_from = date.fromisoformat(date_from_text)
        date_to = date.fromisoformat(date_to_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="연동 기간은 YYYY-MM-DD 형식이어야 합니다") from exc
    if date_from > date_to or (date_to - date_from).days > 92:
        raise HTTPException(status_code=400, detail="은행/카드 연동 기간은 시작일 이후 최대 93일입니다")
    business_id, branch = _normalize_connector_scope(
        "shinhan_business",
        payload.get("business_id") or MIA_BUSINESS_ID,
        payload.get("branch") or MIA_BRANCH_NAME,
    )
    accounts_by_service = _matching_accounts(
        services=requested_services,
        business_id=business_id,
        branch=branch,
        account_id=str(payload.get("account_id") or payload.get("server_account_id") or "").strip(),
    )
    synced_at = _now()
    transactions = _read("transactions")
    by_id = {str(row.get("id") or ""): row for row in transactions if row.get("id")}
    summary: list[dict[str, Any]] = []
    imported_rows: list[dict[str, Any]] = []

    for service in requested_services:
        account = accounts_by_service.get(service)
        if not account:
            summary.append(
                {
                    "service": service,
                    "status": "credential_required",
                    "message": "설정에서 계정/가맹점 정보를 먼저 등록해야 합니다.",
                    "imported_rows": 0,
                }
            )
            continue
        collection_mode = str(account.get("collection_mode") or "").strip()
        if collection_mode in {"bank-excel", "card-pg-report", "statement-upload"}:
            summary.append(
                {
                    "service": service,
                    "status": "upload_required",
                    "message": "현재 연동 방식은 파일 업로드입니다. 거래내역 CSV/리포트를 업로드하면 거래원장에 반영됩니다.",
                    "account_id": account.get("id") or "",
                    "imported_rows": 0,
                }
            )
            continue
        if collection_mode in {"api", "bank-openbanking", "browser-automation", "bank-quick-service"}:
            quick_missing = []
            if collection_mode == "bank-quick-service":
                quick_missing = [
                    label
                    for label, key in (
                        ("로그인 비밀번호", "password"),
                        ("조회용 계좌번호", "account_no"),
                        ("계좌비밀번호", "account_password"),
                        ("사업자번호", "business_registration_no"),
                    )
                    if not _has_secret_value(account, key)
                ]
            if not _has_account_secret(account) or quick_missing:
                status = "credential_required"
                message = (
                    f"은행 간편/빠른조회 필수값 누락: {', '.join(quick_missing)}"
                    if quick_missing
                    else "API 키, 인증서 비밀번호 또는 로그인 비밀번호를 설정에서 등록해야 합니다."
                )
            else:
                status = "connector_not_configured"
                bank_config = BANK_QUICK_SERVICE_CONFIG.get(service)
                if collection_mode == "bank-quick-service" and bank_config:
                    message = (
                        f"{bank_config['label']} 자격증명은 Vault에 준비됐지만 실시간 조회 Playwright 커넥터가 아직 연결되지 않았습니다. "
                        "은행 엑셀/CSV 다운로드 파일 또는 엑셀 복사표로 대체 반영할 수 있습니다."
                    )
                else:
                    message = "자격증명은 저장됐지만 해당 기관 실조회 커넥터가 아직 연결되지 않았습니다. 파일 업로드로 대체 수집할 수 있습니다."
            summary.append(
                {
                    "service": service,
                    "status": status,
                    "message": message,
                    "account_id": account.get("id") or "",
                    "collection_mode": collection_mode,
                    "imported_rows": 0,
                }
            )
            _mark_platform_account_sync_state(account, status=status, message=message, synced_at=synced_at)
            continue
        sample_rows = account.get("last_download_rows") if isinstance(account.get("last_download_rows"), list) else []
        count = 0
        for row in sample_rows:
            record = {
                **row,
                "service": service,
                "source_type": TRANSACTION_SOURCE_BY_SERVICE[service],
                "business_id": business_id,
                "branch": branch,
                "source_account_id": account.get("id") or "",
                "updated_at": synced_at,
            }
            record["id"] = str(record.get("id") or hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest())
            by_id[record["id"]] = record
            imported_rows.append(record)
            count += 1
        summary.append(
            {
                "service": service,
                "status": "completed" if count else "no_records",
                "message": "저장된 다운로드 거래를 반영했습니다." if count else "반영할 신규 거래가 없습니다.",
                "account_id": account.get("id") or "",
                "imported_rows": count,
            }
        )
        status = "completed" if count else "no_records"
        message = "저장된 다운로드 거래를 반영했습니다." if count else "반영할 신규 거래가 없습니다."
        _mark_platform_account_sync_state(account, status=status, message=message, synced_at=synced_at)

    if imported_rows:
        _write("transactions", list(by_id.values()))
    return {
        "synced_at": synced_at,
        "business_id": business_id,
        "branch": branch,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": summary,
        "transactions": imported_rows,
        "totals": {"transactions": len(imported_rows)},
    }


def _delivery_browser_auth_options(payload: dict[str, Any]) -> dict[str, str]:
    storage_state_path = str(payload.get("storage_state_path") or "").strip()
    browser_session_id = str(payload.get("browser_session_id") or "").strip()
    bridge_mode = ""
    if not storage_state_path:
        try:
            from app.browser_bridge.e2e_adapter import build_e2e_config

            config = build_e2e_config(session_id=browser_session_id or None)
            bridge_mode = str(config.get("mode") or "")
            browser_session_id = browser_session_id or str(config.get("session_id") or "").strip()
            storage_state_path = str(config.get("storage_state_path") or "").strip()
        except Exception:
            storage_state_path = ""
    return {
        "storage_state_path": storage_state_path if storage_state_path and Path(storage_state_path).is_file() else "",
        "browser_session_id": browser_session_id,
        "browser_bridge_mode": bridge_mode,
    }


def _baemin_bridge_page_kind(text: str) -> str:
    lowered = text.lower()
    if "리뷰" in text or "review" in lowered:
        return "reviews"
    if "정산" in text or "입금" in text or "settlement" in lowered:
        return "settlements"
    return "sales"


def _delivery_bridge_page_kind(text: str) -> str:
    lowered = str(text or "").lower()
    if "리뷰" in text or "review" in lowered:
        return "reviews"
    if any(term in text for term in ("정산", "입금", "지급")) or any(
        term in lowered for term in ("settlement", "deposit", "payout")
    ):
        return "settlements"
    return "sales"


def _money_from_text(value: str) -> int:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return int(digits or 0)


def _baemin_dashboard_records(text: str, business_id: str, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Capture summary data visible on the authenticated Baemin home dashboard."""
    compact = re.sub(r"\r\n?", "\n", str(text or ""))
    compact = "\n".join(line.strip() for line in compact.splitlines() if line.strip())
    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)
    collected_at = _now()
    records: dict[str, list[dict[str, Any]]] = {"sales": [], "settlements": [], "reviews": []}

    sales_match = re.search(r"어제\s*주문금액\s*([0-9,]+)\s*원.*?어제\s*주문수\s*([0-9,]+)\s*건", compact, re.S)
    if sales_match:
        amount = _money_from_text(sales_match.group(1))
        order_count = _money_from_text(sales_match.group(2))
        source_id = f"baemin-dashboard-sales-{yesterday.isoformat()}"
        records["sales"].append(
            {
                "id": hashlib.sha256(f"{business_id}|{branch}|{source_id}".encode("utf-8")).hexdigest(),
                "source_id": source_id,
                "business_id": business_id,
                "branch": branch,
                "service": "baemin",
                "platform": "baemin",
                "record_type": "sales",
                "occurred_on": yesterday.isoformat(),
                "gross_amount": amount,
                "order_count": order_count,
                "order_status": "dashboard_summary",
                "collected_at": collected_at,
            }
        )

    settlement_match = re.search(r"입금\s*예정\s*금액\s*([0-9,]+)\s*원", compact)
    if settlement_match:
        amount = _money_from_text(settlement_match.group(1))
        source_id = f"baemin-dashboard-settlement-{today.isoformat()}"
        records["settlements"].append(
            {
                "id": hashlib.sha256(f"{business_id}|{branch}|{source_id}".encode("utf-8")).hexdigest(),
                "source_id": source_id,
                "settlement_id": source_id,
                "business_id": business_id,
                "branch": branch,
                "service": "baemin",
                "platform": "baemin",
                "record_type": "settlements",
                "occurred_on": today.isoformat(),
                "settlement_amount": amount,
                "settlement_status": "입금예정",
                "collected_at": collected_at,
            }
        )

    review_matches = re.finditer(r"(오늘|어제)\n(.{8,800}?)\n열정국밥\s+중랑구중화점", compact, re.S)
    for index, match in enumerate(review_matches, start=1):
        review_text = re.sub(r"\s+", " ", match.group(2)).strip()
        if not review_text or review_text == "열정국밥 중랑구중화점":
            continue
        occurred_on = today if match.group(1) == "오늘" else yesterday
        source_material = f"baemin-dashboard-review-{occurred_on.isoformat()}-{index}-{review_text[:80]}"
        source_id = hashlib.sha256(source_material.encode("utf-8")).hexdigest()[:32]
        records["reviews"].append(
            {
                "id": hashlib.sha256(f"{business_id}|{branch}|baemin|reviews|{source_id}".encode("utf-8")).hexdigest(),
                "source_id": source_id,
                "review_id": source_id,
                "business_id": business_id,
                "branch": branch,
                "service": "baemin",
                "platform": "baemin",
                "record_type": "reviews",
                "occurred_on": occurred_on.isoformat(),
                "rating": 0,
                "review_text": review_text[:4000],
                "reply_status": "",
                "collected_at": collected_at,
            }
        )
    return records


def _baemin_bridge_login_state(url: str, text: str) -> str:
    lowered_url = str(url or "").lower()
    lowered_text = str(text or "").lower()
    if any(term in lowered_text for term in ("보안 위배 접근 제한", "올바르지 않은 요청", "access denied", "forbidden")):
        return "blocked"
    if any(term in lowered_text for term in ("captcha", "캡차", "보안문자", "2차 인증", "추가 인증", "본인인증", "휴대폰 인증", "기기 인증", "인증번호")):
        return "challenge"
    if "login" in lowered_url or all(marker in text for marker in ("로그인", "회원가입")):
        return "login"
    return "authenticated"


def _delivery_bridge_login_state(url: str, text: str) -> str:
    lowered_url = str(url or "").lower()
    lowered_text = str(text or "").lower()
    if any(term in lowered_text for term in ("보안 위배 접근 제한", "올바르지 않은 요청", "access denied", "forbidden")):
        return "blocked"
    if any(term in lowered_text for term in ("captcha", "캡차", "보안문자", "2차 인증", "추가 인증", "본인인증", "휴대폰 인증", "기기 인증", "인증번호", "약관 동의")):
        return "challenge"
    if "login" in lowered_url or any(term in text for term in ("로그인", "회원가입", "아이디", "비밀번호")):
        return "login"
    return "authenticated"


async def _baemin_bridge_first_visible(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if not hasattr(locator, "count") or not hasattr(locator, "is_visible"):
                return locator
            if await locator.count() and await locator.is_visible(timeout=700):
                return locator
        except Exception:
            continue
    return None


async def _baemin_bridge_fill_first(page: Any, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if hasattr(locator, "count") and hasattr(locator, "is_visible"):
                if not await locator.count() or not await locator.is_visible(timeout=700):
                    continue
            await locator.fill(value)
            return True
        except Exception:
            continue
    return False


async def _baemin_bridge_click_login(page: Any) -> bool:
    for selector in (
        "button[type='submit']",
        "input[type='submit']",
        "input[type='button'][value*='로그인']",
        "text=로그인",
    ):
        locator = page.locator(selector).first
        try:
            if hasattr(locator, "count") and hasattr(locator, "is_visible"):
                if not await locator.count() or not await locator.is_visible(timeout=700):
                    continue
            await locator.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


async def _baemin_bridge_login_with_saved_secret(page: Any, account: dict[str, Any]) -> dict[str, Any] | None:
    username = str(account.get("username") or "").strip()
    password = _decrypt_secret(str(account.get("password_enc") or "")) if _has_secret_value(account, "password") else ""
    if not username or not password:
        return {
            "status": "credential_required",
            "error_code": "PC_AGENT_LOGIN_REQUIRED",
            "records": {},
            "message": "PC Agent 브라우저가 배민 로그인 화면입니다. 저장된 배민 계정 비밀번호가 필요합니다.",
        }
    try:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            await page.wait_for_selector("input[type='password']", state="visible", timeout=8000)
        except Exception:
            pass
        username_filled = await _baemin_bridge_fill_first(
            page,
            (
                "input[autocomplete='username']",
                "input[name*='id' i]",
                "input[name*='user' i]",
                "input[type='email']",
                "input[type='text']",
            ),
            username,
        )
        password_filled = await _baemin_bridge_fill_first(
            page,
            ("input[autocomplete='current-password']", "input[type='password']"),
            password,
        )
        if not username_filled or not password_filled:
            return {
                "status": "portal_action_required",
                "error_code": "LOGIN_FORM_NOT_FOUND",
                "records": {},
            }
        clicked = await _baemin_bridge_click_login(page)
        if not clicked:
            password_input = await _baemin_bridge_first_visible(
                page,
                ("input[autocomplete='current-password']", "input[type='password']"),
            )
            if password_input is not None and hasattr(password_input, "press"):
                await password_input.press("Enter")
        await page.wait_for_timeout(5000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    finally:
        password = ""
    return None


async def _delivery_bridge_login_with_saved_secret(
    page: Any,
    account: dict[str, Any],
    service_label: str,
) -> dict[str, Any] | None:
    username = str(account.get("username") or "").strip()
    password = _decrypt_secret(str(account.get("password_enc") or "")) if _has_secret_value(account, "password") else ""
    if not username or not password:
        return {
            "status": "credential_required",
            "error_code": "PC_AGENT_LOGIN_REQUIRED",
            "records": {},
            "message": f"PC Agent 브라우저가 {service_label} 로그인 화면입니다. 저장된 계정 ID/PW가 필요합니다.",
        }
    try:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            await page.wait_for_selector("input[type='password']", state="visible", timeout=8000)
        except Exception:
            pass
        username_filled = await _baemin_bridge_fill_first(
            page,
            (
                "input[autocomplete='username']",
                "input[name*='id' i]",
                "input[name*='user' i]",
                "input[type='email']",
                "input[type='text']",
            ),
            username,
        )
        password_filled = await _baemin_bridge_fill_first(
            page,
            ("input[autocomplete='current-password']", "input[type='password']"),
            password,
        )
        if not username_filled or not password_filled:
            return {"status": "portal_action_required", "error_code": "LOGIN_FORM_NOT_FOUND", "records": {}}
        clicked = await _baemin_bridge_click_login(page)
        if not clicked:
            password_input = await _baemin_bridge_first_visible(
                page,
                ("input[autocomplete='current-password']", "input[type='password']"),
            )
            if password_input is not None and hasattr(password_input, "press"):
                await password_input.press("Enter")
        await page.wait_for_timeout(5000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    finally:
        password = ""
    return None


async def _delivery_bridge_click_first(page: Any, labels: tuple[str, ...], timeout: int = 2500) -> bool:
    if not hasattr(page, "get_by_role"):
        try:
            clicked = await page.evaluate(
                r"""
                labels => {
                  const normalizedLabels = labels.map(value => String(value || '').toLowerCase());
                  const candidates = [
                    ...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"],li,span,div')
                  ];
                  const visible = element => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  };
                  const textOf = element => String(element.innerText || element.textContent || element.value || '').trim().toLowerCase();
                  const target = candidates.find(element => {
                    if (!visible(element)) return false;
                    const text = textOf(element);
                    return text && normalizedLabels.some(label => text.includes(label));
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """,
                list(labels),
            )
            if clicked:
                await page.wait_for_timeout(800)
                return True
        except Exception:
            return False
    for label in labels:
        pattern = re.compile(re.escape(label), re.I)
        for role in ("button", "link", None):
            try:
                matches = page.get_by_role(role, name=pattern) if role else page.get_by_text(pattern)
                count = await matches.count() if hasattr(matches, "count") else 0
            except Exception:
                continue
            for index in range(min(count, 20)):
                locator = matches.nth(index)
                try:
                    if await locator.is_visible(timeout=500):
                        await locator.click(timeout=timeout)
                        await page.wait_for_timeout(800)
                        return True
                except Exception:
                    continue
    return False


async def _delivery_bridge_set_period(page: Any, date_from: str, date_to: str) -> None:
    try:
        if not hasattr(page.locator("body"), "count"):
            await page.evaluate(
                r"""
                ({dateFrom, dateTo}) => {
                  const dateInputs = [...document.querySelectorAll('input[type="date"]')];
                  if (dateInputs[0]) dateInputs[0].value = dateFrom;
                  if (dateInputs[1]) dateInputs[1].value = dateTo;
                  const startInputs = [...document.querySelectorAll('input[title*="시작 날짜"],input[placeholder*="시작"]')];
                  const endInputs = [...document.querySelectorAll('input[title*="종료 날짜"],input[placeholder*="종료"]')];
                  if (!dateInputs[0] && startInputs[0]) startInputs[0].value = dateFrom;
                  if (!dateInputs[1] && endInputs[0]) endInputs[0].value = dateTo;
                  [...dateInputs, ...startInputs, ...endInputs].forEach(input => {
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                  });
                }
                """,
                {"dateFrom": date_from, "dateTo": date_to},
            )
            await _delivery_bridge_click_first(page, ("조회", "검색", "적용"), timeout=2500)
            return
        date_inputs = page.locator("input[type='date']")
        count = await date_inputs.count()
        if count >= 1:
            await date_inputs.nth(0).fill(date_from)
        if count >= 2:
            await date_inputs.nth(1).fill(date_to)
        if count < 2:
            for selector, value in (("input[title*='시작 날짜']", date_from), ("input[title*='종료 날짜']", date_to)):
                locator = page.locator(selector).first
                try:
                    if await locator.count() and await locator.is_visible(timeout=400):
                        await locator.fill(value)
                except Exception:
                    continue
        await _delivery_bridge_click_first(page, ("조회", "검색", "적용"), timeout=2500)
    except Exception:
        return


async def _collect_delivery_from_browser_bridge_session_async(
    account: dict[str, Any],
    browser_auth: dict[str, str],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    service = str(account.get("service") or "").strip()
    session_id = str(browser_auth.get("browser_session_id") or "").strip()
    if not session_id:
        return {"status": "credential_required", "error_code": "PC_AGENT_SESSION_REQUIRED", "records": {}}
    try:
        from app.browser_bridge.service import get_browser_bridge_service
        from app.services.yeoljeong_delivery_collectors import PORTAL_CONFIG, parse_portal_export

        config = PORTAL_CONFIG.get(service)
        if not config:
            return {"status": "failed", "error_code": "UNSUPPORTED_PLATFORM", "records": {}}
        service_label = _delivery_platform_label(service)
        bridge = get_browser_bridge_service()
        session = bridge.sessions.get(session_id)
        if not session:
            return {"status": "credential_required", "error_code": "PC_AGENT_SESSION_NOT_FOUND", "records": {}}
        context = await bridge._context_for_session(session)
        page = context.pages[0] if getattr(context, "pages", None) else await context.new_page()

        if service == "baemin":
            home_url = "https://self.baemin.com/"
        else:
            home_url = str(account.get("portal_home_url") or account.get("home_url") or account.get("login_url") or config["login_url"])
        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        url = ""
        text = ""
        html = ""
        try:
            url = str(await page.evaluate("window.location.href") or "")
            text = str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
            html = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
        except Exception:
            pass

        login_state = _baemin_bridge_login_state(url, text) if service == "baemin" else _delivery_bridge_login_state(url, text)
        if login_state == "login":
            login_result = (
                await _baemin_bridge_login_with_saved_secret(page, account)
                if service == "baemin"
                else await _delivery_bridge_login_with_saved_secret(page, account, service_label)
            )
            if login_result is not None:
                login_result.setdefault("diagnostics", {}).update(
                    {"auth_mode": "pc_agent_browser", "browser_session_id": session_id, "url": url}
                )
                return login_result
            try:
                url = str(await page.evaluate("window.location.href") or url)
                text = str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
                html = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
            except Exception:
                pass
            login_state = _baemin_bridge_login_state(url, text) if service == "baemin" else _delivery_bridge_login_state(url, text)
        if login_state == "blocked":
            return {
                "status": "portal_action_required",
                "error_code": f"{service.upper()}_SECURITY_BLOCKED",
                "records": {},
                "diagnostics": {"auth_mode": "pc_agent_browser", "browser_session_id": session_id, "url": url},
                "message": f"{service_label} 포털이 접속을 보안 정책으로 차단했습니다. PC 브라우저에서 인증 또는 정산 CSV 업로드가 필요합니다.",
            }
        if login_state == "challenge":
            return {
                "status": "portal_action_required",
                "error_code": "PORTAL_AUTH_CHALLENGE",
                "records": {},
                "diagnostics": {"auth_mode": "pc_agent_browser", "browser_session_id": session_id, "url": url},
                "message": f"{service_label} 포털이 추가 인증을 요구합니다. PC 브라우저에서 인증을 완료한 뒤 다시 수집해야 합니다.",
            }
        if login_state == "login":
            return {
                "status": "credential_required",
                "error_code": "PC_AGENT_LOGIN_REQUIRED",
                "records": {},
                "diagnostics": {"auth_mode": "pc_agent_browser", "browser_session_id": session_id, "url": url},
                "message": f"PC Agent 브라우저가 {service_label} 로그인 화면입니다. 먼저 해당 포털 로그인이 필요합니다.",
            }

        records: dict[str, list[dict[str, Any]]] = {"sales": [], "settlements": [], "reviews": []}
        diagnostics: dict[str, str] = {
            "auth_mode": "pc_agent_browser",
            "browser_session_id": session_id,
            "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
            "url": url,
        }
        if service == "baemin":
            dashboard_records = _baemin_dashboard_records(
                text,
                str(account.get("business_id") or ""),
                str(account.get("branch") or ""),
            )
            for name, rows in dashboard_records.items():
                if rows:
                    records[name] = rows
            diagnostics["dashboard_sales"] = str(len(dashboard_records["sales"]))
            diagnostics["dashboard_settlements"] = str(len(dashboard_records["settlements"]))
            diagnostics["dashboard_reviews"] = str(len(dashboard_records["reviews"]))

        for kind, labels in config["sections"].items():
            clicked = await _delivery_bridge_click_first(page, tuple(labels), timeout=3500)
            if clicked:
                await _delivery_bridge_set_period(page, date_from, date_to)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            try:
                section_text = str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
                section_html = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
            except Exception:
                section_text = text
                section_html = html
            parsed = parse_portal_export(
                service,
                kind,
                section_html or section_text,
                str(account.get("business_id") or ""),
                str(account.get("branch") or ""),
            )
            incoming = parsed.get("records", {}).get(kind) or []
            if incoming:
                records[kind] = incoming
            diagnostics[kind] = str(parsed.get("diagnostics", {}).get("source") or ("clicked" if clicked else "section_not_found"))

        total_records = sum(len(rows) for rows in records.values())
        return {
            "status": "succeeded" if total_records else "partial",
            "error_code": "" if total_records else "AUTHENTICATED_NO_ROWS",
            "records": records,
            "diagnostics": diagnostics,
            "message": "" if total_records else f"{service_label} 로그인은 확인됐지만 조회 구간에서 표 데이터를 찾지 못했습니다.",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error_code": f"PC_AGENT_COLLECTOR_{exc.__class__.__name__.upper()}",
            "records": {},
            "diagnostics": {
                "auth_mode": "pc_agent_browser",
                "browser_session_id": session_id,
                "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
            },
            "message": str(exc)[:300],
        }


async def _collect_baemin_from_browser_bridge_session_async(
    account: dict[str, Any],
    browser_auth: dict[str, str],
) -> dict[str, Any]:
    session_id = str(browser_auth.get("browser_session_id") or "").strip()
    if not session_id:
        return {"status": "credential_required", "error_code": "PC_AGENT_SESSION_REQUIRED", "records": {}}
    try:
        from app.browser_bridge.service import get_browser_bridge_service
        from app.services.yeoljeong_delivery_collectors import parse_portal_export

        bridge = get_browser_bridge_service()
        session = bridge.sessions.get(session_id)
        if not session:
            return {"status": "credential_required", "error_code": "PC_AGENT_SESSION_NOT_FOUND", "records": {}}
        context = await bridge._context_for_session(session)
        page = context.pages[0] if getattr(context, "pages", None) else await context.new_page()
        url = str(getattr(page, "url", "") or "")
        try:
            url = str(await page.evaluate("window.location.href") or url)
        except Exception:
            pass
        text = ""
        html = ""
        try:
            text = str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
        except Exception:
            text = ""
        try:
            html = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
        except Exception:
            html = text
        login_state = _baemin_bridge_login_state(url, text)
        if login_state == "blocked":
            return {
                "status": "portal_action_required",
                "error_code": "BAEMIN_SECURITY_BLOCKED",
                "records": {},
                "diagnostics": {
                    "auth_mode": "pc_agent_browser",
                    "browser_session_id": session_id,
                    "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                    "url": url,
                },
                "message": "배민 포털이 접속을 보안 정책으로 차단했습니다. PC 브라우저에서 직접 인증 또는 정산 CSV 업로드가 필요합니다.",
            }
        if login_state == "challenge":
            return {
                "status": "portal_action_required",
                "error_code": "PORTAL_AUTH_CHALLENGE",
                "records": {},
                "diagnostics": {
                    "auth_mode": "pc_agent_browser",
                    "browser_session_id": session_id,
                    "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                    "url": url,
                },
                "message": "배민 포털이 추가 인증을 요구합니다. PC 브라우저에서 인증을 완료한 뒤 다시 수집해야 합니다.",
            }
        if login_state == "login":
            login_result = await _baemin_bridge_login_with_saved_secret(page, account)
            if login_result is not None:
                login_result.setdefault("diagnostics", {}).update(
                    {
                        "auth_mode": "pc_agent_browser",
                        "browser_session_id": session_id,
                        "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                        "url": url,
                    }
                )
                return login_result
            try:
                url = str(await page.evaluate("window.location.href") or url)
            except Exception:
                pass
            try:
                text = str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
            except Exception:
                text = ""
            try:
                html = str(await page.evaluate("document.body ? document.body.innerHTML : ''") or "")
            except Exception:
                html = text
            login_state = _baemin_bridge_login_state(url, text)
        if login_state == "login":
            return {
                "status": "credential_required",
                "error_code": "PC_AGENT_LOGIN_REQUIRED",
                "records": {},
                "diagnostics": {
                    "auth_mode": "pc_agent_browser",
                    "browser_session_id": session_id,
                    "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                    "url": url,
                },
                "message": "PC Agent 브라우저가 배민 로그인 화면입니다. 먼저 해당 브라우저에서 배민 관리자 로그인이 필요합니다.",
            }
        if login_state == "challenge":
            return {
                "status": "portal_action_required",
                "error_code": "PORTAL_AUTH_CHALLENGE",
                "records": {},
                "diagnostics": {
                    "auth_mode": "pc_agent_browser",
                    "browser_session_id": session_id,
                    "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                    "url": url,
                },
                "message": "배민 포털이 추가 인증을 요구합니다. PC 브라우저에서 인증을 완료한 뒤 다시 수집해야 합니다.",
            }
        if login_state == "blocked":
            return {
                "status": "portal_action_required",
                "error_code": "BAEMIN_SECURITY_BLOCKED",
                "records": {},
                "diagnostics": {
                    "auth_mode": "pc_agent_browser",
                    "browser_session_id": session_id,
                    "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                    "url": url,
                },
                "message": "배민 포털이 접속을 보안 정책으로 차단했습니다. PC 브라우저에서 직접 인증 또는 정산 CSV 업로드가 필요합니다.",
            }
        source = html or text
        dashboard_records = _baemin_dashboard_records(
            text,
            str(account.get("business_id") or ""),
            str(account.get("branch") or ""),
        )
        kind = _baemin_bridge_page_kind(text)
        parsed = parse_portal_export(
            "baemin",
            kind,
            source,
            str(account.get("business_id") or ""),
            str(account.get("branch") or ""),
        )
        records = {name: [] for name in ("sales", "settlements", "reviews")}
        records.update({name: rows for name, rows in dashboard_records.items() if rows})
        for name, rows in (parsed.get("records") or {}).items():
            if rows:
                records[name] = rows
        total_records = sum(len(rows) for rows in records.values())
        status = "succeeded" if total_records else str(parsed.get("status") or "partial")
        error_code = "" if total_records else str(parsed.get("error_code") or "")
        diagnostics = dict(parsed.get("diagnostics") or {})
        diagnostics.update(
            {
                "auth_mode": "pc_agent_browser",
                "browser_session_id": session_id,
                "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
                "url": url,
                "parsed_page_kind": kind,
                "dashboard_sales": str(len(dashboard_records["sales"])),
                "dashboard_settlements": str(len(dashboard_records["settlements"])),
                "dashboard_reviews": str(len(dashboard_records["reviews"])),
            }
        )
        return {
            "status": status,
            "error_code": error_code,
            "records": records,
            "diagnostics": diagnostics,
            "message": parsed.get("message") or "",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error_code": f"PC_AGENT_COLLECTOR_{exc.__class__.__name__.upper()}",
            "records": {},
            "diagnostics": {
                "auth_mode": "pc_agent_browser",
                "browser_session_id": session_id,
                "browser_bridge_mode": str(browser_auth.get("browser_bridge_mode") or ""),
            },
            "message": str(exc)[:300],
        }


def _collect_baemin_from_browser_bridge_session(
    account: dict[str, Any],
    browser_auth: dict[str, str],
) -> dict[str, Any] | None:
    if str(browser_auth.get("browser_bridge_mode") or "") != "local_agent":
        return None
    if not str(browser_auth.get("browser_session_id") or "").strip():
        return None
    return _run_async(_collect_baemin_from_browser_bridge_session_async(account, browser_auth))


def _collect_delivery_from_browser_bridge_session(
    account: dict[str, Any],
    browser_auth: dict[str, str],
    date_from: str,
    date_to: str,
) -> dict[str, Any] | None:
    if str(browser_auth.get("browser_bridge_mode") or "") != "local_agent":
        return None
    if not str(browser_auth.get("browser_session_id") or "").strip():
        return None
    return _run_async(_collect_delivery_from_browser_bridge_session_async(account, browser_auth, date_from, date_to))


def _normalize_delivery_collection_result(service: str, result: dict[str, Any]) -> dict[str, Any]:
    if service != "baemin" and str(result.get("error_code") or "") == "BAEMIN_SECURITY_BLOCKED":
        label = _delivery_platform_label(service)
        result = {**result}
        result["error_code"] = f"{service.upper()}_SECURITY_BLOCKED"
        result["message"] = f"{label} 포털이 서버 자동접속을 보안 정책으로 차단했습니다. PC 인증 세션 또는 정산 CSV 업로드가 필요합니다."
    return result


def sync_delivery(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="자동 수집 실행 권한이 없습니다")
    from app.services.yeoljeong_delivery_collectors import collect_account

    requested_services = _delivery_requested_services(payload)
    date_from, date_to = _delivery_sync_window(payload)
    requested_account_id = str(payload.get("account_id") or payload.get("server_account_id") or "").strip()
    queued_run_ids = payload.get("queued_run_ids") if isinstance(payload.get("queued_run_ids"), dict) else {}
    sync_job_id = str(payload.get("sync_job_id") or "").strip()

    all_accounts = _read("platform_accounts")
    if _migrate_platform_account_secrets(all_accounts):
        _write("platform_accounts", all_accounts)
    def _delivery_account_score(row: dict[str, Any], service: str) -> tuple[int, int, int, int, int, str]:
        mode = str(row.get("collection_mode") or row.get("collectionMode") or "").strip()
        upload_mode = mode in DELIVERY_UPLOAD_COLLECTION_MODES
        has_password = _has_secret_value(row, "password")
        has_any_secret = _has_account_secret(row)
        is_canonical_account = str(row.get("id") or "") == f"acct-{service}"
        is_canonical_upload = str(row.get("id") or "") == f"acct-{service}" and upload_mode
        return (
            1 if has_password and not upload_mode else 0,
            1 if has_any_secret and not upload_mode else 0,
            1 if not upload_mode else 0,
            1 if is_canonical_account else 0,
            0 if is_canonical_upload else 1,
            str(row.get("updated_at") or row.get("created_at") or ""),
        )

    scopes = _delivery_sync_scopes(payload, requested_services, all_accounts)
    synced_at = _now()
    summary = []
    ledger_names = {"sales": "delivery_sales", "settlements": "delivery_settlements", "reviews": "delivery_reviews"}
    ledgers = {name: _read(name) for name in ledger_names.values()}
    statuses = _read("delivery_collection_status")
    response_ledgers = {"sales": [], "settlements": [], "reviews": []}
    response_records: list[dict[str, Any]] = []
    browser_auth = _delivery_browser_auth_options(payload)

    for business_id, branch in scopes:
        candidates = [
            row
            for row in all_accounts
            if (not requested_services or row.get("service") in requested_services)
            and (not requested_account_id or str(row.get("id") or "") == requested_account_id)
            and str(row.get("business_id") or BUSINESS_BY_BRANCH.get(str(row.get("branch") or "")) or "") == business_id
            and BRANCH_ALIASES.get(str(row.get("branch") or ""), str(row.get("branch") or "")) == branch
        ]
        candidates.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        accounts_by_service: dict[str, dict[str, Any]] = {}
        for requested_service in requested_services:
            service_rows = [row for row in candidates if str(row.get("service") or "") == requested_service]
            if requested_account_id and service_rows:
                accounts_by_service[requested_service] = service_rows[0]
            elif service_rows:
                accounts_by_service[requested_service] = max(
                    service_rows,
                    key=lambda row: _delivery_account_score(row, requested_service),
                )

        for service in requested_services:
            account = accounts_by_service.get(service)
            run_id = str(
                queued_run_ids.get(_delivery_run_key(service, business_id, branch))
                or (queued_run_ids.get(service) if len(scopes) == 1 else "")
                or uuid4()
            )
            queued_status = next((row for row in statuses if str(row.get("id") or "") == run_id), None)
            status_record = {
                "id": run_id,
                "job_id": sync_job_id,
                "service": service,
                "business_id": business_id,
                "branch": branch,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "status": "running",
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                "error_code": "",
                "started_at": synced_at,
                "created_at": synced_at,
                "updated_at": synced_at,
            }
            if queued_status:
                queued_status.update(status_record)
                status_record = queued_status
            else:
                statuses.insert(0, status_record)
            _write_delivery_collection_statuses(statuses, status_record)
            if not account:
                result = {"status": "credential_required", "error_code": "ACCOUNT_NOT_REGISTERED", "records": {}}
            else:
                collection_mode = str(account.get("collection_mode") or account.get("collectionMode") or "").strip()
                collection_account = dict(account)
                if service == "baemin" and browser_auth["storage_state_path"]:
                    collection_account["storage_state_path"] = browser_auth["storage_state_path"]
                can_use_stored_browser_session = service == "baemin" and bool(browser_auth["storage_state_path"])
                bridge_result = (
                    _collect_baemin_from_browser_bridge_session(collection_account, browser_auth)
                    if service == "baemin"
                    else _collect_delivery_from_browser_bridge_session(
                        collection_account,
                        browser_auth,
                        date_from.isoformat(),
                        date_to.isoformat(),
                    )
                )
                if bridge_result is not None:
                    result = bridge_result
                elif collection_mode in DELIVERY_UPLOAD_COLLECTION_MODES and not can_use_stored_browser_session:
                    label = _delivery_platform_label(service)
                    result = {
                        "status": "upload_required",
                        "error_code": "CSV_UPLOAD_REQUIRED",
                        "records": {},
                        "diagnostics": {"collection_mode": collection_mode},
                        "message": f"{label} 포털 CSV/엑셀 정산서 업로드가 필요한 계정입니다.",
                    }
                elif not _has_secret_value(account, "password") and not can_use_stored_browser_session:
                    label = _delivery_platform_label(service)
                    result = {
                        "status": "credential_required",
                        "error_code": "CREDENTIAL_REQUIRED",
                        "records": {},
                        "message": f"{label} 계정 비밀번호가 등록되지 않았습니다.",
                    }
                else:
                    secret = _decrypt_secret(str(account.get("password_enc") or "")) if _has_secret_value(account, "password") else ""
                    result = collect_account(collection_account, secret, date_from.isoformat(), date_to.isoformat())
                    if service == "baemin" and browser_auth["browser_session_id"]:
                        result.setdefault("diagnostics", {})["browser_session_id"] = browser_auth["browser_session_id"]
                    if service == "baemin" and browser_auth["browser_bridge_mode"]:
                        result.setdefault("diagnostics", {})["browser_bridge_mode"] = browser_auth["browser_bridge_mode"]
                result = _normalize_delivery_collection_result(service, result)

            counts = {"sales": 0, "settlements": 0, "reviews": 0}
            for kind, ledger_name in ledger_names.items():
                incoming = result.get("records", {}).get(kind) or []
                by_id = {str(row.get("id") or ""): row for row in ledgers[ledger_name] if row.get("id")}
                for record in incoming:
                    by_id[str(record["id"])] = record
                ledgers[ledger_name] = list(by_id.values())
                counts[kind] = len(incoming)
                response_ledgers[kind].extend(incoming)
                if kind in {"sales", "settlements"}:
                    response_records.extend(_delivery_entry_record(record) for record in incoming)
            finished_at = _now()
            status_record.update(
                {
                    "status": result.get("status") or "failed",
                    "counts": counts,
                    "error_code": result.get("error_code") or "",
                    "diagnostics": result.get("diagnostics") or {},
                    "message": result.get("message") or "",
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                }
            )
            _write_delivery_collection_statuses(statuses, status_record)
            if account:
                account["last_sync_status"] = status_record["status"]
                account["portal_status"] = status_record["status"]
                account["portal_message"] = status_record["message"] or status_record["error_code"]
                account["last_sync_at"] = finished_at
                account["updated_at"] = finished_at
            summary.append(
                {
                    "service": service,
                    "status": status_record["status"],
                    "portal_status": status_record["status"],
                    "error_code": status_record["error_code"],
                    "counts": counts,
                    "run_id": run_id,
                    "account_id": account.get("id") if account else "",
                    "collection_mode": str(account.get("collection_mode") or account.get("collectionMode") or "") if account else "",
                    "business_id": business_id,
                    "branch": branch,
                    "message": status_record["message"],
                    "portal_message": status_record["message"],
                }
            )

    for name, rows in ledgers.items():
        _write(name, rows)
    _write("platform_accounts", all_accounts)
    _write_delivery_collection_statuses(statuses)
    return {
        "synced_at": synced_at,
        "business_id": scopes[0][0] if len(scopes) == 1 else "all",
        "branch": scopes[0][1] if len(scopes) == 1 else "전체",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": summary,
        "records": response_records,
        "sales": response_ledgers["sales"],
        "settlements": response_ledgers["settlements"],
        "reviews": response_ledgers["reviews"],
        "totals": {
            "sales": sum(item["counts"]["sales"] for item in summary),
            "settlements": sum(item["counts"]["settlements"] for item in summary),
            "reviews": sum(item["counts"]["reviews"] for item in summary),
        },
    }


def import_delivery_portal_text(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="배달 포털 파싱 반영 권한이 없습니다")
    from app.services.yeoljeong_delivery_collectors import PORTAL_CONFIG, parse_portal_export

    service = str(payload.get("service") or "").strip()
    record_type = str(payload.get("record_type") or payload.get("recordType") or "").strip()
    if service not in PORTAL_CONFIG:
        raise HTTPException(status_code=400, detail="지원하지 않는 배달 플랫폼입니다")
    if record_type not in {"sales", "settlements", "reviews"}:
        raise HTTPException(status_code=400, detail="반영 구분은 sales, settlements, reviews 중 하나여야 합니다")

    business_id, branch = _normalize_delivery_scope(payload.get("business_id"), payload.get("branch"))
    source_text = str(payload.get("source_text") or payload.get("sourceText") or "")
    parsed = parse_portal_export(service, record_type, source_text, business_id, branch)
    ledger_names = {"sales": "delivery_sales", "settlements": "delivery_settlements", "reviews": "delivery_reviews"}
    ledger_name = ledger_names[record_type]
    existing_rows = _read(ledger_name)
    by_id = {str(row.get("id") or ""): row for row in existing_rows if row.get("id")}
    imported: list[dict[str, Any]] = []
    duplicate_rows = 0
    now = _now()
    for record in parsed.get("records", {}).get(record_type) or []:
        record["source_file"] = Path(str(payload.get("filename") or "pc-browser-copy.html")).name
        record["collection_mode"] = "pc-browser-parse"
        record["created_at"] = record.get("created_at") or now
        record["updated_at"] = now
        record_id = str(record.get("id") or "")
        if record_id in by_id:
            duplicate_rows += 1
        by_id[record_id] = record
        imported.append(record)
    if imported or parsed.get("status") != "succeeded":
        _write(ledger_name, list(by_id.values()))

    statuses = _read("delivery_collection_status")
    counts = {"sales": 0, "settlements": 0, "reviews": 0}
    counts[record_type] = len(imported)
    run_id = str(uuid4())
    statuses.insert(
        0,
        {
            "id": run_id,
            "service": service,
            "business_id": business_id,
            "branch": branch,
            "date_from": str(payload.get("date_from") or ""),
            "date_to": str(payload.get("date_to") or ""),
            "status": parsed.get("status") or "failed",
            "counts": counts,
            "error_code": parsed.get("error_code") or "",
            "diagnostics": {
                **(parsed.get("diagnostics") or {}),
                "collection_mode": "pc-browser-parse",
                "record_type": record_type,
                "duplicate_rows": duplicate_rows,
            },
            "message": parsed.get("message") or "PC에서 로그인 후 복사/저장한 배민 화면 데이터를 파싱해 반영했습니다.",
            "started_at": now,
            "finished_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    _write_delivery_collection_statuses(statuses, statuses[0] if statuses else None)

    response_ledgers = {"sales": [], "settlements": [], "reviews": []}
    response_ledgers[record_type] = imported
    records = [_delivery_entry_record(record) for record in imported if record_type in {"sales", "settlements"}]
    return {
        "synced_at": now,
        "business_id": business_id,
        "branch": branch,
        "summary": [
            {
                "service": service,
                "status": parsed.get("status") or "failed",
                "portal_status": parsed.get("status") or "failed",
                "error_code": parsed.get("error_code") or "",
                "counts": counts,
                "run_id": run_id,
                "collection_mode": "pc-browser-parse",
                "message": parsed.get("message") or "PC 브라우저 파싱 반영 완료",
                "portal_message": parsed.get("message") or "PC 브라우저 파싱 반영 완료",
            }
        ],
        "records": records,
        "sales": response_ledgers["sales"],
        "settlements": response_ledgers["settlements"],
        "reviews": response_ledgers["reviews"],
        "totals": counts,
        "import": {"imported": len(imported), "duplicate_rows": duplicate_rows},
    }


def import_settlement_csv(
    text: str,
    user: dict[str, Any],
    *,
    service: str,
    business_id: str,
    branch: str,
    filename: str = "settlement.csv",
) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="정산서 가져오기 권한이 없습니다")
    from app.services.yeoljeong_delivery_collectors import PORTAL_CONFIG, normalize_record

    normalized_service = str(service or "").strip()
    if normalized_service not in PORTAL_CONFIG:
        raise HTTPException(status_code=400, detail="지원하지 않는 배달 플랫폼입니다")
    normalized_business, normalized_branch = _normalize_delivery_scope(business_id, branch)
    rows = _read("delivery_settlements")
    reader = csv.DictReader(text.splitlines())
    by_id = {str(row.get("id") or ""): row for row in rows if row.get("id")}
    imported: list[dict[str, Any]] = []
    duplicate_rows = 0
    now = _now()
    for raw in reader:
        source_row = {str(key or "").strip(): value for key, value in raw.items()}
        if not any(str(value or "").strip() for value in source_row.values()):
            continue
        record = normalize_record(
            normalized_service,
            "settlements",
            source_row,
            normalized_business,
            normalized_branch,
        )
        record["source_file"] = Path(filename or "settlement.csv").name
        record["created_at"] = now
        record["updated_at"] = now
        if str(record["id"]) in by_id:
            duplicate_rows += 1
            continue
        by_id[str(record["id"])] = record
        imported.append(record)
    if imported:
        _write("delivery_settlements", list(by_id.values()))
    return {
        "imported": len(imported),
        "duplicate_rows": duplicate_rows,
        "business_id": normalized_business,
        "branch": normalized_branch,
        "service": normalized_service,
        "records": imported,
        "settlements": imported,
    }


def reset_data_for_tests() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
