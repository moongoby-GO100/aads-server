"""Tenant-safe, database-backed workspace API for the OBYS V4.1 UI.

The V4.1 screen has many route-specific views, but the durable source remains
the existing OBYS ledgers.  This adapter exposes a stable read contract without
copying data into a second set of tables or falling back to demo rows.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import json
import re
from typing import Any, Annotated
from uuid import UUID

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
    "attendance": ("SELECT id,employee_name,branch,work_date,start_at,end_at,break_minutes,worked_minutes,status,created_at FROM yeoljeong_attendance_records WHERE business_id=$1 AND deleted_at IS NULL ORDER BY work_date DESC LIMIT 500", "yeoljeong_attendance_records"),
    "payroll": ("SELECT id,employee_name,branch,payroll_month,gross_pay,tax_withholding,insurance_deduction,other_deduction,net_pay,status,created_at FROM yeoljeong_payroll_statements WHERE business_id=$1 AND deleted_at IS NULL ORDER BY payroll_month DESC LIMIT 500", "yeoljeong_payroll_statements"),
    "hr-docs": ("SELECT id,employee_name,branch,document_type,document_label,status,original_filename,issue_date,uploaded_at FROM yeoljeong_onboarding_documents WHERE business_id=$1 AND deleted_at IS NULL ORDER BY uploaded_at DESC LIMIT 500", "yeoljeong_onboarding_documents"),
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
            return Decimal(str(row[key]))
        except (ValueError, TypeError):
            continue
    return Decimal("0")


def _date_text(row: dict[str, Any]) -> str:
    for key in ("occurred_at", "occurred_on", "transaction_date", "created_at"):
        if row.get(key):
            return str(row[key])[:10]
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
        "직원": str(value.get("employee_name") or ""),
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
                "거래유형": "입금" if value.get("direction") == "in" else "출금",
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
        date_from=date_from, date_to=date_to,
    )
    uploaded = await upload_svc.list_ledger_rows(
        user=user, business_id=business_id, category=category, limit=500,
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
                                                      date_from=date_from, date_to=date_to)
    else:
        rows = await upload_svc.list_bank_transactions(user=user, business_id=business_id,
                                                      date_from=date_from, date_to=date_to)
    uploaded = await upload_svc.list_ledger_rows(user=user, business_id=business_id,
                         category="card" if category == "card" else "transaction", limit=500)
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
            user=user, business_id=business_id, date_from=date_from, date_to=date_to,
        )
        card_uploads = await upload_svc.list_ledger_rows(
            user=user, business_id=business_id, category="card", limit=500,
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
            user=user, business_id=business_id, date_from=date_from, date_to=date_to,
        )
        uploaded = await upload_svc.list_ledger_rows(
            user=user, business_id=business_id, category="card", limit=500,
        )
        return [*(("card", row) for row in rows), *(("uploaded", row) for row in uploaded)], "obys_card_transactions"
    if route in BANK_ROUTES:
        rows = await upload_svc.list_bank_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to,
        )
        uploaded = await upload_svc.list_ledger_rows(
            user=user, business_id=business_id, category="transaction", limit=500,
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
            user=user, business_id=business_id, date_from=date_from, date_to=date_to,
        )
        banks = await upload_svc.list_bank_transactions(
            user=user, business_id=business_id, date_from=date_from, date_to=date_to,
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
    preview["duplicate_rows"] = duplicate_count
    preview["accepted_rows"] = max(
        0, int(preview.get("accepted_rows") or 0) - int(duplicate_count or 0)
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
            category=LEDGER_ROUTES[selected_route],
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
