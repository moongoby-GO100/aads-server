"""ACCT 원천(위하고 매입매출전표) 매출·매입 읽기.

오비서 V4.1 의 매출/매입 화면은 오비서 자체 원장이 아니라 진아서버 ACCT 의
원천 전표를 본다. 여기서 중요한 것은 **없는 것을 0 으로 보여주지 않는 것**이다.

2026-09-22 라일론(company_id=11) 실측:

* ``source_file.domain='sales'`` 파일은 324개지만 ``atom_record`` 가 0건이다.
  이 도메인을 매출 원천으로 삼으면 화면에는 언제나 "매출 0원" 이 뜬다.
* 실제 전표는 전부 ``domain='wehago'`` 에 있고, 전표유형(``ty_mth``)은
  ``2``(매입매출전표) 26,723건과 ``3``(일반전표) 31,291건뿐이다.
  **매출 전표(``ty_mth='1'``)는 한 건도 없다.**

그래서 조회 결과가 비어 있을 때 "금액 0" 이 아니라 "원천 미적재" 로 구분해
돌려준다. 계정과목 추정이나 헤더 문자열 추측은 하지 않는다 — 취소 전표와
부가세 포함 금액을 그럴듯한 매출로 바꿔 놓는 사고가 바로 거기서 난다.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.api.acct_purchase import _authorized_acct_scope, _fetch_acct_journals, _lit

logger = logging.getLogger(__name__)

# 위하고 전표유형 원값. 추정이 아니라 ``ty_mth`` 컬럼 값 그대로다.
_ENTRY_TYPE: Dict[str, str] = {"sales": "1", "purchase": "2"}

# rec_idx 단위 pivot 대상 — alias: (field_key, 숫자여부)
_SOURCE_FIELDS: Dict[str, tuple[str, bool]] = {
    "entry_type": ("ty_mth", False),
    "detail_type": ("ty_mth2", False),
    "occurred_on": ("da_sbook", False),
    "seq": ("sq_sbook", False),
    "counterparty": ("nm_trade", False),
    "counterparty_biz_no": ("no_biz", False),
    "item_name": ("nm_good", False),
    "remark": ("nm_remark", False),
    "status_code": ("ty_jungstat", False),
    "evidence_code": ("ty_trade", False),
    "debit_name": ("nm_acctit_cha", False),
    "credit_name": ("nm_acctit_dae", False),
    "supply_amount": ("mn_mnam", True),
    "tax_amount": ("mn_vat", True),
    "total_amount": ("mn_total", True),
}

# 증빙구분(ty_trade) — 세무 화면의 계산서/현금영수증/카드 구분에 쓴다.
_EVIDENCE_LABEL: Dict[str, str] = {
    "10": "세금계산서",
    "11": "계산서",
    "15": "신용카드",
    "2": "현금영수증",
    "9": "기타",
    "24": "수입",
    "0": "증빙 없음",
}

# 매입매출 세부유형(ty_mth2).
_DETAIL_LABEL: Dict[str, str] = {
    "11": "과세매출",
    "12": "영세매출",
    "13": "면세매출",
    "14": "건별매출",
    "16": "수출",
    "17": "카드매출",
    "22": "현금영수증매출",
    "51": "과세매입",
    "52": "영세매입",
    "53": "면세매입",
    "54": "불공제매입",
    "55": "수입",
    "57": "카드매입",
    "61": "현금영수증매입",
}

_MAX_ROWS = 2000


def _digits(value: Optional[str]) -> Optional[str]:
    """ISO 날짜(YYYY-MM-DD)를 원천 표기(YYYYMMDD)로 바꾼다."""
    if not value:
        return None
    text = "".join(ch for ch in str(value) if ch.isdigit())[:8]
    return text if len(text) == 8 else None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        return Decimal("0")


def _pivot_sql(company_id: int, entry_type: str, date_from: Optional[str], date_to: Optional[str]) -> str:
    selects = ",\n           ".join(
        "max({column}) FILTER (WHERE ar.field_key = {key}) AS {alias}".format(
            column="ar.num_value" if is_num else "ar.raw_value",
            key=_lit(field_key),
            alias=alias,
        )
        for alias, (field_key, is_num) in _SOURCE_FIELDS.items()
    )
    keys = ", ".join(_lit(field_key) for field_key, _ in _SOURCE_FIELDS.values())
    conditions = [f"entry_type = {_lit(entry_type)}", "occurred_on IS NOT NULL"]
    start, end = _digits(date_from), _digits(date_to)
    if start:
        conditions.append(f"occurred_on >= {_lit(start)}")
    if end:
        conditions.append(f"occurred_on <= {_lit(end)}")
    # 같은 전표가 여러 스냅샷 파일에 그대로 다시 들어온다.  2026-09-22 라일론
    # 실측에서 매입 원본 행 26,723건은 전표키(일자+일련번호) 기준으로 4,210건
    # 뿐이었고, 그대로 더하면 합계가 53.6억 대신 195억으로 6.3배 부풀었다.
    # 그래서 전표키마다 가장 나중 source_file 한 건만 남긴다.
    return (
        "WITH pivot AS (\n"
        "  SELECT ar.source_file_id, ar.rec_idx,\n"
        f"         {selects}\n"
        "    FROM source_file sf\n"
        "    JOIN atom_record ar ON ar.source_file_id = sf.id\n"
        f"   WHERE sf.company_id = {int(company_id)}\n"
        "     AND sf.is_current IS TRUE\n"
        "     AND sf.domain = 'wehago'\n"
        f"     AND ar.field_key IN ({keys})\n"
        "   GROUP BY ar.source_file_id, ar.rec_idx\n"
        "), deduped AS (\n"
        "  SELECT DISTINCT ON (occurred_on, voucher_key) * FROM (\n"
        "    SELECT pivot.*,\n"
        "           coalesce(nullif(seq, ''), 'rec:' || rec_idx::text) AS voucher_key\n"
        "      FROM pivot\n"
        f"     WHERE {' AND '.join(conditions)}\n"
        "  ) keyed\n"
        "  ORDER BY occurred_on, voucher_key, source_file_id DESC\n"
        ")\n"
        "SELECT * FROM deduped\n"
        " ORDER BY occurred_on DESC, voucher_key DESC\n"
        f" LIMIT {_MAX_ROWS}"
    )


def _iso(occurred_on: Optional[str]) -> str:
    text = str(occurred_on or "")
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}" if len(text) >= 8 else ""


def _row(row: Dict[str, Any], category: str) -> Dict[str, Any]:
    detail = str(row.get("detail_type") or "")
    evidence = str(row.get("evidence_code") or "")
    total = _decimal(row.get("total_amount"))
    supply = _decimal(row.get("supply_amount"))
    return {
        "id": "acct:{0}:{1}".format(row.get("source_file_id"), row.get("rec_idx")),
        "category": category,
        "occurred_on": _iso(row.get("occurred_on")),
        "counterparty": str(row.get("counterparty") or ""),
        "counterparty_biz_no": str(row.get("counterparty_biz_no") or ""),
        "description": str(row.get("item_name") or row.get("remark") or ""),
        "detail_type": detail,
        "detail_type_label": _DETAIL_LABEL.get(detail, detail or "미분류"),
        "evidence_label": _EVIDENCE_LABEL.get(evidence, evidence or "미분류"),
        "debit_account": str(row.get("debit_name") or ""),
        "credit_account": str(row.get("credit_name") or ""),
        "supply_amount": supply,
        "tax_amount": _decimal(row.get("tax_amount")),
        "total_amount": total if total else supply,
        "status": "확정" if str(row.get("status_code") or "") else "검토 필요",
        "source": f"acct_wehago_{category}",
        "source_file_id": row.get("source_file_id"),
        "rec_idx": row.get("rec_idx"),
        "voucher_seq": str(row.get("seq") or ""),
    }


async def source_transactions(
    current_user: dict,
    business_id: str,
    category: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], str]:
    """(행 목록, 원천 이름)을 돌려준다. 읽기 전용이며 계정 추정을 하지 않는다."""
    entry_type = _ENTRY_TYPE.get(category)
    if not entry_type:
        raise HTTPException(status_code=400, detail="매출 또는 매입만 조회할 수 있습니다")
    scope = await _authorized_acct_scope(current_user, business_id)
    if scope is None:
        raise HTTPException(status_code=403, detail="사업자 범위가 필요합니다")
    tenant_id, company_id = scope
    rows = await _fetch_acct_journals(
        _pivot_sql(company_id, entry_type, date_from, date_to), tenant_id
    )
    records = [_row(row, category) for row in rows]
    return records, f"acct.source_file/atom_record:wehago:{category}"
