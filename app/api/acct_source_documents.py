"""Authorized read-only source explorer. Raw sheets are never booked as revenue."""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException

from app.api.acct_purchase import _authorized_acct_scope, _fetch_acct_journals, _lit


def _object(value: Any) -> dict:
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def _safe_text(value: Any) -> str:
    text = str(value or "")[:500]
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[이메일 가림]", text)
    return re.sub(r"(?<!\d)\d[\d -]{8,}\d(?!\d)", "[식별번호 가림]", text)


async def _scope(user: dict, business_id: str) -> tuple[int, int]:
    scope = await _authorized_acct_scope(user, business_id)
    if scope is None:
        raise HTTPException(403, "사업자 범위가 필요합니다")
    return scope


async def source_documents(user: dict, business_id: str, domain: str = "", offset: int = 0, limit: int = 50) -> dict:
    tenant, company = await _scope(user, business_id)
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    domain_filter = f" AND sf.domain={_lit(domain)}" if domain else ""
    rows = await _fetch_acct_journals(f"""
SELECT sf.id, sf.domain, regexp_replace(sf.abs_path, '^.*/', '') AS filename,
       sf.ext, sf.bytes, sf.mtime_kst, count(*) OVER() AS total,
       EXISTS(SELECT 1 FROM atom_record ar WHERE ar.source_file_id=sf.id) AS has_records,
       coalesce((SELECT jsonb_agg(jsonb_build_object('id',s.id,'name',s.sheet_name,
         'rows',s.max_row,'columns',s.max_col) ORDER BY s.sheet_index)
         FROM atom_sheet s WHERE s.source_file_id=sf.id), '[]'::jsonb) AS sheets
FROM source_file sf WHERE sf.company_id={company} AND sf.is_current IS TRUE {domain_filter}
ORDER BY sf.domain,sf.id LIMIT {limit} OFFSET {offset}
""", tenant)
    for row in rows:
        if isinstance(row.get("sheets"), str):
            row["sheets"] = json.loads(row["sheets"])
        row["filename"] = _safe_text(row.get("filename"))
        row["transaction_status"] = "원문 조회 가능 · 거래 합계 미포함" if row["sheets"] or row.get("has_records") else "파일 등록 · 구조화 원문 없음"
    return {"files": rows, "offset": offset, "limit": limit,
            "total": int(rows[0]["total"]) if rows else None,
            "has_more": bool(rows and offset + len(rows) < int(rows[0]["total"])),
            "notice": "파일·시트는 원천 보관 자료입니다. 월별 집계·잔액·중복 사본을 거래 합계에 더하지 않습니다."}


async def source_sheet(user: dict, business_id: str, file_id: int, sheet_id: int, after_row: int = 0, limit: int = 50) -> dict:
    tenant, company = await _scope(user, business_id)
    file_id, sheet_id = int(file_id), int(sheet_id)
    limit, after_row = max(1, min(int(limit), 100)), max(0, int(after_row))
    # Source ownership is checked before querying child cells, which have no company column.
    meta = await _fetch_acct_journals(f"""
SELECT s.id,s.sheet_name,s.max_row,s.max_col,s.source_file_id
FROM atom_sheet s JOIN source_file sf ON sf.id=s.source_file_id
WHERE sf.company_id={company} AND sf.is_current IS TRUE
 AND sf.id={file_id} AND s.id={sheet_id}
""", tenant)
    if not meta:
        raise HTTPException(404, "현재 사업자의 원천 시트를 찾을 수 없습니다")
    rows = await _fetch_acct_journals(f"""
SELECT c.row_idx, jsonb_object_agg(c.col_idx, coalesce(nullif(c.raw_text,''),c.cached_value,c.num_value::text,'')) AS cells
FROM atom_cell c JOIN atom_sheet s ON s.id=c.sheet_id
JOIN source_file sf ON sf.id=s.source_file_id
WHERE sf.company_id={company} AND sf.is_current IS TRUE
 AND sf.id={file_id} AND s.id={sheet_id} AND c.row_idx>{after_row}
GROUP BY c.row_idx ORDER BY c.row_idx LIMIT {limit+1}
""", tenant)
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["cells"] = {str(k): _safe_text(v) for k,v in _object(row["cells"]).items()}
    return {"sheet": meta[0], "rows": rows, "has_more": has_more,
            "next_after_row": rows[-1]["row_idx"] if rows else after_row,
            "notice": "저장된 셀 원문입니다. 빈 행은 생략하며 식별번호는 가립니다. 거래일·거래번호 없는 집계는 일별 거래로 변환하지 않습니다."}


async def source_snapshot(user: dict, business_id: str, file_id: int, after_record: int = -1, limit: int = 50) -> dict:
    from app.api.acct_source_ledger import _SOURCE_FIELDS
    tenant, company = await _scope(user, business_id)
    file_id, after_record, limit = int(file_id), max(-1, int(after_record)), max(1, min(int(limit), 100))
    meta = await _fetch_acct_journals(f"SELECT id,domain FROM source_file WHERE company_id={company} AND is_current IS TRUE AND id={file_id}", tenant)
    if not meta:
        raise HTTPException(404, "현재 사업자의 원천 파일을 찾을 수 없습니다")
    # Only known accounting fields. No passwords, PANs or arbitrary nested payloads.
    fields = ",".join(_lit(key) for key, _ in _SOURCE_FIELDS.values())
    rows = await _fetch_acct_journals(f"""
SELECT ar.rec_idx, jsonb_object_agg(ar.field_key,coalesce(ar.raw_value,ar.num_value::text,'')) AS fields
FROM atom_record ar JOIN source_file sf ON sf.id=ar.source_file_id
WHERE sf.company_id={company} AND sf.is_current IS TRUE AND sf.id={file_id}
 AND ar.rec_idx>{after_record} AND ar.field_key IN ({fields})
GROUP BY ar.rec_idx ORDER BY ar.rec_idx LIMIT {limit+1}
""", tenant)
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row["fields"] = {k:_safe_text(v) for k,v in _object(row["fields"]).items()}
    return {"rows":rows,"has_more":has_more,"next_after_record":rows[-1]["rec_idx"] if rows else after_record,
            "notice":"원본 스냅샷의 회계 필드입니다. 다른 파일과 중복될 수 있으므로 이 목록을 실적 합계로 사용하지 않습니다."}
