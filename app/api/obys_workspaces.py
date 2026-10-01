"""Tenant-safe, database-backed workspace API for the OBYS V4.1 UI.

The V4.1 screen has many route-specific views, but the durable source remains
the existing OBYS ledgers.  This adapter exposes a stable read contract without
copying data into a second set of tables or falling back to demo rows.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
import json
import math
import re
from typing import Any, Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.api import acct_purchase, acct_source_ledger, acct_source_documents
from app.auth import get_current_user
from app.services import obys_upload_service as upload_svc


router = APIRouter(prefix="/workspaces", tags=["obys-workspaces"])


class WorkspaceManualRecord(BaseModel):
    model_config = {"extra": "forbid"}
    occurred_on: str = Field(min_length=10, max_length=10)
    counterparty: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=500)
    supply_amount: Decimal = Field(default=Decimal("0"), ge=0)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0)
    total_amount: Decimal = Field(gt=0)
    card_last4: str = Field(default="", max_length=4)
    direction: str = Field(default="in", pattern=r"^(in|out)$")
    account_label: str = Field(default="", max_length=100)
    ledger_category: Literal["sales", "purchase"] | None = None

ROUTES = frozenset(
    {
        "home", "tasks", "reports", "documents", "sales", "orders", "cards",
        "settlements", "accounts", "receivables", "unmatched", "purchases",
        "suppliers", "inventory", "purchase-orders", "employees", "attendance",
        "payroll", "hr-docs", "tax-evidence", "tax-gap", "journals",
        "tax-returns", "approvals", "audit-history", "alerts", "errors",
        "businesses", "branches", "margins", "menu-costs", "integrations",
        "sales-connect", "bank-connect", "tax-connect", "access-log",
    }
)

LEDGER_ROUTES = {
    "sales": "sales",
    "orders": "sales",
    "settlements": "sales",
    "purchases": "purchase",
    "suppliers": "purchase",
    "tax-evidence": "purchase",
    "tax-gap": "purchase",
}

# ACCT source categories retain their own evidence and transaction semantics.
ACCT_SOURCE_ROUTES = {
    "sales": "sales",
    "purchases": "purchase",
    "suppliers": "purchase",
    "cards": "card",
    "accounts": "bank",
    "tax-evidence": "evidence",
}
BANK_ROUTES = frozenset({"accounts", "receivables", "unmatched"})
CARD_ROUTES = frozenset({"cards"})
JOURNAL_ROUTES = frozenset({"journals"})

GENERIC_ROUTE_SQL: dict[str, tuple[str, str]] = {
    "tasks": ("SELECT id,title,description,requested_by,status,priority,created_at FROM yeoljeong_approvals WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500", "yeoljeong_approvals"),
    "reports": ("SELECT id,report_type,period_start,period_end,status,total_sales,total_purchases,vat_payable,tax_amount,created_at FROM yeoljeong_tax_reports WHERE business_id=$1 ORDER BY period_end DESC LIMIT 500", "yeoljeong_tax_reports"),
    "documents": ("SELECT id,category,original_filename,status,imported_rows,duplicate_rows,rejected_rows,created_by,created_at FROM yeoljeong_uploads WHERE business_id=$1 AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 500", "yeoljeong_uploads"),
    "inventory": ("SELECT id,branch_id,name,category,unit,current_stock,min_stock,unit_cost,supplier,updated_at FROM yeoljeong_inventory_items WHERE business_id=$1 ORDER BY name LIMIT 500", "yeoljeong_inventory_items"),
    "purchase-orders": ("SELECT id,branch_id,order_date,supplier,status,total_amount,items,received_at,invoice_number,created_at FROM yeoljeong_purchase_orders WHERE business_id=$1 ORDER BY order_date DESC LIMIT 500", "yeoljeong_purchase_orders"),
    "employees": ("SELECT id,employee_name,employee_email_masked,business_id,branch,role,status,requested_at,reviewed_at FROM yeoljeong_employee_join_requests WHERE business_id=$1 AND deleted_at IS NULL ORDER BY requested_at DESC LIMIT 500", "yeoljeong_employee_join_requests"),
    "attendance": ("SELECT id,employee_name,branch,work_date,start_at,end_at,break_minutes,worked_minutes,hourly_wage,status,memo,created_at,updated_at FROM yeoljeong_attendance_records WHERE business_id=$1 AND deleted_at IS NULL ORDER BY work_date DESC LIMIT 500", "yeoljeong_attendance_records"),
    "payroll": ("SELECT id,employee_name,branch,payroll_month,gross_pay,tax_withholding,insurance_deduction,other_deduction,net_pay,status,created_at FROM yeoljeong_payroll_statements WHERE business_id=$1 AND deleted_at IS NULL ORDER BY payroll_month DESC LIMIT 500", "yeoljeong_payroll_statements"),
    "hr-docs": ("SELECT id,employee_name,branch,document_type,document_label,status,original_filename,issue_date,uploaded_at,expires_at,superseded_by FROM yeoljeong_onboarding_documents WHERE business_id=$1 AND deleted_at IS NULL ORDER BY uploaded_at DESC LIMIT 500", "yeoljeong_onboarding_documents"),
    "tax-returns": ("SELECT id,report_type,period_start,period_end,status,total_sales,total_purchases,vat_payable,tax_amount,submitted_at,created_at FROM yeoljeong_tax_reports WHERE business_id=$1 ORDER BY period_end DESC LIMIT 500", "yeoljeong_tax_reports"),
    "approvals": ("SELECT id,approval_type,reference_id,title,description,requested_by,status,priority,created_at FROM yeoljeong_approvals WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500", "yeoljeong_approvals"),
    "audit-history": ("SELECT id,actor,action,resource_type,resource_id,details,created_at FROM yeoljeong_audit_logs WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500", "yeoljeong_audit_logs"),
    "alerts": ("SELECT id,notification_type,title,body,reference_type,reference_id,is_read,created_at FROM yeoljeong_notifications WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500", "yeoljeong_notifications"),
    "branches": ("SELECT id,name,status,address,sort_order,updated_at FROM yeoljeong_branches WHERE business_id=$1 AND deleted_at IS NULL ORDER BY sort_order,id LIMIT 500", "yeoljeong_branches"),
    "margins": ("SELECT id,category,occurred_on,counterparty,description,total_amount,source,created_at FROM yeoljeong_manual_ledger_entries WHERE business_id=$1 AND deleted_at IS NULL ORDER BY occurred_on DESC LIMIT 500", "yeoljeong_manual_ledger_entries"),
    "menu-costs": ("SELECT id,name,category,unit,current_stock,unit_cost,supplier,updated_at FROM yeoljeong_inventory_items WHERE business_id=$1 ORDER BY name LIMIT 500", "yeoljeong_inventory_items"),
    "integrations": ("SELECT id,bank_name,account_number_masked,account_alias,connection_type,status,last_synced_at,last_sync_status,last_sync_transaction_count,updated_at FROM yeoljeong_bank_accounts WHERE business_id=$1 ORDER BY updated_at DESC LIMIT 500", "yeoljeong_bank_accounts"),
    "sales-connect": ("SELECT row_id AS id,branch,payload,updated_at FROM yeoljeong_platform_accounts WHERE business_id=$1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 500", "yeoljeong_platform_accounts"),
    "bank-connect": ("SELECT id,bank_name,account_number_masked,account_alias,connection_type,status,last_synced_at,last_sync_status,last_sync_transaction_count,updated_at FROM yeoljeong_bank_accounts WHERE business_id=$1 ORDER BY updated_at DESC LIMIT 500", "yeoljeong_bank_accounts"),
    "tax-connect": ("SELECT id,report_type,period_start,period_end,status,submitted_at,updated_at FROM yeoljeong_tax_reports WHERE business_id=$1 ORDER BY updated_at DESC LIMIT 500", "yeoljeong_tax_reports"),
    "access-log": ("SELECT id,actor,action,resource_type,resource_id,details,created_at FROM yeoljeong_audit_logs WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500", "yeoljeong_audit_logs"),
    "errors": ("SELECT row_id AS id,branch,payload,updated_at FROM yeoljeong_delivery_collection_status WHERE business_id=$1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 500", "yeoljeong_delivery_collection_status"),
}


def _import_category(route: str) -> str:
    if route in {"sales", "orders", "settlements"}:
        return "sales"
    if route in {"purchases", "suppliers", "tax-evidence", "tax-gap"}:
        return "purchase"
    if route in CARD_ROUTES:
        return "card"
    if route in BANK_ROUTES:
        return "transaction"
    raise HTTPException(status_code=409, detail="이 화면은 파일 원장 등록 대상이 아닙니다")


async def _read_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > upload_svc.MAX_BYTES:
            raise HTTPException(status_code=413, detail="파일은 10MB 이하여야 합니다")
        chunks.append(chunk)
    return b"".join(chunks)


def _route(value: str) -> str:
    route = str(value or "").strip()
    if route not in ROUTES:
        raise HTTPException(status_code=404, detail="지원하지 않는 오비서 화면입니다")
    return route


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime, time, UUID, Decimal)):
        return str(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


_SENSITIVE_RAW_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|private|credential|"
    r"registration_no|resident|ssn|phone|email|account_number(?!_masked))",
    re.IGNORECASE,
)


def _public_raw(value: Any) -> Any:
    """Serialize DB source rows without returning credentials or identifiers."""
    if isinstance(value, dict):
        return {
            str(key): "[MASKED]" if _SENSITIVE_RAW_KEY.search(str(key)) else _public_raw(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_raw(item) for item in value]
    return _json_value(value)


def _amount(row: dict[str, Any]) -> Decimal:
    for key in ("total_amount", "amount", "debit_total", "credit_total", "source_total_amount", "supply_amount"):
        if row.get(key) is None:
            continue
        try:
            value = Decimal(str(row[key]))
            return -abs(value) if row.get("direction") == "out" else value
        except (ValueError, TypeError):
            continue
    return Decimal("0")


def _date_text(row: dict[str, Any]) -> str:
    for key in ("occurred_at", "occurred_on", "transaction_date", "work_date", "created_at"):
        if row.get(key):
            value = row[key]
            if key == "occurred_at":
                try:
                    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        return parsed.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
                except ValueError:
                    pass
            return str(value)[:10]
    return ""


def _filter_source_dates(
    rows: list[tuple[str, dict[str, Any]]],
    date_from: str | None,
    date_to: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="조회 날짜 형식이 올바르지 않습니다") from exc
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다")
    if not start and not end:
        return rows
    filtered: list[tuple[str, dict[str, Any]]] = []
    for kind, row in rows:
        row_date_text = _date_text(row)
        if not row_date_text:
            filtered.append((kind, row))
            continue
        try:
            row_date = date.fromisoformat(row_date_text)
        except ValueError:
            continue
        if start and row_date < start:
            continue
        if end and row_date > end:
            continue
        filtered.append((kind, row))
    return filtered


def _employee_label(kind: str, value: dict[str, Any]) -> str:
    name = str(value.get("employee_name") or "")
    masked = str(value.get("employee_email_masked") or "")
    return f"{name} ({masked})" if kind == "employees" and name and masked else name


def _record(kind: str, row: dict[str, Any], business_name: str) -> dict[str, Any]:
    value = {key: _json_value(item) for key, item in row.items()}
    record_id = str(value.get("id") or value.get("voucher_no") or "")
    amount = _amount(row)
    status = str(value.get("status") or value.get("source") or "저장됨")
    counterparty = str(value.get("counterparty") or value.get("merchant") or "")
    description = str(value.get("description") or value.get("memo") or counterparty)
    display: dict[str, Any] = {
        "사업자": business_name,
        "일자": _date_text(row),
        "금액": f"{amount:,.0f}원",
        "상태": status,
        "거래처": counterparty,
        "매입처": counterparty,
        "입금처": counterparty,
        "담당자": str(value.get("created_by") or ""),
        "업무": str(value.get("title") or value.get("description") or value.get("action") or ""),
        "문서": str(value.get("original_filename") or value.get("document_label") or ""),
        "직원": _employee_label(kind, value),
        "지점": str(value.get("branch") or value.get("branch_id") or ""),
        "품목": str(value.get("name") or value.get("description") or ""),
        "수량": str(value.get("current_stock") or value.get("worked_minutes") or ""),
        "세목": str(value.get("report_type") or ""),
        "신고기간": " ~ ".join(filter(None, (str(value.get("period_start") or ""), str(value.get("period_end") or "")))),
        "세액": f"{Decimal(str(value.get('tax_amount') or value.get('vat_payable') or 0)):,.0f}원",
        "요청번호": record_id,
        "이벤트번호": record_id,
        "알림번호": record_id,
        "오류번호": record_id,
        "로그번호": record_id,
        "지점코드": record_id,
        "품목코드": record_id,
        "추천번호": record_id,
        "급여번호": record_id,
        "근태번호": record_id,
        "문서번호": record_id,
        "연동번호": record_id,
    }
    if kind == "ledger":
        category = str(value.get("category") or "")
        prefix = "SALE" if category == "sales" else "BUY"
        display.update(
            {
                "주문번호": record_id or prefix,
                "매입번호": record_id or prefix,
                "매출처·채널": counterparty or "직접 등록",
                "주문·취소": description or "매출",
                "품목": description,
                "증빙·지급": "등록 원장",
                "증빙": "등록 원장",
                "공급가": f"{Decimal(str(value.get('supply_amount') or amount)):,.0f}원",
                "세액": f"{Decimal(str(value.get('tax_amount') or 0)):,.0f}원",
            }
        )
    elif kind == "acct-source":
        # 진아서버 ACCT 원천 전표. 계정과목을 추정하지 않고 위하고 원값을 그대로 쓴다.
        display.update(
            {
                "주문번호": record_id,
                "매입번호": record_id,
                "매출처·채널": counterparty or "거래처 미기재",
                "매입처": counterparty or "거래처 미기재",
                "주문·취소": description or str(value.get("detail_type_label") or ""),
                "품목": description,
                "구분": str(value.get("detail_type_label") or ""),
                "증빙": str(value.get("evidence_label") or ""),
                "증빙·지급": str(value.get("evidence_label") or ""),
                "차변계정": str(value.get("debit_account") or "검토 필요"),
                "대변계정": str(value.get("credit_account") or "검토 필요"),
                "사업자번호": str(value.get("counterparty_biz_no") or ""),
                "원천전표번호": str(value.get("voucher_seq") or record_id),
                "카드코드": str(value.get("card_code") or "미기재"),
                "계정과목": str(value.get("debit_account") or "확인 필요"),
                "분개": status,
                "주요품목": description,
                "카드사": str(value.get("card_name") or ("카드 마스터 충돌 · 확인 필요" if value.get("card_master_status") == "conflict" else "원천 카드코드 " + str(value.get("card_code") or "미기재"))),
                "카드번호": str(value.get("card_number_masked") or "번호 미제공"),
                "가맹점": counterparty,
                "승인·취소": status,
                "거래번호": str(value.get("voucher_seq") or record_id),
                "계좌": str(value.get("account_label") or "식별자 확인 필요"),
                "거래유형": (
                    ("입금" if value.get("direction") == "in" else "출금")
                    if value.get("category") == "bank"
                    else "해당 없음"
                ),
                "매칭·미매칭": "원천 조회 · 매칭 미실행",
                "증빙번호": record_id,
                "공급가액": f"{Decimal(str(value.get('supply_amount') or 0)):,.0f}원",
                "공급가": f"{Decimal(str(value.get('supply_amount') or 0)):,.0f}원",
                "세액": f"{Decimal(str(value.get('tax_amount') or 0)):,.0f}원",
            }
        )
    elif kind == "uploaded":
        category = str(value.get("category") or "")
        display.update(
            {
                "주문번호": record_id,
                "매입번호": record_id,
                "매출처·채널": counterparty or "파일 등록",
                "주문·취소": description,
                "품목": description,
                "증빙·지급": "업로드 원장",
                "증빙": "업로드 원장",
            }
        )
    elif kind == "card":
        display.update(
            {
                "승인번호": record_id,
                "카드사": "사업용 카드",
                "카드번호": str(value.get("card_number_masked") or ""),
                "가맹점": str(value.get("merchant") or ""),
                "승인·취소": "승인",
            }
        )
    elif kind == "bank":
        direction = str(value.get("direction") or "")
        display.update(
            {
                "거래번호": record_id,
                "계좌": str(value.get("account_label") or "등록 계좌"),
                "입금처": counterparty,
                "매칭·미매칭": "미매칭" if not value.get("category") else "분류됨",
                "거래유형": "입금" if direction == "in" else "출금",
            }
        )
    elif kind == "bank-account":
        status = {
            "needs_auth": "인증 필요", "error": "오류 · 확인 필요",
            "inactive": "비활성", "connected": "연결됨", "active": "활성",
        }.get(status, status)
        bank = str(value.get("bank_name") or "은행 미확인")
        masked = str(value.get("account_number_masked") or "번호 미등록")
        description = str(value.get("account_alias") or bank)
        last_success = (
            str(value.get("last_synced_at") or "성공 이력 없음")
            if value.get("last_sync_status") == "success" else "성공 이력 미확인"
        )
        display.update({
            "서비스": bank, "기관": bank, "계좌·카드": masked,
            "계좌": f"{bank} {masked}", "마지막성공": last_success,
            "수집건수": str(value.get("last_sync_transaction_count") or 0) + "건",
            "상태": status, "수집상태": status,
            "연동방식": "수동 등록" if value.get("connection_type") == "manual" else str(value.get("connection_type") or "미확인"),
        })
    elif kind == "journal":
        lines = value.get("lines") or []
        if isinstance(lines, str):
            try:
                lines = json.loads(lines)
            except json.JSONDecodeError:
                lines = []
        debit = next((line for line in lines if line.get("side") == "debit" or Decimal(str(line.get("debit") or 0)) > 0), {})
        credit = next((line for line in lines if line.get("side") == "credit" or Decimal(str(line.get("credit") or 0)) > 0), {})
        display.update(
            {
                "전표번호": str(value.get("voucher_no") or record_id),
                "전표일자": str(value.get("transaction_date") or "")[:10],
                "차변계정": str(debit.get("account_name") or debit.get("account_code") or "검토 필요"),
                "대변계정": str(credit.get("account_name") or credit.get("account_code") or "검토 필요"),
                "분개": status,
                "증빙번호": str(value.get("source_id") or record_id),
            }
        )
    return {
        "id": record_id,
        "kind": kind,
        "date": _date_text(row),
        "title": description or counterparty or record_id,
        "counterparty": counterparty,
        "amount": str(amount),
        "status": status,
        "display": display,
        "raw": _public_raw(row),
    }


async def _business(user: dict[str, Any], business_id: str) -> dict[str, Any]:
    businesses = await upload_svc.list_businesses(user=user)
    business = next((item for item in businesses if item["id"] == business_id), None)
    if not business:
        raise HTTPException(status_code=404, detail="현재 테넌트의 사업자를 찾을 수 없습니다")
    return business


async def _ledger_records(
    *, user: dict[str, Any], business_id: str, category: str,
    date_from: str | None, date_to: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    manual = await upload_svc.list_manual_entries(
        user=user, business_id=business_id, category=category,
        date_from=date_from, date_to=date_to, limit=None,
    )
    uploaded = await upload_svc.list_ledger_rows(
        user=user, business_id=business_id, category=category, limit=None,
        date_from=date_from, date_to=date_to,
    )
    rows: list[tuple[str, dict[str, Any]]] = [("ledger", row) for row in manual]
    rows.extend(("uploaded", row) for row in uploaded)
    return rows


async def _acct_journal_records(
    *, user: dict[str, Any], business_id: str,
    date_from: str | None, date_to: str | None,
) -> list[tuple[str, dict[str, Any]]] | None:
    """Read the canonical ACCT journal when this OBYS business has a mapping."""
    try:
        scope = await acct_purchase._authorized_acct_scope(user, business_id)
    except HTTPException as exc:
        if exc.status_code == 403:
            return None
        raise
    if scope is None:
        return None
    acct_tenant_id, company_id = scope
    conditions = [f"e.company_id = {int(company_id)}"]
    if date_from:
        conditions.append(f"e.entry_date >= {acct_purchase._lit(date_from)}::date")
    if date_to:
        conditions.append(f"e.entry_date <= {acct_purchase._lit(date_to)}::date")
    rows = await acct_purchase._fetch_acct_journals(
        "WITH entry_totals AS ("
        " SELECT e.id::text AS id, e.entry_date AS transaction_date,"
        " e.description, e.period_key, e.is_closed, e.source_ref AS source_id,"
        " e.created_at, coalesce(sum(l.debit),0) AS debit_total,"
        " coalesce(sum(l.credit),0) AS credit_total,"
        " jsonb_agg(jsonb_build_object('id',l.id::text,'account_code',l.account_code,"
        " 'debit',l.debit,'credit',l.credit,'note',l.note,'partner_code',l.partner_code)"
        " ORDER BY l.line_order,l.id) AS lines"
        " FROM journal_entry e LEFT JOIN journal_line l ON l.entry_id=e.id"
        " WHERE " + " AND ".join(conditions) +
        " GROUP BY e.id,e.entry_date,e.description,e.period_key,e.is_closed,e.source_ref,e.created_at"
        ") SELECT *, count(*) OVER() AS source_total_count,"
        " sum(greatest(debit_total,credit_total)) OVER() AS source_total_amount,"
        " CASE WHEN debit_total=credit_total THEN CASE WHEN is_closed THEN '마감' ELSE '균형' END"
        " ELSE '불균형' END AS status FROM entry_totals"
        " ORDER BY transaction_date DESC,created_at DESC LIMIT 500",
        acct_tenant_id,
    )
    return [("journal", row) for row in rows]


async def _local_source_rows(route, user, business_id, date_from, date_to):
    category = ACCT_SOURCE_ROUTES[route]
    if category in {"sales", "purchase"}:
        return await _ledger_records(user=user, business_id=business_id, category=category,
                                     date_from=date_from, date_to=date_to)
    if category == "evidence":
        rows = []
        for kind in ("sales", "purchase"):
            rows.extend(await _ledger_records(user=user, business_id=business_id, category=kind,
                                             date_from=date_from, date_to=date_to))
        return rows
    if category == "card":
        rows = await upload_svc.list_card_transactions(user=user, business_id=business_id,
                                                      date_from=date_from, date_to=date_to, limit=None)
    else:
        rows = await upload_svc.list_bank_transactions(user=user, business_id=business_id,
                                                      date_from=date_from, date_to=date_to, limit=None)
    uploaded = await upload_svc.list_ledger_rows(user=user, business_id=business_id,
                         category="card" if category == "card" else "transaction", limit=None,
                         date_from=date_from, date_to=date_to)
    return [(category, row) for row in rows] + [("uploaded", row) for row in uploaded]


async def _source_rows(
    *, route: str, user: dict[str, Any], business_id: str,
    date_from: str | None, date_to: str | None,
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    if route == "tax-gap":
        rows: list[tuple[str, dict[str, Any]]] = []
        for category in ("sales", "purchase"):
            rows.extend(await _ledger_records(
                user=user, business_id=business_id, category=category,
                date_from=date_from, date_to=date_to,
            ))
        cards = await upload_svc.list_card_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to, limit=None,
        )
        card_uploads = await upload_svc.list_ledger_rows(
            user=user, business_id=business_id, category="card", limit=None, date_from=date_from, date_to=date_to,
        )
        rows.extend(("card", row) for row in cards)
        rows.extend(("uploaded", row) for row in card_uploads)
        return rows, "obys_tax_evidence_sources"
    if route in ACCT_SOURCE_ROUTES:
        category = ACCT_SOURCE_ROUTES[route]
        ledger = await _local_source_rows(route, user, business_id, date_from, date_to)
        try:
            acct_rows, acct_source = await acct_source_ledger.source_transactions(
                user, business_id, category, date_from=date_from, date_to=date_to,
            )
        except HTTPException as exc:
            # ACCT 매핑이 없는 사업자(403)는 자체 원장만 본다. 그 밖의 실패는
            # 0건으로 감추지 않는다 — "매출 0원" 은 장애와 구분되지 않는다.
            if exc.status_code != 403:
                raise
            return ledger, "obys_ledger"
        rows: list[tuple[str, dict[str, Any]]] = [("acct-source", row) for row in acct_rows]
        rows.extend(ledger)
        if not acct_rows:
            return rows, f"{acct_source}:조회조건_전표없음+obys_ledger"
        return rows, f"{acct_source}+obys_ledger"
    if route in LEDGER_ROUTES:
        return await _ledger_records(
            user=user, business_id=business_id, category=LEDGER_ROUTES[route],
            date_from=date_from, date_to=date_to,
        ), "obys_ledger"
    if route in CARD_ROUTES:
        rows = await upload_svc.list_card_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to, limit=None,
        )
        uploaded = await upload_svc.list_ledger_rows(
            user=user, business_id=business_id, category="card", limit=None, date_from=date_from, date_to=date_to,
        )
        return [*(("card", row) for row in rows), *(("uploaded", row) for row in uploaded)], "obys_card_transactions"
    if route in BANK_ROUTES:
        rows = await upload_svc.list_bank_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to, limit=None,
        )
        uploaded = await upload_svc.list_ledger_rows(
            user=user, business_id=business_id, category="transaction", limit=None, date_from=date_from, date_to=date_to,
        )
        return [*(("bank", row) for row in rows), *(("uploaded", row) for row in uploaded)], "obys_bank_transactions"
    if route in JOURNAL_ROUTES:
        acct_rows = await _acct_journal_records(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to,
        )
        if acct_rows is not None:
            return acct_rows, "acct.journal_entry"
        rows = await upload_svc.list_journals(user=user, business_id=business_id)
        return [("journal", row) for row in rows], "obys_journal_vouchers"
    if route == "home":
        rows: list[tuple[str, dict[str, Any]]] = []
        for category in ("sales", "purchase"):
            rows.extend(await _ledger_records(
                user=user, business_id=business_id, category=category,
                date_from=date_from, date_to=date_to,
            ))
        cards = await upload_svc.list_card_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to, limit=None,
        )
        banks = await upload_svc.list_bank_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to, limit=None,
        )
        rows.extend(("card", row) for row in cards)
        rows.extend(("bank", row) for row in banks)
        return rows, "obys_aggregate"
    if route == "businesses":
        business = await _business(user, business_id)
        return [("generic", business)], "yeoljeong_businesses"
    if route in GENERIC_ROUTE_SQL:
        sql, source = GENERIC_ROUTE_SQL[route]
        connection = await upload_svc._connect()
        try:
            rows = await connection.fetch(sql, business_id)
            kind = "bank-account" if source == "yeoljeong_bank_accounts" else "generic"
            return [(kind, dict(row)) for row in rows], source
        finally:
            await connection.close()
    return [], "not_configured"


def _filter(records: list[dict[str, Any]], search: str, status: str) -> list[dict[str, Any]]:
    result = records
    if search:
        needle = search.casefold()
        result = [item for item in result if needle in str(item).casefold()]
    if status and status not in {"전체", "전체 상태"}:
        result = [item for item in result if status.casefold() in item["status"].casefold()]
    return result


@router.get("/businesses")
async def workspace_businesses(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await upload_svc.list_businesses(user=current_user)
    return {
        "businesses": [
            {
                "id": row["id"],
                "name": row["name"],
                "entity_type": row.get("entity_type") or "",
                "tax_type": row.get("tax_type") or "",
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.get("/{business_id}/{route}/summary")
async def workspace_summary(
    business_id: str,
    route: str,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    selected_route = _route(route)
    business = await _business(current_user, business_id)
    source_rows, source = await _source_rows(
        route=selected_route, user=current_user, business_id=business_id,
        date_from=date_from, date_to=date_to,
    )
    source_rows = _filter_source_dates(source_rows, date_from, date_to)
    records = [_record(kind, row, business["name"]) for kind, row in source_rows]
    if selected_route in {"bank-connect", "integrations"}:
        needs_attention = sum(row.get("status") in {"needs_auth", "error", "pending"}
                              for _, row in source_rows)
        return {
            "business": {"id": business["id"], "name": business["name"]},
            "route": selected_route,
            "metrics": [
                {"label": "등록 계좌", "value": f"{len(records):,}건"},
                {"label": "인증·확인 필요", "value": f"{needs_attention:,}건"},
                {"label": "데이터 원천", "value": source},
            ],
            "source": {"name": source, "live": source != "not_configured", "record_count": len(records)},
        }
    source_count = int(source_rows[0][1].get("source_total_count") or len(records)) if source_rows else 0
    total = (
        Decimal(str(source_rows[0][1].get("source_total_amount") or 0))
        if source_rows and source_rows[0][1].get("source_total_amount") is not None
        else sum((Decimal(item["amount"]) for item in records), Decimal("0"))
    )
    if selected_route in ACCT_SOURCE_ROUTES:
        # The SQL window totals cover all remote rows before its list limit.
        # Local manual/uploaded rows are an additional, separately owned source.
        remote = [row for kind, row in source_rows if kind == "acct-source"]
        local = [row for kind, row in source_rows if kind != "acct-source"]
        if remote and remote[0].get("source_total_count") is not None:
            source_count = int(remote[0]["source_total_count"]) + len(local)
            total = Decimal(str(remote[0]["source_total_amount"] or 0)) + sum(
                (_amount(row) for row in local), Decimal("0")
            )
    confirmed = sum((_amount(row) for kind,row in source_rows if kind != "acct-source" and row.get("status") == "확정"), Decimal("0"))
    remote_rows = [row for kind,row in source_rows if kind == "acct-source"]
    if remote_rows:
        confirmed += Decimal(str(remote_rows[0].get("source_confirmed_amount") or 0))
    review = sum(1 for item in records if any(token in item["status"].lower() for token in ("review", "pending", "대기", "필요", "미확인", "보류")))
    if remote_rows and remote_rows[0].get("source_review_count") is not None:
        review = int(remote_rows[0]["source_review_count"]) + sum(
            1 for kind,row in source_rows if kind != "acct-source" and any(
                word in str(row.get("status") or "") for word in ("미확인","보류","필요","pending","review")))
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "route": selected_route,
        "metrics": [
            {"label": "DB 건수", "value": f"{source_count:,}건"},
            {"label": "원천 순입출금" if selected_route == "accounts" else "원천 합계(제외·보류 포함)", "value": f"{total:,.0f}원"},
            *([{"label": "확정 원천 금액", "value": f"{confirmed:,.0f}원"}] if selected_route in ACCT_SOURCE_ROUTES else []),
            {"label": "확인 필요", "value": f"{review:,}건"},
            {"label": "데이터 원천", "value": source},
        ],
        "source": {"name": source, "live": source != "not_configured", "record_count": source_count},
    }


@router.get("/{business_id}/source-coverage")
async def workspace_source_coverage(
    business_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Expose source coverage without pretending every menu uses the ACCT DB."""
    business = await _business(current_user, business_id)
    coverage = await acct_source_ledger.source_coverage(current_user, business_id)
    coverage["business"] = {"id": business["id"], "name": business["name"]}
    coverage["routes"] = {
        route: {
            "source": (
                "acct_wehago+obys_ledger" if route in ACCT_SOURCE_ROUTES
                else "acct_journal" if route in JOURNAL_ROUTES
                else "obys_only"
            ),
            "acct_connected": route in ACCT_SOURCE_ROUTES or route in JOURNAL_ROUTES,
        }
        for route in sorted(ROUTES)
    }
    return coverage


@router.get("/{business_id}/source-documents")
async def workspace_source_documents(
    business_id: str, domain: str = Query("", max_length=40),
    offset: Annotated[int, Query(ge=0)] = 0, limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    await _business(current_user, business_id)
    return await acct_source_documents.source_documents(current_user, business_id, domain, offset, limit)


@router.get("/{business_id}/source-documents/{file_id}/sheets/{sheet_id}")
async def workspace_source_sheet(
    business_id: str, file_id: int, sheet_id: int,
    after_row: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    await _business(current_user, business_id)
    return await acct_source_documents.source_sheet(current_user, business_id, file_id, sheet_id, after_row, limit)


@router.get("/{business_id}/source-documents/{file_id}/snapshot")
async def workspace_source_snapshot(
    business_id: str, file_id: int, after_record: int = Query(-1, ge=-1),
    limit: int = Query(50, ge=1, le=100), current_user: dict = Depends(get_current_user),
):
    await _business(current_user, business_id)
    return await acct_source_documents.source_snapshot(current_user, business_id, file_id, after_record, limit)


# ── 근태 쓰기 ─────────────────────────────────────────────────────────────
# 읽기(목록)는 GENERIC_ROUTE_SQL["attendance"] 를 그대로 쓴다. 아래 라우트는
# 제네릭 `/{business_id}/{route}/records` 보다 먼저 등록되어야 먼저 매칭된다.
#
# 운영 스키마(2026-09-30 조회): employee_email NOT NULL 이고
# UNIQUE (employee_email, work_date, start_at) 가 사업자 구분 없이 걸려 있다.
# 이메일 없는 직원은 사업자·이름으로 키를 만들어 다른 사업자와 겹치지 않게 한다.
ATTENDANCE_STATUSES = frozenset({"pending", "approved", "rejected"})
ATTENDANCE_COLUMNS = (
    "id,employee_name,branch,work_date,start_at,end_at,break_minutes,"
    "worked_minutes,hourly_wage,status,memo,created_at,updated_at"
)
_ATTENDANCE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ATTENDANCE_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AttendanceRecordIn(BaseModel):
    # worked_minutes 등 클라이언트 계산값은 받지 않고 버린다 — 서버가 계산한다.
    model_config = {"extra": "ignore"}
    work_date: str
    employee_name: str = Field(min_length=1, max_length=100)
    employee_email: str = Field(default="", max_length=320)
    branch: str = Field(default="", max_length=100)
    start_at: str
    end_at: str
    break_minutes: int = 0
    hourly_wage: int = 0
    status: str = "pending"
    memo: str = Field(default="", max_length=500)


def attendance_worked_minutes(start_at: time, end_at: time, break_minutes: int) -> int:
    start = start_at.hour * 60 + start_at.minute
    end = end_at.hour * 60 + end_at.minute
    if end == start:
        raise HTTPException(status_code=400, detail="시작과 종료 시각이 같습니다")
    span = end - start
    if span < 0:
        span += 24 * 60  # 자정 넘김
    worked = span - break_minutes
    if worked < 0:
        raise HTTPException(status_code=400, detail="휴게시간이 근무시간보다 깁니다")
    if worked > 24 * 60:
        raise HTTPException(status_code=400, detail="근무시간은 24시간을 넘을 수 없습니다")
    return worked


def _attendance_values(business_id: str, payload: AttendanceRecordIn) -> dict[str, Any]:
    if not _ATTENDANCE_DATE.match(payload.work_date):
        raise HTTPException(status_code=400, detail="근무일은 YYYY-MM-DD 형식이어야 합니다")
    try:
        work_date = date.fromisoformat(payload.work_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="근무일이 올바른 날짜가 아닙니다") from exc
    for value in (payload.start_at, payload.end_at):
        if not _ATTENDANCE_TIME.match(value):
            raise HTTPException(status_code=400, detail="시각은 HH:MM 형식이어야 합니다")
    start_at = time.fromisoformat(payload.start_at)
    end_at = time.fromisoformat(payload.end_at)
    if payload.break_minutes < 0:
        raise HTTPException(status_code=400, detail="휴게시간은 0분 이상이어야 합니다")
    if payload.hourly_wage < 0:
        raise HTTPException(status_code=400, detail="시급은 0원 이상이어야 합니다")
    if payload.status not in ATTENDANCE_STATUSES:
        raise HTTPException(status_code=400, detail="근태 상태는 pending·approved·rejected 중 하나여야 합니다")
    employee_name = payload.employee_name.strip()
    if not employee_name:
        raise HTTPException(status_code=400, detail="직원명을 입력하십시오")
    email = payload.employee_email.strip().lower()
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="직원 이메일 형식이 올바르지 않습니다")
    local, _, domain = email.partition("@")
    return {
        "employee_email": email or f"name:{business_id}:{employee_name}",
        "employee_email_masked": f"{local[:1]}***@{domain}" if email else "",
        "employee_name": employee_name,
        "branch": payload.branch.strip(),
        "work_date": work_date,
        "start_at": start_at,
        "end_at": end_at,
        "break_minutes": payload.break_minutes,
        "worked_minutes": attendance_worked_minutes(start_at, end_at, payload.break_minutes),
        "hourly_wage": payload.hourly_wage,
        "status": payload.status,
        "memo": payload.memo.strip(),
    }


async def _attendance_audit(connection, business_id: str, user: dict[str, Any],
                            action: str, record_id: str, details: dict[str, Any]) -> None:
    await connection.execute(
        """INSERT INTO yeoljeong_audit_logs (business_id,actor,action,resource_type,resource_id,details)
           VALUES ($1,$2,$3,'attendance',$4,$5::jsonb)""",
        business_id, upload_svc._actor(user), action, record_id,
        json.dumps(_json_value(details), ensure_ascii=False),
    )


def _attendance_response(business: dict[str, Any], row: Any) -> dict[str, Any]:
    return {"record": _record("generic", dict(row), business["name"]),
            "source": "yeoljeong_attendance_records"}


@router.post("/{business_id}/attendance/records", status_code=201)
async def create_attendance_record(
    business_id: str,
    payload: AttendanceRecordIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    business = await _business(current_user, business_id)
    values = _attendance_values(business_id, payload)
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            # 같은 키의 soft delete 행이 있으면 되살린다. 살아 있는 행과 겹치면 409.
            row = await connection.fetchrow(
                f"""INSERT INTO yeoljeong_attendance_records
                      (business_id,employee_email,employee_email_masked,employee_name,branch,
                       work_date,start_at,end_at,break_minutes,worked_minutes,hourly_wage,
                       source,status,memo,created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'manual',$12,$13,$14)
                    ON CONFLICT (employee_email,business_id,work_date,start_at) DO UPDATE SET
                      employee_email_masked=EXCLUDED.employee_email_masked,
                      employee_name=EXCLUDED.employee_name, branch=EXCLUDED.branch,
                      end_at=EXCLUDED.end_at, break_minutes=EXCLUDED.break_minutes,
                      worked_minutes=EXCLUDED.worked_minutes, hourly_wage=EXCLUDED.hourly_wage,
                      source='manual', status=EXCLUDED.status, memo=EXCLUDED.memo,
                      created_by=EXCLUDED.created_by, confirmed_by='', confirmed_at=NULL,
                      created_at=now(), updated_at=now(), deleted_at=NULL
                    WHERE yeoljeong_attendance_records.deleted_at IS NOT NULL
                      AND yeoljeong_attendance_records.business_id=EXCLUDED.business_id
                    RETURNING {ATTENDANCE_COLUMNS}""",
                business_id, values["employee_email"], values["employee_email_masked"],
                values["employee_name"], values["branch"], values["work_date"],
                values["start_at"], values["end_at"], values["break_minutes"],
                values["worked_minutes"], values["hourly_wage"], values["status"],
                values["memo"], upload_svc._actor(current_user),
            )
            if row is None:
                raise HTTPException(status_code=409, detail="같은 직원·근무일·시작시각 근태가 이미 있습니다")
            await _attendance_audit(connection, business_id, current_user, "attendance.create",
                                    str(row["id"]), dict(row))
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="같은 직원·근무일·시작시각 근태가 이미 있습니다") from exc
    finally:
        await connection.close()
    return _attendance_response(business, row)


@router.get("/{business_id}/attendance/records/{record_id}")
async def get_attendance_record(
    business_id: str,
    record_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    business = await _business(current_user, business_id)
    connection = await upload_svc._connect()
    try:
        row = await connection.fetchrow(
            f"""SELECT {ATTENDANCE_COLUMNS} FROM yeoljeong_attendance_records
                 WHERE id=$1 AND business_id=$2 AND deleted_at IS NULL""",
            record_id, business_id,
        )
    finally:
        await connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="현재 사업자의 근태를 찾을 수 없습니다")
    return {**_attendance_response(business, row), "route": "attendance"}


@router.put("/{business_id}/attendance/records/{record_id}")
async def update_attendance_record(
    business_id: str,
    record_id: str,
    payload: AttendanceRecordIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    business = await _business(current_user, business_id)
    values = _attendance_values(business_id, payload)
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            row = await connection.fetchrow(
                f"""UPDATE yeoljeong_attendance_records SET
                      employee_email=$3, employee_email_masked=$4, employee_name=$5, branch=$6,
                      work_date=$7, start_at=$8, end_at=$9, break_minutes=$10,
                      worked_minutes=$11, hourly_wage=$12, status=$13, memo=$14,
                      updated_at=now()
                    WHERE id=$1 AND business_id=$2 AND deleted_at IS NULL
                    RETURNING {ATTENDANCE_COLUMNS}""",
                record_id, business_id, values["employee_email"], values["employee_email_masked"],
                values["employee_name"], values["branch"], values["work_date"],
                values["start_at"], values["end_at"], values["break_minutes"],
                values["worked_minutes"], values["hourly_wage"], values["status"], values["memo"],
            )
            if row is None:
                raise HTTPException(status_code=404, detail="현재 사업자의 근태를 찾을 수 없습니다")
            await _attendance_audit(connection, business_id, current_user, "attendance.update",
                                    record_id, dict(row))
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="같은 직원·근무일·시작시각 근태가 이미 있습니다") from exc
    finally:
        await connection.close()
    return _attendance_response(business, row)


@router.delete("/{business_id}/attendance/records/{record_id}")
async def delete_attendance_record(
    business_id: str,
    record_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    await _business(current_user, business_id)
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            # 물리 삭제하지 않는다 — 급여 산정 근거가 되므로 이력을 남긴다.
            deleted = await connection.fetchval(
                """UPDATE yeoljeong_attendance_records SET deleted_at=now(), updated_at=now()
                    WHERE id=$1 AND business_id=$2 AND deleted_at IS NULL RETURNING id""",
                record_id, business_id,
            )
            if deleted is None:
                raise HTTPException(status_code=404, detail="현재 사업자의 근태를 찾을 수 없습니다")
            await _attendance_audit(connection, business_id, current_user, "attendance.delete",
                                    record_id, {"id": record_id})
    finally:
        await connection.close()
    return {"deleted": True, "id": record_id}


# ── PWA 출퇴근 (직원 본인) ──────────────────────────────────────────────────
# 버튼을 누른 순간의 위치 1점만 받는다. 연속 추적·오프라인 큐잉은 하지 않는다.
# 신원은 로그인 세션에서만 가져온다 — 본문의 employee_email 은 모델이 버린다.
# 관리자 대리 타각은 이 경로로 허용하지 않는다(관리자는 수기 입력 경로를 쓴다).
#
# 반경: CEO 결정 D1(2026-09-30, docs/prd/20260930_OBYS_ATTENDANCE_PWA_GPS_PRD.md §8.1)
# "매장 반경 50m 안", 정확도 여유 없음. 지점별 geofence_radius_m 이 있으면 그 값을
# 쓰되 상한을 넘지 않는다. 정확도(오차)가 반경보다 크면 안/밖을 판정하지 않는다.
#
# 퇴근 전 행: 운영 스키마의 end_at 이 NOT NULL 일 수 있어(확인 불가) 출근 시
# end_at=start_at, worked_minutes=0 으로 두고 check_out_at IS NULL 을 "근무 중" 표지로 쓴다.
GEOFENCE_DEFAULT_RADIUS_M = 50
GEOFENCE_MIN_RADIUS_M = 30
GEOFENCE_MAX_RADIUS_M = 50
GEOFENCE_RESULTS = frozenset({"inside", "outside", "unknown"})
ATTENDANCE_SOURCES = frozenset({"manual", "pwa"})
CLOCK_OPEN_WINDOW = timedelta(hours=24)  # 퇴근은 24시간 안의 열린 출근에만 붙는다
CLOCK_CONSENT_ACTIONS = ("attendance.consent", "attendance.consent_withdraw")
_KST = ZoneInfo("Asia/Seoul")
_EARTH_RADIUS_M = 6_371_008.8

# 직원 본인에게 돌려주는 열 — 좌표 원본·기기 정보는 넣지 않는다.
ATTENDANCE_CLOCK_COLUMNS = (
    ATTENDANCE_COLUMNS + ",source,check_in_at,check_out_at,check_in_accuracy_m,"
    "check_out_accuracy_m,check_in_distance_m,check_out_distance_m,geofence_result,"
    "location_consent_at"
)
# 관리자 위치 조회 전용 — 좌표 원본 포함.
ATTENDANCE_LOCATION_COLUMNS = (
    ATTENDANCE_CLOCK_COLUMNS + ",employee_email_masked,check_in_lat,check_in_lng,"
    "check_out_lat,check_out_lng,device_info"
)
_CLOCK_PRIVATE_KEYS = frozenset(
    {"employee_email", "check_in_lat", "check_in_lng", "check_out_lat", "check_out_lng", "device_info"}
)
_BRANCH_GEOFENCE_SQL = (
    "SELECT id,name,latitude,longitude,geofence_radius_m FROM yeoljeong_branches"
    " WHERE business_id=$1 AND deleted_at IS NULL ORDER BY sort_order,id"
)


class AttendanceClockIn(BaseModel):
    # employee_email·status 등 본문의 신원·판정 값은 받지 않고 버린다.
    model_config = {"extra": "ignore"}
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=1_000_000)
    device_info: str = Field(default="", max_length=300)
    location_consent: bool = False
    branch_id: str = Field(default="", max_length=64)


class AttendanceConsentIn(BaseModel):
    model_config = {"extra": "forbid"}
    agree: bool


class BranchGeofenceIn(BaseModel):
    model_config = {"extra": "forbid"}
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: int | None = None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def geofence_radius_m(branch: dict[str, Any] | None) -> int:
    value = (branch or {}).get("geofence_radius_m")
    if value is None or int(value) <= 0:
        return GEOFENCE_DEFAULT_RADIUS_M
    return min(int(value), GEOFENCE_MAX_RADIUS_M)


def judge_geofence(*, consent: bool, latitude: float | None, longitude: float | None,
                   accuracy_m: float | None, branch: dict[str, Any] | None) -> dict[str, Any]:
    """반경 판정. 확실히 안일 때만 inside/approved — 나머지는 전부 확인필요(pending)."""
    radius = geofence_radius_m(branch)
    verdict: dict[str, Any] = {
        "result": "unknown", "status": "pending", "radius_m": radius,
        "distance_m": None, "accuracy_m": None, "latitude": None, "longitude": None,
    }
    if not consent:
        return {**verdict, "reason": "위치정보 미동의 — 위치 미수집"}
    has_coords = latitude is not None and longitude is not None
    accuracy = None if accuracy_m is None else math.ceil(accuracy_m)
    if has_coords:
        verdict.update(latitude=latitude, longitude=longitude, accuracy_m=accuracy)
    if not branch or branch.get("latitude") is None or branch.get("longitude") is None:
        return {**verdict, "reason": "지점 좌표 미등록 — 관리자 확인 필요"}
    if not has_coords:
        return {**verdict, "reason": "위치 측정 실패(권한 거부·시간 초과)"}
    # 올림: 반경 경계에서 "안" 쪽으로 반올림하지 않는다.
    distance = math.ceil(haversine_m(latitude, longitude,
                                     float(branch["latitude"]), float(branch["longitude"])))
    verdict["distance_m"] = distance
    if accuracy is None:
        return {**verdict, "reason": f"GPS 정확도 정보 없음 — 매장에서 {distance}m"}
    if accuracy > radius:
        return {**verdict, "reason": f"GPS 오차 {accuracy}m 가 반경 {radius}m 보다 큼 — 판정 불가"}
    if distance <= radius:
        return {**verdict, "result": "inside", "status": "approved",
                "reason": f"매장에서 {distance}m (반경 {radius}m 안)"}
    return {**verdict, "result": "outside", "reason": f"매장에서 {distance}m (반경 {radius}m 밖)"}


def combined_geofence(check_in: str | None, check_out: str | None) -> str:
    if check_in == "inside" and check_out == "inside":
        return "inside"
    if "outside" in (check_in, check_out):
        return "outside"
    return "unknown"


def _clock_now() -> datetime:
    return datetime.now(_KST)


def _clock_identity(user: dict[str, Any]) -> dict[str, str]:
    # 테넌트 활성 멤버십(owner/admin/member) 판정은 기존 함수를 그대로 쓴다.
    upload_svc._require_write(user)
    email = str(user.get("email") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=403, detail="로그인 계정의 이메일이 있어야 출퇴근을 기록할 수 있습니다")
    local, _, domain = email.partition("@")
    return {"email": email, "masked": f"{local[:1]}***@{domain}",
            "name": str(user.get("name") or "").strip()[:100] or local}


async def _clock_business(user: dict[str, Any], business_id: str) -> dict[str, Any]:
    try:
        return await _business(user, business_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=403, detail="현재 테넌트의 사업자가 아니므로 출퇴근을 기록·조회할 수 없습니다") from exc
        raise


def _require_attendance_admin(user: dict[str, Any]) -> None:
    if not upload_svc.tenant_session_for_user(user)["permissions"]["can_manage_settings"]:
        raise HTTPException(status_code=403, detail="소유자·관리자만 출퇴근 위치를 조회·설정할 수 있습니다")


def _clock_view(row: Any) -> dict[str, Any]:
    value = {key: _json_value(item) for key, item in dict(row).items() if key not in _CLOCK_PRIVATE_KEYS}
    value["open"] = bool(value.get("check_in_at")) and not value.get("check_out_at")
    return value


def _pick_branch(branches: list[dict[str, Any]], branch_id: str,
                 branch_name: str | None) -> dict[str, Any] | None:
    if branch_id:
        match = next((item for item in branches if str(item["id"]) == branch_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="현재 사업자의 지점을 찾을 수 없습니다")
        return match
    if branch_name is not None:  # 퇴근: 출근 행에 적힌 지점만 본다(없으면 좌표 미등록과 같다)
        return next((item for item in branches if branch_name and item["name"] == branch_name), None)
    if len(branches) > 1:
        raise HTTPException(status_code=400, detail="출근할 지점을 선택하십시오")
    return branches[0] if branches else None


async def _clock_consent(connection, business_id: str, user: dict[str, Any]) -> dict[str, Any]:
    row = await connection.fetchrow(
        """SELECT action,created_at FROM yeoljeong_audit_logs
            WHERE business_id=$1 AND actor=$2 AND resource_type='attendance'
              AND action = ANY($3::text[])
            ORDER BY created_at DESC LIMIT 1""",
        business_id, upload_svc._actor(user), list(CLOCK_CONSENT_ACTIONS),
    )
    agreed = row is not None and row["action"] == "attendance.consent"
    return {"agreed": agreed, "at": row["created_at"] if agreed else None}


async def _clock_judge(connection, business_id: str, user: dict[str, Any],
                       payload: AttendanceClockIn, *, branch_id: str, branch_name: str | None):
    branches = [dict(item) for item in await connection.fetch(_BRANCH_GEOFENCE_SQL, business_id)]
    branch = _pick_branch(branches, branch_id, branch_name)
    consent = await _clock_consent(connection, business_id, user)
    # 서버에 남은 동의와 이번 요청의 동의가 둘 다 있어야 위치를 저장한다(철회 우선).
    agreed = bool(payload.location_consent and consent["agreed"])
    verdict = judge_geofence(consent=agreed, latitude=payload.latitude, longitude=payload.longitude,
                             accuracy_m=payload.accuracy_m, branch=branch)
    return branch, (consent["at"] if agreed else None), verdict


async def _clock_lock(connection, business_id: str, email: str) -> None:
    # 두 번 누름·두 기기 동시 요청이 같은 직원의 열린 출근을 두 개 만들지 않게 한다.
    await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"obys-clock:{business_id}:{email}")


def _clock_memo(previous: str | None, label: str, reason: str) -> str:
    text = f"{label}: {reason}"
    return (f"{previous} / {text}" if previous else text)[:500]


def _clock_audit(verdict: dict[str, Any], branch: dict[str, Any] | None) -> dict[str, Any]:
    # 감사로그에는 직원 좌표 원문을 남기지 않는다 — 판정·거리·정확도만.
    return {key: verdict[key] for key in ("result", "status", "distance_m", "accuracy_m", "radius_m", "reason")} | {
        "branch": (branch or {}).get("name") or ""}


def _clock_response(business: dict[str, Any], row: Any, verdict: dict[str, Any]) -> dict[str, Any]:
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "record": _clock_view(row),
        "judgement": {key: verdict[key] for key in ("result", "status", "distance_m", "accuracy_m", "radius_m", "reason")},
        "source": "yeoljeong_attendance_records",
    }


@router.get("/{business_id}/attendance/clock/state")
async def attendance_clock_state(
    business_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    identity = _clock_identity(current_user)
    business = await _clock_business(current_user, business_id)
    now = _clock_now()
    connection = await upload_svc._connect()
    try:
        branches = [dict(item) for item in await connection.fetch(_BRANCH_GEOFENCE_SQL, business_id)]
        consent = await _clock_consent(connection, business_id, current_user)
        rows = await connection.fetch(
            f"""SELECT {ATTENDANCE_CLOCK_COLUMNS} FROM yeoljeong_attendance_records
                 WHERE business_id=$1 AND employee_email=$2 AND deleted_at IS NULL
                 ORDER BY work_date DESC, start_at DESC LIMIT 31""",
            business_id, identity["email"],
        )
    finally:
        await connection.close()
    open_row = next((row for row in rows if row["source"] == "pwa" and row["check_out_at"] is None
                     and row["check_in_at"] is not None and row["check_in_at"] >= now - CLOCK_OPEN_WINDOW), None)
    recent = [_clock_view(row) for row in rows]
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "employee": {"name": identity["name"], "email_masked": identity["masked"]},
        "consent": _json_value(consent),
        "open_record": _clock_view(open_row) if open_row is not None else None,
        "today": [item for item in recent if item["work_date"] == now.date().isoformat()],
        "recent": recent,
        "branches": [
            {"id": str(item["id"]), "name": item["name"],
             "has_geofence": item.get("latitude") is not None and item.get("longitude") is not None,
             "radius_m": geofence_radius_m(item)}
            for item in branches
        ],
        "policy": {"default_radius_m": GEOFENCE_DEFAULT_RADIUS_M, "max_radius_m": GEOFENCE_MAX_RADIUS_M},
        "server_time": now.isoformat(),
    }


@router.post("/{business_id}/attendance/consent")
async def attendance_location_consent(
    business_id: str,
    payload: AttendanceConsentIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    identity = _clock_identity(current_user)
    await _clock_business(current_user, business_id)
    action = CLOCK_CONSENT_ACTIONS[0] if payload.agree else CLOCK_CONSENT_ACTIONS[1]
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            await _attendance_audit(connection, business_id, current_user, action,
                                    f"consent:{identity['masked']}",
                                    {"agree": payload.agree, "scope": "출퇴근 버튼을 누른 순간 위치 1점",
                                     "purpose": "근태 확인"})
    finally:
        await connection.close()
    return {"consent": {"agreed": payload.agree, "at": _clock_now().isoformat() if payload.agree else None}}


@router.post("/{business_id}/attendance/check-in", status_code=201)
async def attendance_check_in(
    business_id: str,
    payload: AttendanceClockIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    identity = _clock_identity(current_user)
    business = await _clock_business(current_user, business_id)
    now = _clock_now()
    start_at = time(now.hour, now.minute)
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            await _clock_lock(connection, business_id, identity["email"])
            branch, consent_at, verdict = await _clock_judge(
                connection, business_id, current_user, payload,
                branch_id=payload.branch_id.strip(), branch_name=None)
            already_open = await connection.fetchval(
                """SELECT id FROM yeoljeong_attendance_records
                    WHERE business_id=$1 AND employee_email=$2 AND work_date=$3 AND source='pwa'
                      AND check_in_at IS NOT NULL AND check_out_at IS NULL AND deleted_at IS NULL
                    LIMIT 1""",
                business_id, identity["email"], now.date(),
            )
            if already_open is not None:
                raise HTTPException(status_code=409, detail="오늘 이미 출근이 기록되어 있습니다. 퇴근을 먼저 기록하십시오")
            row = await connection.fetchrow(
                f"""INSERT INTO yeoljeong_attendance_records
                      (business_id,employee_email,employee_email_masked,employee_name,branch,
                       work_date,start_at,end_at,break_minutes,worked_minutes,hourly_wage,
                       source,status,memo,created_by,check_in_at,check_in_lat,check_in_lng,
                       check_in_accuracy_m,check_in_distance_m,geofence_result,device_info,
                       location_consent_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$7,0,0,0,'pwa',$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                    RETURNING {ATTENDANCE_CLOCK_COLUMNS}""",
                business_id, identity["email"], identity["masked"], identity["name"],
                (branch or {}).get("name") or "", now.date(), start_at, verdict["status"],
                _clock_memo("", "출근", verdict["reason"]), upload_svc._actor(current_user), now,
                verdict["latitude"], verdict["longitude"], verdict["accuracy_m"], verdict["distance_m"],
                verdict["result"], payload.device_info.strip(), consent_at,
            )
            await _attendance_audit(connection, business_id, current_user, "attendance.check_in",
                                    str(row["id"]), _clock_audit(verdict, branch))
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="같은 시각의 출근이 이미 기록되어 있습니다") from exc
    finally:
        await connection.close()
    return _clock_response(business, row, verdict)


@router.post("/{business_id}/attendance/check-out")
async def attendance_check_out(
    business_id: str,
    payload: AttendanceClockIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    identity = _clock_identity(current_user)
    business = await _clock_business(current_user, business_id)
    now = _clock_now()
    end_at = time(now.hour, now.minute)
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            await _clock_lock(connection, business_id, identity["email"])
            record = await connection.fetchrow(
                f"""SELECT {ATTENDANCE_CLOCK_COLUMNS} FROM yeoljeong_attendance_records
                     WHERE business_id=$1 AND employee_email=$2 AND source='pwa'
                       AND check_in_at IS NOT NULL AND check_in_at >= $3
                       AND check_out_at IS NULL AND deleted_at IS NULL
                     ORDER BY check_in_at DESC LIMIT 1 FOR UPDATE""",
                business_id, identity["email"], now - CLOCK_OPEN_WINDOW,
            )
            if record is None:
                raise HTTPException(status_code=409, detail="출근 기록이 없어 퇴근을 기록할 수 없습니다")
            # 퇴근은 출근한 지점 기준으로 판정한다.
            branch, consent_at, verdict = await _clock_judge(
                connection, business_id, current_user, payload,
                branch_id="", branch_name=record["branch"] or "")
            # 근무분은 수기 입력과 같은 함수로 계산한다(자정 넘김·24시간 상한 동일).
            worked = attendance_worked_minutes(record["start_at"], end_at, int(record["break_minutes"] or 0))
            combined = combined_geofence(record["geofence_result"], verdict["result"])
            status = ("rejected" if record["status"] == "rejected"
                      else "approved" if combined == "inside" else "pending")
            row = await connection.fetchrow(
                f"""UPDATE yeoljeong_attendance_records SET
                      check_out_at=$3, end_at=$4, worked_minutes=$5, check_out_lat=$6,
                      check_out_lng=$7, check_out_accuracy_m=$8, check_out_distance_m=$9,
                      geofence_result=$10, status=$11, memo=$12,
                      device_info=COALESCE(NULLIF(device_info,''),$13),
                      location_consent_at=COALESCE(location_consent_at,$14), updated_at=now()
                    WHERE id=$1 AND business_id=$2 AND check_out_at IS NULL AND deleted_at IS NULL
                    RETURNING {ATTENDANCE_CLOCK_COLUMNS}""",
                record["id"], business_id, now, end_at, worked, verdict["latitude"],
                verdict["longitude"], verdict["accuracy_m"], verdict["distance_m"], combined, status,
                _clock_memo(record["memo"], "퇴근", verdict["reason"]), payload.device_info.strip(),
                consent_at,
            )
            if row is None:
                raise HTTPException(status_code=409, detail="이미 퇴근이 기록되었습니다")
            await _attendance_audit(connection, business_id, current_user, "attendance.check_out",
                                    str(row["id"]), _clock_audit(verdict, branch) | {"worked_minutes": worked})
    finally:
        await connection.close()
    return _clock_response(business, row, verdict)


@router.get("/{business_id}/attendance/me/{record_id}")
async def get_my_attendance_record(
    business_id: str,
    record_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    identity = _clock_identity(current_user)
    business = await _clock_business(current_user, business_id)
    connection = await upload_svc._connect()
    try:
        row = await connection.fetchrow(
            f"""SELECT employee_email,{ATTENDANCE_CLOCK_COLUMNS} FROM yeoljeong_attendance_records
                 WHERE id=$1 AND business_id=$2 AND deleted_at IS NULL""",
            record_id, business_id,
        )
    finally:
        await connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="현재 사업자의 근태를 찾을 수 없습니다")
    if str(row["employee_email"] or "").lower() != identity["email"]:
        raise HTTPException(status_code=403, detail="본인 근태만 조회할 수 있습니다")
    return {"business": {"id": business["id"], "name": business["name"]}, "record": _clock_view(row)}


@router.get("/{business_id}/attendance/locations")
async def attendance_locations(
    business_id: str,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    result: str = Query("", max_length=10),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """관리자 확인용 위치 요약. 좌표 원본은 이 경로에서만 나간다."""
    _require_attendance_admin(current_user)
    business = await _business(current_user, business_id)
    _filter_source_dates([], date_from, date_to)  # 날짜 형식·순서 검증
    if result and result not in GEOFENCE_RESULTS:
        raise HTTPException(status_code=400, detail="판정은 inside·outside·unknown 중 하나여야 합니다")
    conditions = ["business_id=$1", "source='pwa'", "deleted_at IS NULL"]
    params: list[Any] = [business_id]
    for column, op, value in (("work_date", ">=", date_from), ("work_date", "<=", date_to),
                              ("geofence_result", "=", result)):
        if value:
            params.append(date.fromisoformat(value) if column == "work_date" else value)
            conditions.append(f"{column} {op} ${len(params)}")
    connection = await upload_svc._connect()
    try:
        rows = await connection.fetch(
            f"SELECT {ATTENDANCE_LOCATION_COLUMNS} FROM yeoljeong_attendance_records"
            f" WHERE {' AND '.join(conditions)} ORDER BY work_date DESC, start_at DESC LIMIT 500",
            *params,
        )
    finally:
        await connection.close()
    records = [{key: _json_value(item) for key, item in dict(row).items()} for row in rows]
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "records": records,
        "count": len(records),
        "summary": {name: sum(1 for item in records if item.get("geofence_result") == name)
                    for name in sorted(GEOFENCE_RESULTS)}
                   | {"pending": sum(1 for item in records if item.get("status") == "pending")},
    }


@router.get("/{business_id}/branches/geofence")
async def list_branch_geofences(
    business_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _require_attendance_admin(current_user)
    business = await _business(current_user, business_id)
    connection = await upload_svc._connect()
    try:
        rows = await connection.fetch(_BRANCH_GEOFENCE_SQL, business_id)
    finally:
        await connection.close()
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "branches": [{**{key: _json_value(item) for key, item in dict(row).items()},
                      "effective_radius_m": geofence_radius_m(dict(row))} for row in rows],
        "policy": {"default_radius_m": GEOFENCE_DEFAULT_RADIUS_M, "min_radius_m": GEOFENCE_MIN_RADIUS_M,
                   "max_radius_m": GEOFENCE_MAX_RADIUS_M},
    }


@router.patch("/{business_id}/branches/{branch_id}/geofence")
async def update_branch_geofence(
    business_id: str,
    branch_id: str,
    payload: BranchGeofenceIn,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _require_attendance_admin(current_user)
    business = await _business(current_user, business_id)
    radius = GEOFENCE_DEFAULT_RADIUS_M if payload.radius_m is None else payload.radius_m
    if not GEOFENCE_MIN_RADIUS_M <= radius <= GEOFENCE_MAX_RADIUS_M:
        raise HTTPException(
            status_code=400,
            detail=f"반경은 {GEOFENCE_MIN_RADIUS_M}~{GEOFENCE_MAX_RADIUS_M}m 사이여야 합니다(CEO 결정 상한 {GEOFENCE_MAX_RADIUS_M}m)",
        )
    connection = await upload_svc._connect()
    try:
        async with connection.transaction():
            row = await connection.fetchrow(
                """UPDATE yeoljeong_branches SET latitude=$3, longitude=$4, geofence_radius_m=$5,
                          updated_by=$6, updated_at=now()
                    WHERE id=$1 AND business_id=$2 AND deleted_at IS NULL
                    RETURNING id,name,latitude,longitude,geofence_radius_m""",
                branch_id, business_id, payload.latitude, payload.longitude, radius,
                upload_svc._actor(current_user),
            )
            if row is None:
                raise HTTPException(status_code=404, detail="현재 사업자의 지점을 찾을 수 없습니다")
            await _attendance_audit(connection, business_id, current_user, "attendance.geofence_config",
                                    branch_id, dict(row))
    finally:
        await connection.close()
    return {"business": {"id": business["id"], "name": business["name"]},
            "branch": {key: _json_value(item) for key, item in dict(row).items()}}


@router.get("/{business_id}/{route}/records")
async def workspace_records(
    business_id: str,
    route: str,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    status: str = Query("전체 상태", max_length=80),
    search: str = Query("", max_length=200),
    limit: int = Query(200, ge=1, le=500),
    offset: Annotated[int, Query(ge=0)] = 0,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    selected_route = _route(route)
    business = await _business(current_user, business_id)
    if selected_route in ACCT_SOURCE_ROUTES:
        _filter_source_dates([], date_from, date_to)
        local_rows = await _local_source_rows(selected_route, current_user, business_id, date_from, date_to)
        local_rows = _filter_source_dates(local_rows, date_from, date_to)
        local = _filter([_record(k,r,business["name"]) for k,r in local_rows], search, status)
        records = local[offset:offset+limit]
        quota = limit - len(records)
        remote, source = [], "obys_ledger"
        if quota:
            try:
                remote, source = await acct_source_ledger.source_transactions(
                    current_user, business_id, ACCT_SOURCE_ROUTES[selected_route],
                    date_from=date_from, date_to=date_to, limit=quota,
                    offset=max(0,offset-len(local)), search=search, status=status,
                )
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
            records.extend(_record("acct-source",r,business["name"]) for r in remote)
        if quota and not remote and source != "obys_ledger":
            source += ":조회조건_전표없음"
        total = len(local) + (int(remote[0].get("source_total_count") or len(remote)) if remote else 0)
        has_more = (offset+len(records) < total) if remote else len(records)==limit
        return {"business": {"id":business["id"],"name":business["name"]},
                "route":selected_route,"records":records,"count":len(records),
                "offset":offset,"has_more":has_more,"next_offset":offset+len(records),
                "source":{"name":source+"+obys_ledger","live":True}}
    source_rows, source = await _source_rows(
        route=selected_route, user=current_user, business_id=business_id,
        date_from=date_from, date_to=date_to,
    )
    source_rows = _filter_source_dates(source_rows, date_from, date_to)
    records = _filter(
        [_record(kind, row, business["name"]) for kind, row in source_rows], search, status
    )[offset:offset+limit]
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "route": selected_route,
        "records": records,
        "count": len(records),
        "source": {"name": source, "live": source != "not_configured"},
    }


@router.get("/{business_id}/{route}/records/{record_id}")
async def workspace_record_detail(
    business_id: str,
    route: str,
    record_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    selected_route = _route(route)
    business = await _business(current_user, business_id)
    if selected_route in ACCT_SOURCE_ROUTES and record_id.startswith("acct:"):
        source_rows, source = await acct_source_ledger.source_transactions(
            current_user, business_id, ACCT_SOURCE_ROUTES[selected_route], record_id=record_id,
        )
        if not source_rows:
            raise HTTPException(status_code=404, detail="현재 사업자의 내역을 찾을 수 없습니다")
        return {
            "business": {"id": business["id"], "name": business["name"]},
            "route": selected_route,
            "record": _record("acct-source", source_rows[0], business["name"]),
            "source": {"name": source, "live": True},
        }
    if selected_route in LEDGER_ROUTES or selected_route in CARD_ROUTES or selected_route in BANK_ROUTES:
        try:
            local_id = UUID(record_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="현재 사업자의 내역을 찾을 수 없습니다") from exc
        categories = (["sales", "purchase"] if selected_route in {"tax-evidence", "tax-gap"}
                      else [_import_category(selected_route)])
        for category in categories:
            try:
                if category in {"sales", "purchase"}:
                    row = await upload_svc.get_manual_entry(user=current_user, category=category, entry_id=local_id)
                    kind = "ledger"
                elif category == "card":
                    row = await upload_svc.get_card_transaction(user=current_user, transaction_id=local_id)
                    kind = "card"
                else:
                    row = await upload_svc.get_bank_transaction(user=current_user, transaction_id=local_id)
                    kind = "bank"
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                row = await upload_svc.get_ledger_row(user=current_user, business_id=business_id,
                                                     category=category, entry_id=local_id)
                kind = "uploaded"
            if row and row.get("business_id") == business_id:
                return {"business": {"id": business["id"], "name": business["name"]},
                        "route": selected_route, "record": _record(kind, row, business["name"]),
                        "source": {"name": "obys_ledger", "live": True}}
        raise HTTPException(status_code=404, detail="현재 사업자의 내역을 찾을 수 없습니다")
    source_rows, source = await _source_rows(
        route=selected_route, user=current_user, business_id=business_id,
        date_from=None, date_to=None,
    )
    records = [_record(kind, row, business["name"]) for kind, row in source_rows]
    record = next((item for item in records if item["id"] == record_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="현재 사업자의 내역을 찾을 수 없습니다")
    return {
        "business": {"id": business["id"], "name": business["name"]},
        "route": selected_route,
        "record": record,
        "source": {"name": source, "live": source != "not_configured"},
    }


@router.post("/{business_id}/{route}/imports/preview")
async def preview_workspace_import(
    business_id: str,
    route: str,
    file: UploadFile = File(...),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    selected_route = _route(route)
    await _business(current_user, business_id)
    category = _import_category(selected_route)
    data = await _read_upload(file)
    preview = upload_svc.preview_upload(
        category=category,
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    tenant_id = upload_svc._tenant(current_user)
    connection = await upload_svc._connect()
    try:
        duplicate_count = await connection.fetchval(
            """SELECT COUNT(*) FROM yeoljeong_uploaded_ledger_rows
                 WHERE tenant_id=$1 AND business_id=$2 AND category=$3
                   AND source_hash=ANY($4::text[])""",
            tenant_id,
            business_id,
            category,
            preview.pop("source_hashes"),
        )
    finally:
        await connection.close()
    preview["duplicate_rows"] += int(duplicate_count or 0)
    preview["accepted_rows"] = max(
        0, int(preview.get("accepted_rows") or 0) - int(preview["duplicate_rows"])
    )
    return {"preview": preview, "route": selected_route, "category": category}


@router.post("/{business_id}/{route}/imports/commit", status_code=201)
async def commit_workspace_import(
    business_id: str,
    route: str,
    file: UploadFile = File(...),
    expected_sha256: str = Form(..., min_length=64, max_length=64),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    selected_route = _route(route)
    await _business(current_user, business_id)
    category = _import_category(selected_route)
    data = await _read_upload(file)
    preview = upload_svc.preview_upload(
        category=category,
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    if preview["sha256"] != expected_sha256:
        raise HTTPException(status_code=409, detail="미리보기 이후 파일이 변경되었습니다")
    result = await upload_svc.create_upload(
        user=current_user,
        business_id=business_id,
        category=category,
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    return {"upload": result, "route": selected_route, "category": category}


@router.post("/{business_id}/{route}/records", status_code=201)
async def create_workspace_record(
    business_id: str,
    route: str,
    payload: WorkspaceManualRecord,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    selected_route = _route(route)
    await _business(current_user, business_id)
    values = payload.model_dump()
    values["business_id"] = business_id
    if selected_route in LEDGER_ROUTES:
        result = await upload_svc.create_manual_entry(
            user=current_user,
            category=(payload.ledger_category or "purchase") if selected_route in {"tax-evidence", "tax-gap"} else LEDGER_ROUTES[selected_route],
            payload=values,
        )
        return {"record": _json_value(result), "source": "obys_manual_ledger"}
    if selected_route in CARD_ROUTES:
        if len(payload.card_last4) != 4 or not payload.card_last4.isdigit():
            raise HTTPException(status_code=422, detail="카드번호 끝 4자리를 입력하십시오")
        result = await upload_svc.create_card_transaction(
            user=current_user,
            payload={
                **values,
                "occurred_at": f"{payload.occurred_on}T00:00:00+09:00",
                "merchant": payload.counterparty,
            },
        )
        return {"record": _json_value(result), "source": "obys_card_transactions"}
    if selected_route in BANK_ROUTES:
        if payload.total_amount != payload.total_amount.to_integral_value():
            raise HTTPException(status_code=422, detail="통장 금액은 원 단위 정수로 입력하십시오")
        result = await upload_svc.create_bank_transaction(
            user=current_user,
            payload={
                **values,
                "occurred_at": f"{payload.occurred_on}T00:00:00+09:00",
                "amount": int(payload.total_amount),
                "balance": None,
                "memo": payload.description,
                "category": "manual",
            },
        )
        return {"record": _json_value(result), "source": "obys_bank_transactions"}
    raise HTTPException(status_code=409, detail="이 화면은 직접 등록 대상이 아닙니다")
