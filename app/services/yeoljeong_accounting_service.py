"""Service layer for the Yeoljeong store-assistant tax/accounting module.

Tables (see scripts/migrations/yeoljeong_accounting_tables.sql):
    yeoljeong_accounting_entries, yeoljeong_tax_reports, yeoljeong_category_rules

Core rules:
    - auto_classify() pulls unclassified rows from yeoljeong_bank_transactions
      (category IS NULL OR category = '') and turns them into accounting
      entries using yeoljeong_category_rules first, then DEFAULT_CATEGORY_RULES.
    - VAT is assumed to be included in the transaction amount (부가세 포함가):
      vat_amount = round(amount / 11).
    - 손익 = 매출(sales) - 매입(purchase) - 경비(expense).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from app.core.obys_db import obys_db_url
from app.services import yeoljeong_withholding as withholding
from app.services.obys_upload_service import (
    require_accounting_write_enabled,
    require_journal_write_enabled,
)

KST = timezone(timedelta(hours=9))


def _db_url() -> str:
    return obys_db_url()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> date | None:
    """asyncpg는 ::date 캐스팅된 파라미터로 실제 date 객체를 요구한다 (문자열 불가)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _row(record: Any) -> dict[str, Any]:
    return dict(record) if record is not None else {}


def _rows(records: Any) -> list[dict[str, Any]]:
    return [_row(r) for r in records]


# ---------------------------------------------------------------------------
# 자동분류 기본 규칙 (yeoljeong_category_rules 커스텀 규칙이 우선한다)
# ---------------------------------------------------------------------------
DEFAULT_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("식자재", ("쌀", "백미", "고춧가루", "소스", "김치", "대파", "양파", "고기", "식자재")),
    ("배달앱", ("배민", "배달의민족", "요기요", "쿠팡이츠", "정산")),
    ("임차료", ("월세", "임대료", "관리비")),
    ("인건비", ("급여", "4대보험", "고용", "알바", "직원")),
    ("공과금", ("전기", "가스", "수도", "통신", "인터넷")),
    ("카드수수료", ("카드수수료", "수수료")),
    ("비품", ("비품", "소모품", "주방", "용기", "포장")),
]

# 매입(purchase)으로 취급하는 카테고리. 그 외 지출성 카테고리는 경비(expense).
PURCHASE_CATEGORIES = {"식자재"}

# 사업소득 원천징수 합산 세율(소득세 3% + 지방소득세 0.3%). 값과 출처는 yeoljeong_withholding 이 정본이다.
# 소득 구분 없이 이 값을 일괄 적용하지 않는다 — 원천세 신고는 income_kind 별로 계산한다.
WITHHOLDING_TAX_RATE = float(
    withholding.BUSINESS_INCOME_TAX_RATE * (1 + withholding.LOCAL_INCOME_TAX_RATIO)
)


def _match_category(text: str, custom_rules: list[dict[str, Any]]) -> tuple[str, str, str]:
    """커스텀 규칙 -> 기본 규칙 -> '미분류' 순으로 매칭한다."""
    for rule in custom_rules:
        keyword = str(rule.get("keyword") or "").strip()
        if keyword and keyword in text:
            return (
                str(rule.get("category") or "미분류"),
                str(rule.get("subcategory") or ""),
                str(rule.get("tax_type") or "vat"),
            )
    for category, keywords in DEFAULT_CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category, "", "vat"
    return "미분류", "", "vat"


def _entry_type_for(direction: str, category: str) -> str:
    if direction == "in":
        return "sales"
    return "purchase" if category in PURCHASE_CATEGORIES else "expense"


def _vat_amount_for(amount: float, tax_type: str) -> float:
    if tax_type != "vat" or not amount:
        return 0
    return round(float(amount) / 11)


# ---------------------------------------------------------------------------
# 회계 내역 (yeoljeong_accounting_entries)
# ---------------------------------------------------------------------------
async def list_entries(
    business_id: str = "biz-mia",
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    category: str | None = None,
    entry_type: str | None = None,
) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        if parsed_from:
            params.append(parsed_from)
            conditions.append(f"entry_date >= ${len(params)}::date")
        if parsed_to:
            params.append(parsed_to)
            conditions.append(f"entry_date <= ${len(params)}::date")
        if category:
            params.append(category)
            conditions.append(f"category = ${len(params)}")
        if entry_type:
            params.append(entry_type)
            conditions.append(f"entry_type = ${len(params)}")
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT * FROM yeoljeong_accounting_entries WHERE {where} ORDER BY entry_date DESC, created_at DESC",
            *params,
        )
        return _rows(rows)
    finally:
        await conn.close()


async def create_entry(payload: dict[str, Any], actor: str = "") -> dict[str, Any]:
    require_journal_write_enabled()
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_accounting_entries (
                business_id, entry_date, entry_type, category, subcategory, description,
                debit_amount, credit_amount, counterparty, source_type, source_id,
                tax_type, vat_amount, receipt_id, verified, verified_by, memo
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
            )
            RETURNING *
            """,
            payload.get("business_id") or "biz-mia",
            _parse_date(payload.get("entry_date")),
            payload.get("entry_type"),
            payload.get("category") or "",
            payload.get("subcategory") or "",
            payload.get("description") or "",
            payload.get("debit_amount") or 0,
            payload.get("credit_amount") or 0,
            payload.get("counterparty") or "",
            payload.get("source_type") or "manual",
            payload.get("source_id") or "",
            payload.get("tax_type") or "vat",
            payload.get("vat_amount") or 0,
            payload.get("receipt_id") or "",
            bool(payload.get("verified") or False),
            payload.get("verified_by") or (actor if payload.get("verified") else ""),
            payload.get("memo") or "",
        )
        return _row(row)
    finally:
        await conn.close()


_ENTRY_EDITABLE_FIELDS = (
    "entry_date",
    "entry_type",
    "category",
    "subcategory",
    "description",
    "debit_amount",
    "credit_amount",
    "counterparty",
    "source_type",
    "source_id",
    "tax_type",
    "vat_amount",
    "receipt_id",
    "verified",
    "verified_by",
    "memo",
)


async def update_entry(entry_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    require_journal_write_enabled()
    import asyncpg

    fields = {k: v for k, v in payload.items() if k in _ENTRY_EDITABLE_FIELDS and v is not None}
    if "entry_date" in fields:
        fields["entry_date"] = _parse_date(fields["entry_date"])
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        if not fields:
            row = await conn.fetchrow("SELECT * FROM yeoljeong_accounting_entries WHERE id = $1", entry_id)
            return _row(row) if row else None
        set_clauses = []
        params: list[Any] = [entry_id]
        for key, value in fields.items():
            params.append(value)
            set_clauses.append(f"{key} = ${len(params)}")
        params.append(_now())
        set_clauses.append(f"updated_at = ${len(params)}")
        row = await conn.fetchrow(
            f"""
            UPDATE yeoljeong_accounting_entries
               SET {', '.join(set_clauses)}
             WHERE id = $1
         RETURNING *
            """,
            *params,
        )
        return _row(row) if row else None
    finally:
        await conn.close()


async def delete_entry(entry_id: str) -> bool:
    require_journal_write_enabled()
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        deleted = await conn.fetchval(
            "DELETE FROM yeoljeong_accounting_entries WHERE id = $1 RETURNING id",
            entry_id,
        )
        return deleted is not None
    finally:
        await conn.close()


async def auto_classify(business_id: str = "biz-mia", limit: int = 500) -> dict[str, Any]:
    """미분류 은행거래를 규칙 기반으로 분류하여 회계 항목으로 등록한다."""
    require_journal_write_enabled()
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        custom_rules = _rows(
            await conn.fetch(
                "SELECT * FROM yeoljeong_category_rules WHERE business_id = $1 ORDER BY priority DESC, created_at ASC",
                business_id,
            )
        )
        unclassified = await conn.fetch(
            """
            SELECT * FROM yeoljeong_bank_transactions
             WHERE business_id = $1 AND (category IS NULL OR category = '')
             ORDER BY occurred_date ASC NULLS LAST, occurred_at ASC
             LIMIT $2
            """,
            business_id,
            limit,
        )

        created: list[dict[str, Any]] = []
        for txn in unclassified:
            txn = _row(txn)
            text = " ".join(
                str(txn.get(field) or "") for field in ("counterparty", "memo", "raw_memo")
            )
            category, subcategory, tax_type = _match_category(text, custom_rules)
            direction = str(txn.get("direction") or "")
            entry_type = _entry_type_for(direction, category)
            amount = float(txn.get("amount") or 0)
            vat_amount = _vat_amount_for(amount, tax_type)
            entry_date = txn.get("occurred_date") or txn.get("occurred_at")

            async with conn.transaction():
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO yeoljeong_accounting_entries (
                        business_id, entry_date, entry_type, category, subcategory, description,
                        debit_amount, credit_amount, counterparty, source_type, source_id,
                        tax_type, vat_amount, memo
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, 'bank_auto', $10, $11, $12, $13
                    )
                    RETURNING *
                    """,
                    business_id,
                    entry_date,
                    entry_type,
                    category,
                    subcategory,
                    str(txn.get("memo") or txn.get("raw_memo") or ""),
                    amount if entry_type in ("purchase", "expense") else 0,
                    amount if entry_type == "sales" else 0,
                    str(txn.get("counterparty") or ""),
                    str(txn.get("id") or ""),
                    tax_type,
                    vat_amount,
                    f"자동분류: {category}",
                )
                await conn.execute(
                    "UPDATE yeoljeong_bank_transactions SET category = $2, updated_at = $3 WHERE id = $1",
                    txn.get("id"),
                    category,
                    _now(),
                )
                created.append(_row(inserted))

        return {"created": created, "count": len(created), "scanned": len(unclassified)}
    finally:
        await conn.close()


async def get_summary(
    business_id: str = "biz-mia",
    date_from: str | date | None = None,
    date_to: str | date | None = None,
) -> dict[str, Any]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        if parsed_from:
            params.append(parsed_from)
            conditions.append(f"entry_date >= ${len(params)}::date")
        if parsed_to:
            params.append(parsed_to)
            conditions.append(f"entry_date <= ${len(params)}::date")
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"""
            SELECT entry_type,
                   COALESCE(SUM(debit_amount), 0) AS debit_total,
                   COALESCE(SUM(credit_amount), 0) AS credit_total
              FROM yeoljeong_accounting_entries
             WHERE {where}
             GROUP BY entry_type
            """,
            *params,
        )
        totals = {r["entry_type"]: r for r in _rows(rows)}
        total_sales = float(totals.get("sales", {}).get("credit_total", 0)) - float(
            totals.get("sales", {}).get("debit_total", 0)
        )
        total_purchases = float(totals.get("purchase", {}).get("debit_total", 0)) - float(
            totals.get("purchase", {}).get("credit_total", 0)
        )
        total_expenses = float(totals.get("expense", {}).get("debit_total", 0)) - float(
            totals.get("expense", {}).get("credit_total", 0)
        )
        return {
            "business_id": business_id,
            "period": {"date_from": date_from, "date_to": date_to},
            "total_sales": total_sales,
            "total_purchases": total_purchases,
            "total_expenses": total_expenses,
            "operating_profit": total_sales - total_purchases - total_expenses,
        }
    finally:
        await conn.close()


async def get_pnl(
    business_id: str = "biz-mia",
    date_from: str | date | None = None,
    date_to: str | date | None = None,
) -> dict[str, Any]:
    """손익계산서: entry_type/category 별 집계."""
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        if parsed_from:
            params.append(parsed_from)
            conditions.append(f"entry_date >= ${len(params)}::date")
        if parsed_to:
            params.append(parsed_to)
            conditions.append(f"entry_date <= ${len(params)}::date")
        where = " AND ".join(conditions)
        rows = _rows(
            await conn.fetch(
                f"""
                SELECT entry_type, category,
                       COALESCE(SUM(debit_amount), 0) AS debit_total,
                       COALESCE(SUM(credit_amount), 0) AS credit_total
                  FROM yeoljeong_accounting_entries
                 WHERE {where}
                 GROUP BY entry_type, category
                 ORDER BY entry_type, category
                """,
                *params,
            )
        )
        revenue: list[dict[str, Any]] = []
        cost_of_sales: list[dict[str, Any]] = []
        expenses: list[dict[str, Any]] = []
        total_revenue = total_cost_of_sales = total_expenses = 0.0
        for r in rows:
            net_in = float(r["credit_total"]) - float(r["debit_total"])
            net_out = float(r["debit_total"]) - float(r["credit_total"])
            if r["entry_type"] == "sales":
                revenue.append({"category": r["category"], "amount": net_in})
                total_revenue += net_in
            elif r["entry_type"] == "purchase":
                cost_of_sales.append({"category": r["category"], "amount": net_out})
                total_cost_of_sales += net_out
            else:
                expenses.append({"category": r["category"], "amount": net_out})
                total_expenses += net_out
        gross_profit = total_revenue - total_cost_of_sales
        return {
            "business_id": business_id,
            "period": {"date_from": date_from, "date_to": date_to},
            "revenue": revenue,
            "cost_of_sales": cost_of_sales,
            "expenses": expenses,
            "total_revenue": total_revenue,
            "total_cost_of_sales": total_cost_of_sales,
            "total_expenses": total_expenses,
            "gross_profit": gross_profit,
            "operating_profit": gross_profit - total_expenses,
        }
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 세무신고 (yeoljeong_tax_reports)
# ---------------------------------------------------------------------------
def _withholding_figures(income_kind: Any, labor_cost: float) -> tuple[Any, dict[str, Any]]:
    """원천세 신고의 세액과 근거. 검증되지 않았거나 구분이 없으면 세액은 None(계산하지 않음)."""
    kind = str(income_kind or "").strip()
    if not kind:
        return None, {
            "status": "verification_required",
            "income_kind": "",
            "reason": "소득 구분(income_kind)이 없어 원천세를 계산하지 않았다",
        }
    try:
        result = withholding.compute_withholding(kind, labor_cost)
    except withholding.WithholdingUnverified as exc:
        return None, {"status": "verification_required", "income_kind": kind, "reason": exc.reason}
    except withholding.WithholdingKindUnknown as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result["total"], {"status": "computed", **result}


async def _calculate_tax_figures(
    conn: Any,
    business_id: str,
    report_type: str,
    period_start: Any,
    period_end: Any,
    income_kind: Any = None,
) -> dict[str, Any]:
    rows = _rows(
        await conn.fetch(
            """
            SELECT entry_type, category,
                   COALESCE(SUM(debit_amount), 0) AS debit_total,
                   COALESCE(SUM(credit_amount), 0) AS credit_total,
                   COALESCE(SUM(vat_amount), 0) AS vat_total
              FROM yeoljeong_accounting_entries
             WHERE business_id = $1 AND entry_date >= $2::date AND entry_date <= $3::date
             GROUP BY entry_type, category
            """,
            business_id,
            period_start,
            period_end,
        )
    )
    total_sales = sum(float(r["credit_total"]) - float(r["debit_total"]) for r in rows if r["entry_type"] == "sales")
    total_purchases = sum(
        float(r["debit_total"]) - float(r["credit_total"]) for r in rows if r["entry_type"] == "purchase"
    )
    output_vat = sum(float(r["vat_total"]) for r in rows if r["entry_type"] == "sales")
    input_vat = sum(float(r["vat_total"]) for r in rows if r["entry_type"] in ("purchase", "expense"))
    vat_payable = round(output_vat - input_vat)

    labor_cost = sum(
        float(r["debit_total"]) - float(r["credit_total"]) for r in rows if r["category"] == "인건비"
    )

    details: dict[str, Any] = {
        "output_vat": output_vat,
        "input_vat": input_vat,
        "labor_cost": labor_cost,
        "by_category": rows,
    }
    if report_type == "withholding":
        tax_amount, details["withholding"] = _withholding_figures(income_kind, labor_cost)
        if details["withholding"]["status"] == "computed":
            details["withholding_rate"] = WITHHOLDING_TAX_RATE
    else:
        tax_amount = vat_payable

    return {
        "total_sales": total_sales,
        "total_purchases": total_purchases,
        "vat_payable": vat_payable,
        "tax_amount": tax_amount,
        "details": details,
    }


# ---------------------------------------------------------------------------
# 신고 접수 구분 (P-10) — submitted 는 자기선언이다. 접수번호·접수일시가 있어야 접수 완료.
# ---------------------------------------------------------------------------
TAX_REPORT_STATUSES = ("draft", "file_generated", "submitted", "receipt_confirmed")
_TAX_FILING_LABELS = {
    "draft": "초안",
    "file_generated": "신고 파일 생성(미제출)",
    "submitted": "제출 선언(접수번호 없음 · 신고 완료 아님)",
    "receipt_confirmed": "접수 완료",
}
_RECEIPT_PLACEHOLDERS = frozenset({"-", "--", "없음", "미정", "n/a", "na", "tbd", "none", "null", "unknown", "?", "0"})
_RECEIPT_NUMBER_MAX = 64
_NUMERIC_REPORT_FIELDS = ("total_sales", "total_purchases", "vat_payable", "tax_amount")


def _details_of(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = {}
    return details if isinstance(details, dict) else {}


def filing_view(row: dict[str, Any]) -> dict[str, Any]:
    """신고서 행에 접수 구분 필드를 덧붙인다. 접수번호·접수일시 없이는 filing_complete 가 거짓이다."""
    out = dict(row)
    receipt = _details_of(row).get("receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    number = str(receipt.get("receipt_number") or "").strip()
    received_at = receipt.get("received_at")
    status = str(row.get("status") or "draft")
    complete = status == "receipt_confirmed" and bool(number) and bool(received_at)
    if status == "receipt_confirmed" and not complete:
        label = "접수번호 없음 · 신고 완료 아님"
    else:
        label = _TAX_FILING_LABELS.get(status, f"알 수 없는 상태({status})")
    out["filing_complete"] = complete
    out["filing_label"] = label
    out["receipt_number"] = number if complete else ""
    out["receipt_received_at"] = received_at if complete else None
    return out


def _clean_receipt_number(value: Any) -> str:
    number = str(value or "").strip()
    if (
        not number
        or len(number) > _RECEIPT_NUMBER_MAX
        or number.lower() in _RECEIPT_PLACEHOLDERS
        or any(ch.isspace() or ord(ch) < 32 for ch in number)
    ):
        raise HTTPException(status_code=422, detail="접수 완료에는 유효한 접수번호가 필요합니다")
    return number


def _clean_received_at(value: Any) -> datetime:
    if value in (None, ""):
        raise HTTPException(status_code=422, detail="접수 완료에는 접수일시가 필요합니다")
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="접수일시 형식이 올바르지 않습니다") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    if moment > _now() + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="접수일시가 미래입니다")
    return moment


async def list_tax_reports(business_id: str = "biz-mia", report_type: str | None = None) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        conditions = ["business_id = $1"]
        params: list[Any] = [business_id]
        if report_type:
            params.append(report_type)
            conditions.append(f"report_type = ${len(params)}")
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT * FROM yeoljeong_tax_reports WHERE {where} ORDER BY period_start DESC, created_at DESC",
            *params,
        )
        return [filing_view(r) for r in _rows(rows)]
    finally:
        await conn.close()


async def create_tax_report(payload: dict[str, Any]) -> dict[str, Any]:
    require_accounting_write_enabled()
    import asyncpg

    business_id = payload.get("business_id") or "biz-mia"
    report_type = payload.get("report_type") or "vat"
    period_start = _parse_date(payload.get("period_start"))
    period_end = _parse_date(payload.get("period_end"))

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        figures = await _calculate_tax_figures(
            conn, business_id, report_type, period_start, period_end, payload.get("income_kind")
        )
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_tax_reports (
                business_id, report_type, period_start, period_end, status,
                total_sales, total_purchases, vat_payable, tax_amount, details, memo
            ) VALUES ($1, $2, $3, $4, 'draft', $5, $6, $7, $8, $9::jsonb, $10)
            RETURNING *
            """,
            business_id,
            report_type,
            period_start,
            period_end,
            figures["total_sales"],
            figures["total_purchases"],
            figures["vat_payable"],
            figures["tax_amount"],
            json.dumps(figures["details"], ensure_ascii=False, default=str),
            payload.get("memo") or "",
        )
        return filing_view(_row(row))
    finally:
        await conn.close()


_TAX_REPORT_EDITABLE_FIELDS = (
    "status",
    "total_sales",
    "total_purchases",
    "vat_payable",
    "tax_amount",
    "memo",
)


async def tax_report_business_id(report_id: str) -> str | None:
    """신고서가 속한 사업자(테넌트 소유 확인용). 없으면 None."""
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        return await conn.fetchval("SELECT business_id FROM yeoljeong_tax_reports WHERE id = $1", report_id)
    finally:
        await conn.close()


async def update_tax_report(report_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    require_accounting_write_enabled()
    import asyncpg

    fields = {k: v for k, v in payload.items() if k in _TAX_REPORT_EDITABLE_FIELDS and v is not None}
    status = fields.get("status")
    if status is not None and status not in TAX_REPORT_STATUSES:
        raise HTTPException(status_code=422, detail=f"알 수 없는 신고 상태: {status}")
    has_receipt_input = bool(payload.get("receipt_number")) or payload.get("receipt_received_at") is not None
    receipt: dict[str, Any] | None = None
    if status == "receipt_confirmed":
        receipt = {
            "receipt_number": _clean_receipt_number(payload.get("receipt_number")),
            "received_at": _clean_received_at(payload.get("receipt_received_at")).isoformat(),
        }
    elif has_receipt_input:
        raise HTTPException(status_code=422, detail="접수번호·접수일시는 상태를 receipt_confirmed 로 바꿀 때만 입력한다")

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        current = await conn.fetchrow("SELECT status FROM yeoljeong_tax_reports WHERE id = $1", report_id)
        if current is None:
            return None
        if current["status"] == "receipt_confirmed":
            if status not in (None, "receipt_confirmed") or any(f in fields for f in _NUMERIC_REPORT_FIELDS):
                raise HTTPException(status_code=409, detail="접수 완료된 신고서의 상태·금액은 바꿀 수 없다")
            if receipt is not None:
                raise HTTPException(status_code=409, detail="이미 접수 완료된 신고서다")
        set_clauses = []
        params: list[Any] = [report_id]
        for key, value in fields.items():
            params.append(value)
            set_clauses.append(f"{key} = ${len(params)}")
        if status in ("submitted", "receipt_confirmed"):
            params.append(_now())
            set_clauses.append(f"submitted_at = COALESCE(submitted_at, ${len(params)})")
        if receipt is not None:
            params.append(json.dumps(receipt, ensure_ascii=False))
            set_clauses.append(
                f"details = COALESCE(details, '{{}}'::jsonb) || jsonb_build_object('receipt', ${len(params)}::jsonb)"
            )
        params.append(_now())
        set_clauses.append(f"updated_at = ${len(params)}")
        row = await conn.fetchrow(
            f"""
            UPDATE yeoljeong_tax_reports
               SET {', '.join(set_clauses)}
             WHERE id = $1
         RETURNING *
            """,
            *params,
        )
        return filing_view(_row(row)) if row else None
    finally:
        await conn.close()


async def calculate_tax_report(report_id: str) -> dict[str, Any] | None:
    """기존 신고 건의 기간 기준으로 부가세/원천세를 재계산해서 반환한다 (읽기 전용)."""
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        report = await conn.fetchrow("SELECT * FROM yeoljeong_tax_reports WHERE id = $1", report_id)
        if not report:
            return None
        report = _row(report)
        stored_kind = (_details_of(report).get("withholding") or {}).get("income_kind")
        figures = await _calculate_tax_figures(
            conn,
            report["business_id"],
            report["report_type"],
            report["period_start"],
            report["period_end"],
            stored_kind,
        )
        return {**filing_view(report), **figures}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 자동분류 규칙 (yeoljeong_category_rules)
# ---------------------------------------------------------------------------
async def list_category_rules(business_id: str = "biz-mia") -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        rows = await conn.fetch(
            "SELECT * FROM yeoljeong_category_rules WHERE business_id = $1 ORDER BY priority DESC, created_at ASC",
            business_id,
        )
        return _rows(rows)
    finally:
        await conn.close()


async def create_category_rule(payload: dict[str, Any]) -> dict[str, Any]:
    require_accounting_write_enabled()
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO yeoljeong_category_rules (
                business_id, keyword, category, subcategory, tax_type, priority
            ) VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            payload.get("business_id") or "biz-mia",
            payload.get("keyword"),
            payload.get("category"),
            payload.get("subcategory") or "",
            payload.get("tax_type") or "vat",
            int(payload.get("priority") or 0),
        )
        return _row(row)
    finally:
        await conn.close()


async def category_rule_business_id(rule_id: str) -> str | None:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        return await conn.fetchval("SELECT business_id FROM yeoljeong_category_rules WHERE id = $1", rule_id)
    finally:
        await conn.close()


async def entry_business_id(entry_id: str) -> str | None:
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        return await conn.fetchval("SELECT business_id FROM yeoljeong_accounting_entries WHERE id = $1", entry_id)
    finally:
        await conn.close()


async def delete_category_rule(rule_id: str) -> bool:
    require_accounting_write_enabled()
    import asyncpg

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        deleted = await conn.fetchval(
            "DELETE FROM yeoljeong_category_rules WHERE id = $1 RETURNING id",
            rule_id,
        )
        return deleted is not None
    finally:
        await conn.close()
