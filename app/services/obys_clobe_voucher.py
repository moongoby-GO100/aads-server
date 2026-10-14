"""클로브 확정 거래(obys_clobe_ledger_entry) → 오비서 전표 초안(yeoljeong_journal_vouchers/lines).

파일럿: OBYS_CLOBE_VOUCHER_BUSINESSES(쉼표 목록, 기본 biz-chicblack)에 있는 사업장만 전표를 만든다.
차단 범위(core.blocked_*)는 환경변수로 켜 두어도 항상 제외한다. 전표는 draft/needs_review 로만 만들고
승인·확정(approved/posted)은 이 모듈이 절대 건드리지 않는다. 매핑 불가는 미분류 가계정(9998/9999)에 두고
description 에 표시해 사람이 고치게 한다.

계정코드 출처: yeoljeong_journal_lines.account_code 는 자유 text 이고 ACCT 에도 계정과목 마스터가 없다
(docs/prd/ACCT-LAYOUT-001 §5.3). ACCT 실사용 코드 83100 은 docs/prd/ACCT-LAYOUT-002 §2.2 의 DB 조회값,
10300 은 tests/unit/test_obys_workspaces.py 픽스처, 9998/9999 는 obys_upload_service.create_journal 의 기존 가계정이다.
나머지는 더존 표준코드 기준 제안이라 회계 확인이 필요하다.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.services import obys_collection_core as core

logger = logging.getLogger(__name__)

SOURCE_TYPE = "clobe"
DEFAULT_PILOT_BUSINESSES = "biz-chicblack"
PILOT_ENV = "OBYS_CLOBE_VOUCHER_BUSINESSES"
UNCLASSIFIED_NAME = "미분류(가계정)"
STATUS_CLEAN = "draft"
STATUS_REVIEW = "needs_review"

# 키 → (account_code, account_name). 코드 상수 + tests/unit/test_obys_clobe_voucher.py 로 고정한다.
ACCOUNTS: dict[str, tuple[str, str]] = {
    "cash": ("10100", "현금"),
    "bank": ("10300", "보통예금"),
    "receivable": ("10800", "외상매출금"),
    "vat_paid": ("13500", "부가세대급금"),
    "payable": ("25100", "외상매입금"),
    "unpaid": ("25300", "미지급금"),
    "vat_received": ("25500", "부가세예수금"),
    "sales": ("40100", "상품매출"),
    "interest_income": ("90100", "이자수익"),
    "other_opex": ("83100", "(판)지급수수료"),
    "unclassified_debit": ("9998", UNCLASSIFIED_NAME),
    "unclassified_credit": ("9999", UNCLASSIFIED_NAME),
}

# 클로브 category(공백 제거 후 일치) → (계정 키, 허용 방향). income=입금·매출 쪽, expense=출금·비용 쪽.
# 여기 없는 category, isUnclassified, '계정 없는 출금' 은 모두 미분류로 간다.
CATEGORY_ACCOUNTS: dict[str, tuple[str, str]] = {
    "매출": ("sales", "income"),
    "이자수익": ("interest_income", "income"),
    "기타영업비용": ("other_opex", "expense"),
}

TAX_CODE = {"vat_paid": "매입세액", "vat_received": "매출세액"}


class VoucherSkip(Exception):
    """전표를 만들 수 없는 거래. reason 은 집계용 짧은 코드."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Line:
    side: str
    account: str
    amount: Decimal
    tax_code: str = ""


@dataclass
class VoucherDraft:
    transaction_date: date
    description: str
    supply: Decimal
    tax: Decimal
    total: Decimal
    lines: list[Line] = field(default_factory=list)
    unclassified: bool = False

    @property
    def status(self) -> str:
        return STATUS_REVIEW if self.unclassified else STATUS_CLEAN


def pilot_businesses() -> frozenset[str]:
    raw = os.getenv(PILOT_ENV)
    text = DEFAULT_PILOT_BUSINESSES if raw is None else raw
    return frozenset(x.strip() for x in text.split(",") if x.strip())


def voucher_enabled(tenant_id: Any, business_id: str) -> bool:
    """파일럿 목록에 있고 차단 범위가 아닐 때만 True. 차단은 환경변수보다 우선한다."""
    if str(tenant_id) in core.blocked_tenants() or business_id in core.blocked_businesses():
        return False
    return business_id in pilot_businesses()


def _dec(value: Any) -> Decimal:
    parsed = core._dec(value)
    return parsed if parsed is not None else Decimal("0.00")


def _classify(raw: dict[str, Any], side: str) -> str | None:
    """클로브 category → 계정 키. side 는 상대 계정이 놓일 방향(income|expense). 매핑 불가면 None."""
    if raw.get("isUnclassified") is True:
        return None
    mapped = CATEGORY_ACCOUNTS.get("".join(str(raw.get("category") or "").split()))
    return mapped[0] if mapped and mapped[1] == side else None


def _line(side: str, key: str, amount: Decimal) -> Line:
    return Line(side=side, account=key, amount=amount, tax_code=TAX_CODE.get(key, ""))


def _flip(side: str) -> str:
    return "credit" if side == "debit" else "debit"


def _split(total: Decimal, tax: Decimal) -> tuple[Decimal, Decimal]:
    """(공급가, 세액). 세액이 합계 이상이거나 음수면 세액 0 으로 본다(전표 줄 금액은 > 0). total = supply + tax 를 항상 지킨다."""
    tax = tax if Decimal(0) <= tax < total else Decimal("0.00")
    return total - tax, tax


_Built = tuple[Decimal, Decimal, list[Line], bool, str]


def _purchase_side(raw: dict[str, Any], supply: Decimal, tax: Decimal, counter: str) -> tuple[list[Line], bool]:
    """매입·비용 전표의 차변(비용 + 부가세대급금)과 대변 상대 계정. 비용 계정이 미분류면 True."""
    key = _classify(raw, "expense")
    lines = [_line("debit", key or "unclassified_debit", supply)]
    if tax:
        lines.append(_line("debit", "vat_paid", tax))
    lines.append(_line("credit", counter, supply + tax))
    return lines, key is None


def _sales_side(debit: str, supply: Decimal, tax: Decimal) -> list[Line]:
    lines = [_line("debit", debit, supply + tax), _line("credit", "sales", supply)]
    if tax:
        lines.append(_line("credit", "vat_received", tax))
    return lines


def _tax_invoice(raw: dict[str, Any], direction: str, amount: Decimal) -> _Built:
    total = abs(_dec(raw.get("totalAmount")) or amount)
    supply, tax = _split(total, abs(_dec(raw.get("taxAmount"))))
    if direction == "SALES":
        return supply, tax, _sales_side("receivable", supply, tax), False, "세금계산서 매출"
    lines, unclassified = _purchase_side(raw, supply, tax, "payable")
    return supply, tax, lines, unclassified, "세금계산서 매입"


def _card(raw: dict[str, Any], amount: Decimal) -> _Built:
    total = abs(core.card_amount(raw) or amount)
    supply, tax = _split(total, abs(core.card_vat(raw) or Decimal(0)))
    lines, unclassified = _purchase_side(raw, supply, tax, "unpaid")
    return supply, tax, lines, unclassified, "카드 승인"


def _cash_receipt(raw: dict[str, Any], direction: str, amount: Decimal) -> _Built:
    total = abs(_dec(raw.get("totalAmount")) or amount)
    supply, tax = _split(total, abs(_dec(raw.get("vatAmount"))))
    if direction == "SALES":
        return supply, tax, _sales_side("cash", supply, tax), False, "현금영수증 매출"
    lines, unclassified = _purchase_side(raw, supply, tax, "cash")
    return supply, tax, lines, unclassified, "현금영수증 매입"


def _bank(raw: dict[str, Any], direction: str, amount: Decimal) -> _Built:
    total = abs(amount)
    zero = Decimal("0.00")
    if direction == "IN":
        key = _classify(raw, "income")
        lines = [_line("debit", "bank", total), _line("credit", key or "unclassified_credit", total)]
        return total, zero, lines, key is None, "은행 입금"
    if direction == "OUT":
        key = _classify(raw, "expense")
        lines = [_line("debit", key or "unclassified_debit", total), _line("credit", "bank", total)]
        return total, zero, lines, key is None, "은행 출금"
    raise VoucherSkip("unknown_bank_direction")


def _is_negative(kind: str, raw: dict[str, Any], amount: Decimal) -> bool:
    """취소·음수 금액 거래는 차대를 뒤집은 역방향 전표로 만든다(금액은 항상 양수)."""
    if amount < 0:
        return True
    if kind == core.KIND_CARD:
        return core.is_card_cancelled(raw)
    return core.is_cancellation(kind, raw)


def build_draft(
    kind: str, *, direction: str, occurred_on: date | None, amount: Any, counterparty: str, payload: dict[str, Any],
) -> VoucherDraft:
    """순수 함수: 확정 거래 1건 → 전표 초안. 만들 수 없으면 VoucherSkip."""
    if occurred_on is None:
        raise VoucherSkip("no_date")
    signed = _dec(amount)
    if signed == 0:
        raise VoucherSkip("zero_amount")
    direction = (direction or "").upper()
    if kind == core.KIND_TAX:
        supply, tax, lines, unclassified, label = _tax_invoice(payload, direction, signed)
    elif kind == core.KIND_CARD:
        supply, tax, lines, unclassified, label = _card(payload, signed)
    elif kind == core.KIND_CASH:
        supply, tax, lines, unclassified, label = _cash_receipt(payload, direction, signed)
    elif kind == core.KIND_BANK:
        supply, tax, lines, unclassified, label = _bank(payload, direction, signed)
    else:
        raise VoucherSkip("unknown_kind")
    total = supply + tax
    if total <= 0:
        raise VoucherSkip("zero_amount")
    if _is_negative(kind, payload, signed):
        lines = [Line(_flip(ln.side), ln.account, ln.amount, ln.tax_code) for ln in lines]
        label += " 취소"
    parts = [f"[클로브] {label}"]
    if counterparty:
        parts.append(counterparty)
    if unclassified:
        parts.insert(0, "[미분류]")
        parts.append(f"계정 확인 필요(클로브 분류: {str(payload.get('category') or '').strip() or '없음'})")
    return VoucherDraft(
        transaction_date=occurred_on, description=" ".join(parts)[:500], supply=supply, tax=tax, total=total,
        lines=lines, unclassified=unclassified,
    )


def is_balanced(draft: VoucherDraft) -> bool:
    debit = sum((ln.amount for ln in draft.lines if ln.side == "debit"), Decimal(0))
    credit = sum((ln.amount for ln in draft.lines if ln.side == "credit"), Decimal(0))
    return debit == credit and len(draft.lines) >= 2


def voucher_no(entry_id: uuid.UUID, day: date) -> str:
    return f"CLOBE-{day:%Y%m%d}-{entry_id.hex[:12].upper()}"


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


async def create_draft_for_entry(
    conn: Any, *, tenant_id: Any, business_id: str, entry_id: uuid.UUID, item: Any, actor: str,
) -> str:
    """확정 원장 1건의 전표 초안을 만든다. 호출자의 트랜잭션 안에서 실행된다.

    반환: created | exists | disabled | skipped:<reason>. 같은 source 의 전표(역분개 제외)가 있으면 exists.
    item 은 obys_clobe_item 행(data_kind, direction, occurred_on, amount, counterparty, payload).
    """
    if not voucher_enabled(tenant_id, business_id):
        return "disabled"
    tenant = uuid.UUID(str(tenant_id))
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"clobe-voucher|{tenant}|{business_id}|{entry_id}")
    exists = await conn.fetchval(
        "SELECT 1 FROM yeoljeong_journal_vouchers WHERE tenant_id=$1 AND business_id=$2 AND source_type=$3 "
        "AND source_id=$4 AND reversal_of_id IS NULL", tenant, business_id, SOURCE_TYPE, entry_id)
    if exists:
        return "exists"
    try:
        draft = build_draft(
            item["data_kind"], direction=item["direction"], occurred_on=item["occurred_on"], amount=item["amount"],
            counterparty=item["counterparty"] or "", payload=_payload(item["payload"]))
    except VoucherSkip as skip:
        return f"skipped:{skip.reason}"
    if not is_balanced(draft):
        logger.error("clobe voucher unbalanced entry=%s", entry_id)
        return "skipped:unbalanced"
    voucher_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO yeoljeong_journal_vouchers (id,tenant_id,business_id,voucher_no,transaction_date,description,status,"
        " source_type,source_id,supply_amount,tax_amount,total_amount,evidence_source,created_by) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
        voucher_id, tenant, business_id, voucher_no(entry_id, draft.transaction_date), draft.transaction_date,
        draft.description, draft.status, SOURCE_TYPE, entry_id, draft.supply, draft.tax, draft.total,
        f"clobe:{item['data_kind']}", actor)
    for number, ln in enumerate(draft.lines, start=1):
        code, name = ACCOUNTS[ln.account]
        await conn.execute(
            "INSERT INTO yeoljeong_journal_lines (id,voucher_id,line_no,side,account_code,account_name,amount,tax_code,memo) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            uuid.uuid4(), voucher_id, number, ln.side, code, name, ln.amount, ln.tax_code,
            "미분류 - 계정 확인 필요" if ln.account.startswith("unclassified") else "")
    return "created"
