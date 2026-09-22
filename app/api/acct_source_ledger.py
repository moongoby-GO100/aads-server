"""ACCT 원천(위하고 매입매출전표) 매출·매입 읽기.

오비서 V4.1 의 매출/매입 화면은 오비서 자체 원장이 아니라 진아서버 ACCT 의
원천 전표를 본다. 여기서 중요한 것은 **없는 것을 0 으로 보여주지 않는 것**이다.

2026-09-22 라일론(company_id=11) 실측:

* ``source_file.domain='sales'`` 파일은 324개지만 ``atom_record`` 가 0건이다.
  이 도메인을 매출 원천으로 삼으면 화면에는 언제나 "매출 0원" 이 뜬다.
* 실제 전표는 전부 ``domain='wehago'`` 에 있고, 전표유형(``ty_mth``)은
  ``2``(매입매출전표) 26,723건과 ``3``(일반전표) 31,291건뿐이다.
  **매출 전표(``ty_mth='1'``)는 한 건도 없다.**

이 사실은 위하고 JSON 전표에만 해당한다. sales 도메인의 atom_cell에는
별도 엑셀 자료가 있으므로 조회 결과가 비었다고 전체 원천 미적재로 판정하지
않는다. 원천 보유 현황과 거래 정규화 상태는 source_coverage에서 분리한다.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.api.acct_purchase import _STATUS_LABEL, _authorized_acct_scope, _fetch_acct_journals, _lit

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
    "card_code": ("cd_ctrade", False),
    "invoice_number": ("no_eserotax", False),
    "deposit_amount": ("deposit_amount", True),
    "withdraw_amount": ("withdraw_amount", True),
    "bank_description": ("deal_abstract", False),
    "bank_counterparty": ("consignee", False),
}

# ty_trade=2 also occurs on electronic tax invoices; it is NOT a cash-receipt flag.
# Use explicit document/transaction subtype evidence, retain unknown raw codes.
_EVIDENCE_LABEL = {"15": "신용카드"}
_DETAIL_EVIDENCE = {"11": "세금계산서", "12": "세금계산서", "13": "계산서",
                    "17": "신용카드", "22": "현금영수증", "51": "세금계산서",
                    "52": "세금계산서", "53": "계산서", "57": "신용카드", "61": "현금영수증"}

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


def _card_master_sql(company_id: int) -> str:
    """Verified card-register format; never read historical or foreign masters.

    Transaction cd_ctrade joins register cd_trade (not nm_ctrade). Mask PANs
    inside PostgreSQL so full numbers never enter the API process or response.
    Multiple identities for one code are deliberately left unresolved.
    """
    return f"""
WITH master AS (
 SELECT sf.id AS source_file_id, ar.rec_idx,
  max(ar.raw_value) FILTER (WHERE ar.field_key='cd_trade') AS card_code,
  max(ar.raw_value) FILTER (WHERE ar.field_key='nm_trade') AS card_name,
  max(ar.raw_value) FILTER (WHERE ar.field_key='id_sa') AS pan,
  max(ar.raw_value) FILTER (WHERE ar.field_key='yn_use') AS use_code
 FROM source_file sf JOIN atom_record ar ON ar.source_file_id=sf.id
 WHERE sf.company_id={int(company_id)} AND sf.is_current IS TRUE
  AND sf.domain='wehago'
  AND regexp_replace(sf.abs_path, '^.*/', '')='카드거래처.json'
  AND ar.field_key IN ('cd_trade','nm_trade','id_sa','yn_use')
 GROUP BY sf.id,ar.rec_idx
), identities AS (
 SELECT *, regexp_replace(coalesce(pan,''),'[^0-9]','','g') AS pan_digits
 FROM master WHERE nullif(card_code,'') IS NOT NULL
), resolved AS (
 SELECT card_code, count(DISTINCT (coalesce(card_name,''),pan_digits,coalesce(use_code,''))) AS variants,
  min(card_name) AS card_name, min(pan_digits) AS pan_digits,
  min(use_code) AS use_code, array_agg(DISTINCT source_file_id) AS source_file_ids
 FROM identities GROUP BY card_code
)
SELECT card_code, variants, source_file_ids,
 CASE WHEN variants=1 THEN regexp_replace(card_name, '[0-9][0-9 -]{{8,}}[0-9]', '[식별번호 가림]', 'g') END AS card_name,
 CASE WHEN variants=1 AND length(pan_digits) BETWEEN 13 AND 19
      THEN '****-****-****-' || right(pan_digits,4) END AS card_number_masked,
 CASE WHEN variants=1 THEN use_code END AS master_use_code
FROM resolved
"""


async def _enrich_cards(records: list[dict], tenant_id: int, company_id: int) -> None:
    if not any(record.get("card_code") for record in records):
        return
    masters = await _fetch_acct_journals(_card_master_sql(company_id), tenant_id)
    by_code = {str(row["card_code"]): row for row in masters}
    for record in records:
        code = record.get("card_code")
        if not code:
            continue
        master = by_code.get(code)
        record["card_master_status"] = "missing"
        if not master:
            continue
        record["card_master_source_file_ids"] = master["source_file_ids"]
        if int(master["variants"]) != 1:
            record["card_master_status"] = "conflict"
            continue
        record.update(card_name=master.get("card_name"),
                      card_number_masked=master.get("card_number_masked"),
                      card_master_use_code=master.get("master_use_code"),
                      card_master_status="matched" if master.get("card_number_masked") else "number_missing")


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


def _pivot_sql(company_id: int, entry_type: str, date_from: Optional[str], date_to: Optional[str], record_id: Optional[str] = None, *, limit: int = _MAX_ROWS, offset: int = 0, search: str = "", status: str = "") -> str:
    fields = _SOURCE_FIELDS
    if entry_type == "bank":
        bank_fields = {"occurred_on","seq","counterparty","status_code","deposit_amount","withdraw_amount","bank_description","bank_counterparty","item_name"}
        fields = {k:v for k,v in fields.items() if k in bank_fields}
    selects = ",\n           ".join(
        "max({column}) FILTER (WHERE ar.field_key = {key}) AS {alias}".format(
            column="ar.num_value" if is_num else "ar.raw_value",
            key=_lit(field_key),
            alias=alias,
        )
        for alias, (field_key, is_num) in fields.items()
    )
    for alias, (_, numeric) in _SOURCE_FIELDS.items():
        if alias not in fields:
            selects += f", NULL::{'numeric' if numeric else 'text'} AS {alias}"
    keys = ", ".join(_lit(field_key) for field_key, _ in fields.values())
    marker = {
        "bank": "marker.field_key IN ('deposit_amount','withdraw_amount') AND marker.num_value <> 0",
        "card": "marker.field_key='cd_ctrade' AND nullif(marker.raw_value,'') IS NOT NULL",
        "evidence": "((marker.field_key='ty_mth' AND marker.raw_value IN ('1','2')) OR (marker.field_key='cd_ctrade' AND nullif(marker.raw_value,'') IS NOT NULL))",
    }.get(entry_type, f"marker.field_key='ty_mth' AND marker.raw_value={_lit(entry_type)}")
    category_condition = {
        "card": "nullif(card_code, '') IS NOT NULL",
        "bank": "seq IS NOT NULL AND seq NOT IN ('', '0') AND (coalesce(deposit_amount,0) <> 0 OR coalesce(withdraw_amount,0) <> 0)",
        "evidence": "entry_type IN ('1','2') OR nullif(card_code,'') IS NOT NULL",
    }.get(entry_type, f"entry_type = {_lit(entry_type)}")
    conditions = ["("+category_condition+")", "occurred_on ~ '^[0-9]{8}$'", "occurred_on > '19000101'"]
    start, end = _digits(date_from), _digits(date_to)
    if start:
        conditions.append(f"occurred_on >= {_lit(start)}")
    if end:
        conditions.append(f"occurred_on <= {_lit(end)}")
    detail_filter = ""
    if record_id is not None:
        match = re.fullmatch(r"acct:(\d+):(\d+)", record_id)
        if not match:
            raise HTTPException(status_code=404, detail="원천 내역을 찾을 수 없습니다")
        detail_filter = f" WHERE source_file_id = {int(match[1])} AND rec_idx = {int(match[2])}"
    filters = []
    if search:
        filters.append(f"position(lower({_lit(search)}) in lower(concat_ws(' ',counterparty,item_name,remark,bank_description,bank_counterparty,card_code,seq))) > 0")
    if status and status not in {"전체", "전체 상태"}:
        codes = [code for code, label in _STATUS_LABEL.items() if status in label]
        if codes:
            filters.append("status_code IN (" + ",".join(_lit(code) for code in codes) + ")")
        elif status == "검토 필요":
            filters.append("coalesce(status_code,'') NOT IN ('1','2','3','5')")
        else:
            filters.append("FALSE")
    if filters:
        detail_filter += (" AND " if detail_filter else " WHERE ") + " AND ".join(filters)
    amount_sql = ("coalesce(deposit_amount,0) - coalesce(withdraw_amount,0)" if entry_type == "bank"
                  else "coalesce(nullif(total_amount,0),supply_amount,0)")
    limit, offset = max(1,min(int(limit),_MAX_ROWS)), max(0,int(offset))
    # 같은 전표가 여러 스냅샷 파일에 그대로 다시 들어온다.  2026-09-22 라일론
    # 실측에서 매입 원본 행 26,723건은 전표키(일자+일련번호) 기준으로 4,210건
    # 뿐이었고, 중복 스냅샷을 그대로 더하면 건수와 금액이 부풀었다.
    # 그래서 전표키마다 가장 나중 source_file 한 건만 남긴다.
    return (
        "WITH candidates AS MATERIALIZED (\n"
        " SELECT DISTINCT marker.source_file_id, marker.rec_idx FROM source_file sf\n"
        " JOIN atom_record marker ON marker.source_file_id=sf.id\n"
        f" WHERE sf.company_id={int(company_id)} AND sf.is_current IS TRUE AND sf.domain='wehago' AND {marker}\n"
        "), pivot AS (\n"
        "  SELECT ar.source_file_id, ar.rec_idx,\n"
        f"         {selects}\n"
        "    FROM source_file sf\n"
        "    JOIN candidates c ON c.source_file_id=sf.id\n"
        "    JOIN atom_record ar ON ar.source_file_id = sf.id AND ar.rec_idx=c.rec_idx\n"
        f"   WHERE sf.company_id = {int(company_id)}\n"
        "     AND sf.is_current IS TRUE\n"
        "     AND sf.domain = 'wehago'\n"
        f"     AND ar.field_key IN ({keys})\n"
        "   GROUP BY ar.source_file_id, ar.rec_idx\n"
        "), deduped AS (\n"
        "  SELECT DISTINCT ON (occurred_on, voucher_key) * FROM (\n"
        "    SELECT pivot.*,\n"
        "           coalesce(nullif(seq, ''), 'rec:' || source_file_id::text || ':' || rec_idx::text) AS voucher_key\n"
        "      FROM pivot\n"
        f"     WHERE {' AND '.join(conditions)}\n"
        "  ) keyed\n"
        "  ORDER BY occurred_on, voucher_key, source_file_id DESC\n"
        ")\n"
        "SELECT *, count(*) OVER() AS source_total_count,\n"
        f" sum({amount_sql}) OVER() AS source_total_amount,\n"
        f" sum(CASE WHEN status_code='2' THEN {amount_sql} ELSE 0 END) OVER() AS source_confirmed_amount,\n"
        " count(*) FILTER (WHERE coalesce(status_code,'') NOT IN ('2','3')) OVER() AS source_review_count\n"
        " FROM deduped" + detail_filter + "\n"
        " ORDER BY occurred_on DESC, voucher_key DESC\n"
        f" LIMIT {limit} OFFSET {offset}"
    )


def _iso(occurred_on: Optional[str]) -> str:
    text = str(occurred_on or "")
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}" if len(text) >= 8 else ""


def _row(row: Dict[str, Any], category: str) -> Dict[str, Any]:
    detail = str(row.get("detail_type") or "")
    evidence = str(row.get("evidence_code") or "")
    total = _decimal(row.get("total_amount"))
    if category == "bank":
        total = _decimal(row.get("deposit_amount")) - _decimal(row.get("withdraw_amount"))
    supply = _decimal(row.get("supply_amount"))
    return {
        "id": "acct:{0}:{1}".format(row.get("source_file_id"), row.get("rec_idx")),
        "category": category,
        "occurred_on": _iso(row.get("occurred_on")),
        "counterparty": str(row.get("counterparty") or row.get("bank_counterparty") or ""),
        "counterparty_biz_no": str(row.get("counterparty_biz_no") or ""),
        "description": str(row.get("item_name") or row.get("remark") or row.get("bank_description") or ""),
        "detail_type": detail,
        "detail_type_label": _DETAIL_LABEL.get(detail, detail or "미분류"),
        "evidence_label": ("전자세금계산서" if row.get("invoice_number") else _DETAIL_EVIDENCE.get(detail) or _EVIDENCE_LABEL.get(evidence) or "증빙 확인 필요"),
        "evidence_code": evidence,
        "card_code": str(row.get("card_code") or ""),
        "account_label": "원천 계좌 식별자 확인 필요" if category == "bank" else "",
        "direction": "in" if _decimal(row.get("deposit_amount")) else "out",
        "deposit_amount": _decimal(row.get("deposit_amount")),
        "withdraw_amount": _decimal(row.get("withdraw_amount")),
        "debit_account": str(row.get("debit_name") or ""),
        "credit_account": str(row.get("credit_name") or ""),
        "supply_amount": supply,
        "tax_amount": _decimal(row.get("tax_amount")),
        "total_amount": total if total else supply,
        "status_code": str(row.get("status_code") or ""),
        "status": _STATUS_LABEL.get(str(row.get("status_code") or ""), "검토 필요"),
        "source": f"acct_wehago_{category}",
        "source_file_id": row.get("source_file_id"),
        "rec_idx": row.get("rec_idx"),
        "voucher_seq": str(row.get("seq") or ""),
        "source_total_count": row.get("source_total_count"),
        "source_total_amount": row.get("source_total_amount"),
        "source_confirmed_amount": row.get("source_confirmed_amount"),
        "source_review_count": row.get("source_review_count"),
    }


async def source_transactions(
    current_user: dict,
    business_id: str,
    category: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    record_id: Optional[str] = None,
    *, limit: int = _MAX_ROWS, offset: int = 0, search: str = "", status: str = "",
) -> tuple[List[Dict[str, Any]], str]:
    """(행 목록, 원천 이름)을 돌려준다. 읽기 전용이며 계정 추정을 하지 않는다."""
    entry_type = category if category in {"bank", "card", "evidence"} else _ENTRY_TYPE.get(category)
    if not entry_type:
        raise HTTPException(status_code=400, detail="매출 또는 매입만 조회할 수 있습니다")
    scope = await _authorized_acct_scope(current_user, business_id)
    if scope is None:
        raise HTTPException(status_code=403, detail="사업자 범위가 필요합니다")
    tenant_id, company_id = scope
    rows = await _fetch_acct_journals(
        _pivot_sql(company_id, entry_type, date_from, date_to, record_id, limit=limit, offset=offset, search=search, status=status), tenant_id
    )
    records = [_row(row, category) for row in rows]
    await _enrich_cards(records, tenant_id, company_id)
    return records, f"acct.source_file/atom_record:wehago:{category}"


def _coverage_sql(company_id: int) -> str:
    """Count each representation separately: cells and snapshots are not trades.

    No raw cells, filenames, or paths leave this query. Each aggregate starts
    from company-scoped current files; joining EAV records to cells would
    multiply both counts and must never be used here.
    """
    return f"""
WITH files AS MATERIALIZED (
    SELECT id, domain FROM source_file
    WHERE company_id = {int(company_id)} AND is_current IS TRUE
), file_counts AS (
    SELECT domain, count(*) AS registered_files FROM files GROUP BY domain
), sheets AS (
    SELECT f.domain, s.source_file_id, s.id
    FROM files f JOIN atom_sheet s ON s.source_file_id = f.id
), sheet_counts AS (
    SELECT domain, count(*) AS sheets FROM sheets GROUP BY domain
), cell_counts AS (
    SELECT s.domain, count(DISTINCT s.source_file_id) AS files_with_cells,
           count(*) AS cells
    FROM sheets s JOIN atom_cell c ON c.sheet_id = s.id GROUP BY s.domain
), record_counts AS (
    SELECT f.domain, count(DISTINCT ar.source_file_id) AS files_with_records,
           count(DISTINCT (ar.source_file_id, ar.rec_idx)) AS snapshot_records
    FROM files f JOIN atom_record ar ON ar.source_file_id = f.id GROUP BY f.domain
)
SELECT f.domain, f.registered_files, coalesce(s.sheets, 0) AS sheets,
       coalesce(c.files_with_cells, 0) AS files_with_cells,
       coalesce(c.cells, 0) AS cells,
       coalesce(r.files_with_records, 0) AS files_with_records,
       coalesce(r.snapshot_records, 0) AS snapshot_records
FROM file_counts f LEFT JOIN sheet_counts s USING (domain)
LEFT JOIN cell_counts c USING (domain) LEFT JOIN record_counts r USING (domain)
ORDER BY f.domain
"""


async def source_coverage(current_user: dict, business_id: str) -> Dict[str, Any]:
    """Read-only inventory; never treats unnormalized spreadsheets as revenue."""
    scope = await _authorized_acct_scope(current_user, business_id)
    if scope is None:
        raise HTTPException(status_code=403, detail="사업자 범위가 필요합니다")
    tenant_id, company_id = scope
    rows = await _fetch_acct_journals(_coverage_sql(company_id), tenant_id)
    domains = []
    count_fields = (
        "registered_files", "sheets", "files_with_cells", "cells",
        "files_with_records", "snapshot_records",
    )
    for row in rows:
        counts = {key: int(row.get(key) or 0) for key in count_fields}
        domains.append({
            "domain": str(row.get("domain") or ""),
            **counts,
            "storage_status": (
                "cells_and_records" if counts["cells"] and counts["snapshot_records"]
                else "cells_only" if counts["cells"]
                else "records_only" if counts["snapshot_records"]
                else "registered_only"
            ),
            "spreadsheet_transaction_mapping": "not_implemented" if counts["cells"] else "not_applicable",
        })
    return {
        "business_id": business_id,
        "domains": domains,
        "scope": "all_current_files; not a transaction-date-filtered inventory",
        "complete": False,
        "warnings": [
            "파일·셀·스냅샷 수는 거래 건수 또는 확정 금액이 아닙니다.",
            "조회조건에 맞는 전표가 없어도 다른 형식의 원천 자료가 존재할 수 있습니다.",
            "엑셀 자료는 거래일·거래 ID·사업자 귀속·취소·중복 검증 후 원장에 반영해야 합니다.",
        ],
    }
