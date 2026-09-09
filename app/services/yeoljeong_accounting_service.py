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

KST = timezone(timedelta(hours=9))


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://aads:aads@localhost:5432/aads")


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

# 사업소득 원천징수세율 (프리랜서/일용직 3.3% 근사치)
WITHHOLDING_TAX_RATE = 0.033


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
async def _calculate_tax_figures(
    conn: Any,
    business_id: str,
    report_type: str,
    period_start: Any,
    period_end: Any,
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

    if report_type == "withholding":
        tax_amount = round(labor_cost * WITHHOLDING_TAX_RATE)
    else:
        tax_amount = vat_payable

    return {
        "total_sales": total_sales,
        "total_purchases": total_purchases,
        "vat_payable": vat_payable,
        "tax_amount": tax_amount,
        "details": {
            "output_vat": output_vat,
            "input_vat": input_vat,
            "labor_cost": labor_cost,
            "withholding_rate": WITHHOLDING_TAX_RATE,
            "by_category": rows,
        },
    }


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
        return _rows(rows)
    finally:
        await conn.close()


async def create_tax_report(payload: dict[str, Any]) -> dict[str, Any]:
    import asyncpg

    business_id = payload.get("business_id") or "biz-mia"
    report_type = payload.get("report_type") or "vat"
    period_start = _parse_date(payload.get("period_start"))
    period_end = _parse_date(payload.get("period_end"))

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        figures = await _calculate_tax_figures(conn, business_id, report_type, period_start, period_end)
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
        return _row(row)
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


async def update_tax_report(report_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    import asyncpg

    fields = {k: v for k, v in payload.items() if k in _TAX_REPORT_EDITABLE_FIELDS and v is not None}
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        set_clauses = []
        params: list[Any] = [report_id]
        for key, value in fields.items():
            params.append(value)
            set_clauses.append(f"{key} = ${len(params)}")
        if fields.get("status") == "submitted":
            params.append(_now())
            set_clauses.append(f"submitted_at = ${len(params)}")
        params.append(_now())
        set_clauses.append(f"updated_at = ${len(params)}")
        if not set_clauses:
            row = await conn.fetchrow("SELECT * FROM yeoljeong_tax_reports WHERE id = $1", report_id)
            return _row(row) if row else None
        row = await conn.fetchrow(
            f"""
            UPDATE yeoljeong_tax_reports
               SET {', '.join(set_clauses)}
             WHERE id = $1
         RETURNING *
            """,
            *params,
        )
        return _row(row) if row else None
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
        figures = await _calculate_tax_figures(
            conn,
            report["business_id"],
            report["report_type"],
            report["period_start"],
            report["period_end"],
        )
        return {**report, **figures}
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


async def delete_category_rule(rule_id: str) -> bool:
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
