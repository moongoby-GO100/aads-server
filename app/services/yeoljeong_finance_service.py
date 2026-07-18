"""JSON-backed store for the Yeoljeong store assistant app.

The app is a static SPA, so this service keeps the first operational HR flow
small and explicit: employee join requests, onboarding documents, contracts,
payroll statements, and delivery account status.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, UploadFile

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("YEOLJEONG_FINANCE_DATA_DIR", "app/data/yeoljeong_finance"))
UPLOAD_DIR = DATA_DIR / "uploads" / "onboarding"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

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

PLATFORM_LABELS = {
    "baemin": "배민셀프서비스",
    "coupangeats": "쿠팡이츠",
    "yogiyo": "요기요",
    "ddangyo": "땡겨요",
}

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
        "template_version": "majangbiseo-freelancer-2026-07-a4",
        "print_title": "3.3% 프리랜서 용역계약서",
    },
    "confidentiality": {
        "document_kind": "confidentiality_agreement",
        "template_version": "majangbiseo-confidentiality-2026-07-a4",
        "print_title": "보안 및 개인정보 보호 서약서",
    },
    "default": {
        "document_kind": "standard_employment_contract",
        "template_version": "majangbiseo-employment-2026-07-a4",
        "print_title": "표준근로계약서",
    },
}

CANONICAL_BUSINESSES: list[dict[str, Any]] = [
    {
        "id": "biz-junghwa",
        "entityType": "individual",
        "name": "열정국밥 중화점",
        "registrationNo": "기초등록 필요",
        "representative": "미등록",
        "taxType": "일반과세",
        "openedAt": "",
        "address": "",
        "memo": "개인사업자 1",
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
        "registrationNo": "기초등록 필요",
        "representative": "미등록",
        "taxType": "일반과세",
        "openedAt": "",
        "address": "",
        "memo": "개인사업자 3",
    },
]

CANONICAL_BRANCHES: list[dict[str, Any]] = [
    {"id": "branch-junghwa", "name": "중화점", "businessId": "biz-junghwa", "status": "active", "phone": "", "address": ""},
    {"id": "branch-sungshin", "name": "성신여대점", "businessId": "biz-sungshin", "status": "active", "phone": "", "address": ""},
    {"id": "branch-gangbuk-mia", "name": "열정국밥_미아점", "businessId": "biz-mia", "status": "active", "phone": "", "address": ""},
]

CANONICAL_BUSINESS_IDS = {item["id"] for item in CANONICAL_BUSINESSES}
CANONICAL_BRANCH_NAMES = {item["name"] for item in CANONICAL_BRANCHES}
MIA_BUSINESS_ID = "biz-mia"
MIA_BRANCH_NAME = "열정국밥_미아점"


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


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
    url = os.getenv("DATABASE_URL", "")
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
        return asyncio.run(coro)
    except Exception:
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


def _pg_ts(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(KST).isoformat()
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.replace(tzinfo=KST).isoformat()
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
        payload = json.dumps(record, ensure_ascii=False)
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
                hashlib.sha256(token.encode("utf-8")).hexdigest() if token else "",
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
        if db_rows:
            return db_rows
        if file_rows:
            for row in file_rows:
                _run_db(_db_upsert_ledger(name, row))
            seeded = _run_db(_db_fetch_ledger(name))
            if isinstance(seeded, list):
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


def _migrate_platform_account_secrets(rows: list[dict[str, Any]]) -> bool:
    changed = False
    for row in rows:
        plaintext = str(row.get("password") or "")
        if not plaintext:
            continue
        if not row.get("password_enc"):
            row["password_enc"] = _encrypt_secret(plaintext)
        row.pop("password", None)
        changed = True
    return changed


def _has_account_secret(row: dict[str, Any]) -> bool:
    return bool(row.get("password_enc") or row.get("password"))


def _get_pool_or_none() -> Any | None:
    try:
        from app.core.db_pool import get_pool

        return get_pool()
    except Exception:
        return None


async def get_storage_status(user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="저장소 상태 확인 권한이 없습니다")
    json_ledgers = []
    for name in JSON_LEDGER_FILES:
        path = _path(name)
        json_ledgers.append(
            {
                "name": name,
                "path": str(path),
                "exists": path.exists(),
                "records": len(_read(name)) if path.exists() else 0,
            }
        )

    pool = _get_pool_or_none()
    db_tables = {name: False for name in (*SETTINGS_TABLES, *HR_LEDGER_TABLES)}
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

    settings_db_ready = all(db_tables[name] for name in SETTINGS_TABLES)
    hr_db_ready = all(db_tables[name] for name in HR_LEDGER_TABLES)
    return {
        "checked_at": _now(),
        "mode": "database+json-fallback" if settings_db_ready else "json-only",
        "settings": {
            "source": "database" if settings_db_ready else "json",
            "tables": {name: db_tables[name] for name in SETTINGS_TABLES},
        },
        "hr_ledgers": {
            "source": "database" if hr_db_ready else "json",
            "tables": {name: db_tables[name] for name in HR_LEDGER_TABLES},
            "json_files": json_ledgers,
        },
        "migration": {
            "settings": "113_yeoljeong_finance_settings.sql",
            "hr_ledgers": "115_yeoljeong_finance_hr_ledgers.sql",
            "hr_db_ready": hr_db_ready,
            "note": "HR/계약/급여 API는 HR 테이블이 실제 적용되기 전까지 기존 JSON 원장을 유지합니다.",
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
    record.update(
        {
            "name": str(payload.get("name") or record.get("name") or "").strip(),
            "email": email,
            "email_masked": _mask_email(email),
            "phone": str(payload.get("phone") or record.get("phone") or "").strip(),
            "phone_masked": _mask_phone(str(payload.get("phone") or record.get("phone") or "")),
            "branch": str(payload.get("branch") or record.get("branch") or "").strip(),
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


def list_approved_employees(user: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_admin(user):
        return []
    docs = _read("onboarding_documents")
    contracts = _read("contracts")
    payroll = _read("payroll_statements")

    result = []
    for row in _read("employee_join_requests"):
        if row.get("status") != "approved":
            continue
        email = str(row.get("email") or "").strip().lower()
        employee = dict(row)
        employee["email_masked"] = employee.get("email_masked") or _mask_email(email)
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
        "employee_name": employee_name.strip(),
        "employee_email": email,
        "employee_email_masked": _mask_email(email),
        "branch": branch.strip(),
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


def _onboarding_missing_document_rows(
    *,
    existing_rows: list[dict[str, Any]],
    user: dict[str, Any],
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
                    "branch": employee.get("branch") or "",
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


def list_onboarding_documents(user: dict[str, Any]) -> list[dict[str, Any]]:
    stored_rows = _read("onboarding_documents")
    visible_rows = _filter_user(stored_rows, user, "employee_email", "uploaded_by")
    rows = visible_rows + _onboarding_missing_document_rows(existing_rows=stored_rows, user=user)
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
    contract = _contract_defaults(payload)
    existing = _find(rows, contract["id"])
    if existing:
        existing.update(contract)
        saved = existing
    else:
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
    contract["status"] = "requested"
    contract["sign_token"] = contract.get("sign_token") or secrets.token_urlsafe(24)
    contract["requested_at"] = _now()
    contract["updated_at"] = contract["requested_at"]
    _write("contracts", rows)
    return contract


def get_contract_by_token(token: str) -> dict[str, Any]:
    contract = next((row for row in _read("contracts") if row.get("sign_token") == token), None)
    if not contract:
        raise HTTPException(status_code=404, detail="서명 요청 계약서를 찾을 수 없습니다")
    return contract


def sign_contract(payload: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    token = str(payload.get("token") or "")
    rows = _read("contracts")
    contract = next((row for row in rows if row.get("sign_token") == token), None)
    if not contract:
        raise HTTPException(status_code=404, detail="서명 요청 계약서를 찾을 수 없습니다")
    signer_email = str(payload.get("signer_email") or (user and _email(user)) or "").strip().lower()
    if signer_email and str(contract.get("employee_email") or "").strip().lower() not in {"", signer_email} and not (user and _is_admin(user)):
        raise HTTPException(status_code=403, detail="서명 대상자가 아닙니다")
    contract["status"] = "signed"
    contract["signed_at"] = _now()
    contract["signer_name"] = str(payload.get("signer_name") or contract.get("employee_name") or "")
    contract["signer_email"] = signer_email
    contract["updated_at"] = contract["signed_at"]
    _write("contracts", rows)
    return contract


def delete_contract(contract_id: str, user: dict[str, Any]) -> None:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="계약서 삭제 권한이 없습니다")
    _delete("contracts", contract_id)


def list_payroll(user: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(_filter_user(_read("payroll_statements"), user, "employee_email"), key=lambda row: row.get("updated_at", ""), reverse=True)


def save_payroll(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="급여내역서 작성 권한이 없습니다")
    rows = _read("payroll_statements")
    gross = int(float(payload.get("gross_pay") or 0))
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


def list_accounts(user: dict[str, Any]) -> list[dict[str, Any]]:
    if not _is_admin(user):
        return []
    rows = _read("platform_accounts")
    if _migrate_platform_account_secrets(rows):
        _write("platform_accounts", rows)
    result = []
    for row in rows:
        item = {k: v for k, v in row.items() if k not in {"password", "password_enc"}}
        item["password_masked"] = "********" if _has_account_secret(row) else ""
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
    service = str(payload.get("service") or "").strip()
    username = str(payload.get("username") or "").strip()
    if not service or not username:
        raise HTTPException(status_code=400, detail="플랫폼과 아이디가 필요합니다")
    existing = next((row for row in rows if row.get("service") == service and row.get("username") == username and row.get("branch") == payload.get("branch")), None)
    now = _now()
    record = existing or {"id": str(uuid4()), "created_at": now}
    incoming_password = str(payload.get("password") or "")
    if incoming_password:
        record["password_enc"] = _encrypt_secret(incoming_password)
        record.pop("password", None)
    elif record.get("password"):
        _migrate_platform_account_secrets([record])
    record.update(
        {
            "service": service,
            "label": payload.get("label") or PLATFORM_LABELS.get(service, service),
            "login_url": payload.get("login_url") or "",
            "username": username,
            "business_id": payload.get("business_id") or "",
            "branch": payload.get("branch") or "",
            "collection_mode": payload.get("collection_mode") or "browser-automation",
            "status": "credential_registered",
            "last_sync_status": record.get("last_sync_status") or "not_started",
            "memo": payload.get("memo") or "",
            "updated_at": now,
        }
    )
    if not existing:
        rows.insert(0, record)
    _write("platform_accounts", rows)
    public = {k: v for k, v in record.items() if k not in {"password", "password_enc"}}
    public["password_masked"] = "********" if _has_account_secret(record) else ""
    return public


def list_settlements(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    rows = _read("delivery_settlements")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def list_sales(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    rows = _read("delivery_sales")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def list_reviews(user: dict[str, Any], business_id: str | None = None) -> list[dict[str, Any]]:
    rows = _read("delivery_reviews")
    if business_id:
        rows = [row for row in rows if str(row.get("business_id") or "") == business_id]
    return rows


def list_collection_status(user: dict[str, Any]) -> list[dict[str, Any]]:
    return _read("delivery_collection_status")


def automation_status(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "browser-automation",
        "status": "available",
        "message": "계정 기반 수집 API와 CSV 정산서 가져오기를 사용할 수 있습니다.",
        "checked_at": _now(),
    }


def sync_delivery(payload: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="자동 수집 실행 권한이 없습니다")
    services = payload.get("services") or []
    all_accounts = _read("platform_accounts")
    if _migrate_platform_account_secrets(all_accounts):
        _write("platform_accounts", all_accounts)
    accounts = [row for row in all_accounts if not services or row.get("service") in services]
    synced_at = _now()
    summary = []
    for account in accounts:
        summary.append(
            {
                "service": account.get("service"),
                "status": "portal_action_required",
                "portal_status": "portal_action_required",
                "portal_message": "포털 직접 로그인/2차 인증 또는 정산 CSV 업로드가 필요합니다.",
                "message": "계정은 등록되어 있으나 서버 헤드리스 포털 수집은 확인이 필요합니다.",
            }
        )
    return {"synced_at": synced_at, "records": [], "settlements": list_settlements(user), "summary": summary}


def import_settlement_csv(text: str, user: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="정산서 가져오기 권한이 없습니다")
    rows = _read("delivery_settlements")
    reader = csv.DictReader(text.splitlines())
    imported = []
    now = _now()
    for raw in reader:
        record = {str(k or "").strip(): v for k, v in raw.items()}
        record.setdefault("id", str(uuid4()))
        record.setdefault("created_at", now)
        imported.append(record)
    rows = imported + rows
    _write("delivery_settlements", rows)
    return {"imported": len(imported), "settlements": imported}


def reset_data_for_tests() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
