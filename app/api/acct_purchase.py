"""ACCT 매입자료 화면 API (ACCT-LAYOUT-002).

진아서버(jinah244) `acct` DB 에 적재된 위하고(WEHAGO) 카드매입 스냅샷을
매입전표 그리드·마스터·검증 결과 형태로 돌려준다.

데이터 경로
-----------
`source_file`(스냅샷 파일) → `atom_record`(EAV: rec_idx × field_key) 를
rec_idx 기준으로 pivot 해서 전표 1건을 만든다. 기본 스냅샷은
`/srv/biseo/회계비서/회계비서/.wehago/card_*.json` 중 최신 파일이며
`source_file_id` 파라미터로 바꿀 수 있다.

연결은 `ceo_chat_tools_db.query_acct_database` 를 재사용한다 —
SSH 터널 + `acct_app` 자격증명 + 읽기전용 트랜잭션이 이미 그쪽에 있고,
자격증명을 이 모듈에서 다시 다루지 않기 위해서다(R-KEY).

보안
----
- 카드번호(PAN)는 응답 단계에서 앞 4 / 뒤 4 자리만 남기고 마스킹한다.
  전체 노출 경로는 이 API 에 없다.
- 사용자 입력은 전부 `_lit()`(길이 제한 + 작은따옴표 이스케이프 + 주석/세미콜론 차단)
  또는 정수 변환을 거친 뒤에야 SQL 에 들어간다. 원문 SQL 은 받지 않는다.
"""
from __future__ import annotations

import json
from datetime import date
import logging
import os
from typing import Any, Dict, List, Optional

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.ceo_chat_tools_db import query_acct_database
from app.auth import get_current_user
from app.services import obys_upload_service as obys_upload_svc

router = APIRouter(prefix="/acct-purchase", tags=["acct-purchase"])
logger = logging.getLogger(__name__)

# 위하고 카드매입 스냅샷(기본값). /snapshots 로 다른 파일을 고를 수 있다.
_DEFAULT_SOURCE_FILE_ID = 16179
# 카드 마스터(카드사코드 → 카드사명/카드번호)가 들어 있는 스냅샷들.
_CARD_MASTER_FILE_IDS = (16130, 16186, 16011, 16028, 16094)

# 위하고 ty_jungstat 코드. 3 은 적요가 '카드중복'/'카드취소' 인 행과 일치한다.
_STATUS_LABEL = {
    "1": "미확인",
    "2": "확정",
    "3": "제외(중복/취소)",
    "5": "보류",
}

# 부족(결측) 데이터 규칙 — key: (라벨, pivot 결과에 적용할 SQL 조건, 왜 문제인가)
# 화면의 "부족 데이터" 필터와 결측 배지, /missing-summary 가 모두 이 표 하나를 쓴다.
_MISSING_RULES: Dict[str, tuple] = {
    "bizno": (
        "사업자번호 없음",
        "vendor_biz_no IS NULL",
        "거래처 원장·세무신고 귀속 불가",
    ),
    "bizno_format": (
        "사업자번호 형식 불일치",
        "vendor_biz_no IS NOT NULL AND vendor_biz_no NOT LIKE '%-%'",
        "거래처 매칭이 표기 차이로 실패",
    ),
    "remark": (
        "적요(사용내역) 없음",
        "remark IS NULL OR remark = ''",
        "지출 목적 확인 불가 — 증빙 소명 불가",
    ),
    "dept": (
        "부서·브랜드 태그 없음",
        "remark IS NULL OR remark NOT LIKE '%-%'",
        "비용 귀속처 불명 — 브랜드별 손익 산출 불가",
    ),
    "account": (
        "계정과목 없음",
        "debit_code IS NULL",
        "분개 불가 — 전표 확정 불가",
    ),
    "bizcond": (
        "업태·업종 없음",
        "vendor_biz_cond IS NULL",
        "접대비·복리후생 판정 근거 부족",
    ),
    "card": (
        "카드 미지정",
        "card_code IS NULL",
        "카드사 명세서 대조 불가",
    ),
}


# rec_idx pivot 대상 — alias: (field_key, 숫자여부)
_TXN_FIELDS: Dict[str, tuple] = {
    "txn_date": ("da_sbook", False),
    "debit_code": ("cd_acctit_cha", False),
    "debit_name": ("nm_acctit_cha", False),
    "credit_code": ("cd_acctit_dae", False),
    "credit_name": ("nm_acctit_dae", False),
    "vendor": ("nm_trade", False),
    "vendor_biz_no": ("bisocial_no", False),
    "vendor_biz_cond": ("bizcond", False),
    "remark": ("nm_remark", False),
    "domestic": ("nm_dnf", False),
    "status_code": ("ty_jungstat", False),
    "card_code": ("cd_ctrade", False),
    "supply_amount": ("mn_mnam", True),
    "vat_amount": ("mn_vat", True),
    "total_amount": ("mn_total", True),
}

_MAX_TEXT_LEN = 80
_FORBIDDEN_FRAGMENTS = (";", "--", "/*", "*/", "\x00", "\\")

# This is the CEO-approved OBYS business → ACCT tenant/company mapping.  It is
# deliberately not derived from a caller-supplied company_id: ownership is
# first checked against the JWT tenant in the OBYS business registry.
_DEFAULT_OBYS_ACCT_COMPANY_MAP = {
    "biz-junghwa": (7, 7),
    "biz-mia": (8, 8),
    "biz-sungshin": (9, 9),
    "biz-eonni-naengmyeon": (10, 10),
}


def _acct_company_map() -> Dict[str, tuple[int, int]]:
    """Read an optional deployment mapping without accepting request input."""
    raw = os.getenv("OBYS_ACCT_COMPANY_MAP", "").strip()
    if not raw:
        return _DEFAULT_OBYS_ACCT_COMPANY_MAP
    try:
        parsed = json.loads(raw)
        return {
            str(business_id): (int(value["tenant_id"]), int(value["company_id"]))
            for business_id, value in parsed.items()
        }
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.error("acct_purchase: invalid OBYS_ACCT_COMPANY_MAP")
        raise HTTPException(status_code=503, detail="ACCT 회사 매핑 설정이 올바르지 않습니다") from exc


async def _authorized_acct_scope(current_user: dict, business_id: Optional[str]) -> Optional[tuple[int, int]]:
    """Return only the ACCT scope owned by this JWT tenant; roles never bypass it."""
    if not business_id:
        return None
    business_id = str(business_id).strip()
    scope = _acct_company_map().get(business_id)
    if not scope:
        raise HTTPException(status_code=403, detail="이 사업자에는 ACCT 회사 매핑이 없습니다")
    tenant_id = obys_upload_svc._tenant(current_user)
    conn = await obys_upload_svc._connect()
    try:
        await obys_upload_svc._require_business(conn, tenant_id, business_id)
    finally:
        await conn.close()
    return scope


def _lit(value: str) -> str:
    """사용자 입력을 SQL 문자열 리터럴로 안전하게 바꾼다."""
    text = str(value)[:_MAX_TEXT_LEN]
    for bad in _FORBIDDEN_FRAGMENTS:
        if bad in text:
            raise HTTPException(
                status_code=400, detail="검색어에 허용되지 않는 문자가 포함돼 있습니다"
            )
    return "'" + text.replace("'", "''") + "'"


def _int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _mask_pan(pan: Optional[str]) -> Optional[str]:
    """카드번호를 앞 4 / 뒤 4 자리만 남기고 가린다."""
    if not pan:
        return None
    digits = [c for c in pan if c.isdigit()]
    if len(digits) < 8:
        return "*" * len(pan)
    head, tail = "".join(digits[:4]), "".join(digits[-4:])
    return f"{head}-****-****-{tail}"


async def _fetch(sql: str) -> List[Dict[str, Any]]:
    result = await query_acct_database(sql)
    if isinstance(result, dict) and result.get("error"):
        logger.error("acct_purchase: ACCT 조회 실패 | %s", result["error"])
        raise HTTPException(status_code=502, detail=f"ACCT DB 조회 실패: {result['error']}")
    return list(result.get("rows") or [])


async def _fetch_acct_journals(sql: str, acct_tenant_id: int) -> List[Dict[str, Any]]:
    """ACCT journal reads always carry the RLS tenant selected from JWT scope."""
    result = await query_acct_database(sql, acct_tenant_id=str(acct_tenant_id))
    if isinstance(result, dict) and result.get("error"):
        logger.error("acct_purchase: ACCT journal 조회 실패 | %s", result["error"])
        raise HTTPException(status_code=502, detail="ACCT 전표 조회 실패")
    return list(result.get("rows") or [])


def _journal_value(entry: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if entry.get(name) is not None:
            return entry[name]
    return None


def _journal_entry_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    entry = row.get("entry") or row
    if isinstance(entry, str):
        entry = json.loads(entry)
    entry = dict(entry)
    return {
        "id": _journal_value(entry, "id", "entry_id"),
        "company_id": _journal_value(entry, "company_id"),
        "entry_date": _journal_value(entry, "entry_date", "posted_date", "transaction_date"),
        "entry_no": _journal_value(entry, "entry_no", "journal_no", "voucher_no", "number"),
        "description": _journal_value(entry, "description", "memo", "narration"),
        "status": _journal_value(entry, "status", "state"),
        "posted_at": _journal_value(entry, "posted_at"),
        "created_at": _journal_value(entry, "created_at"),
    }


def _journal_line_totals(lines: List[Dict[str, Any]]) -> tuple[float, float]:
    debit = credit = 0.0
    for line in lines:
        side = str(line.get("side") or "").lower()
        amount = line.get("amount")
        try:
            if side == "debit":
                debit += float(amount or 0)
            elif side == "credit":
                credit += float(amount or 0)
            else:
                debit += float(line.get("debit_amount") or 0)
                credit += float(line.get("credit_amount") or 0)
        except (TypeError, ValueError):
            continue
    return debit, credit


def _pivot_cte(source_file_id: int) -> str:
    """atom_record EAV 를 rec_idx 단위 전표로 pivot 하는 CTE 본문."""
    parts = []
    for alias, (field_key, is_num) in _TXN_FIELDS.items():
        column = "num_value" if is_num else "raw_value"
        parts.append(
            f"max({column}) FILTER (WHERE field_key = '{field_key}') AS {alias}"
        )
    body = ",\n           ".join(parts)
    return (
        "    SELECT rec_idx,\n           "
        + body
        + f"\n      FROM atom_record\n     WHERE source_file_id = {source_file_id}\n"
        "     GROUP BY rec_idx"
    )


def _where(
    ym: Optional[str],
    status: Optional[str],
    account: Optional[str],
    vendor: Optional[str],
    remark: Optional[str],
    domestic: Optional[str],
    card_code: Optional[str],
    min_amount: Optional[int],
    missing: Optional[str] = None,
) -> str:
    conds: List[str] = []
    if ym:
        digits = "".join(ch for ch in str(ym) if ch.isdigit())[:6]
        if len(digits) == 6:
            conds.append(f"substring(txn_date, 1, 6) = '{digits}'")
    if status and status in _STATUS_LABEL:
        conds.append(f"status_code = '{status}'")
    if account:
        conds.append(f"(debit_code = {_lit(account)} OR debit_name = {_lit(account)})")
    if vendor:
        conds.append(
            f"(vendor ILIKE '%' || {_lit(vendor)} || '%'"
            f" OR coalesce(vendor_biz_no, '') ILIKE '%' || {_lit(vendor)} || '%')"
        )
    if remark:
        conds.append(f"remark ILIKE '%' || {_lit(remark)} || '%'")
    if domestic in ("국내", "해외"):
        conds.append(f"domestic = '{domestic}'")
    if card_code:
        conds.append(f"card_code = {_lit(card_code)}")
    if min_amount:
        conds.append(f"coalesce(total_amount, 0) >= {_int(min_amount, 0, 10**12, 0)}")
    if missing:
        key = str(missing).strip()
        if key == "any":
            conds.append(
                "(" + " OR ".join(f"({rule[1]})" for rule in _MISSING_RULES.values()) + ")"
            )
        elif key in _MISSING_RULES:
            conds.append(f"({_MISSING_RULES[key][1]})")
        else:
            raise HTTPException(
                status_code=400,
                detail=f"missing 은 any 또는 {', '.join(_MISSING_RULES)} 중 하나여야 합니다",
            )
    return (" WHERE " + " AND ".join(conds)) if conds else ""


def _missing_fields(row: Dict[str, Any]) -> List[str]:
    """전표 1행에서 비어 있는 항목의 라벨을 뽑는다(화면 배지·검색 결과 표시용)."""
    biz = row.get("vendor_biz_no")
    remark = row.get("remark")
    found: List[str] = []
    if not biz:
        found.append("bizno")
    elif "-" not in str(biz):
        found.append("bizno_format")
    if not remark:
        found.append("remark")
    elif "-" not in str(remark):
        found.append("dept")
    if not row.get("debit_code"):
        found.append("account")
    if not row.get("vendor_biz_cond"):
        found.append("bizcond")
    if not row.get("card_code"):
        found.append("card")
    return found


async def _card_master() -> Dict[str, Dict[str, Any]]:
    """카드사코드 → {카드사명, 마스킹된 카드번호}."""
    ids = ", ".join(str(i) for i in _CARD_MASTER_FILE_IDS)
    rows = await _fetch(
        "WITH p AS (\n"
        "    SELECT source_file_id, rec_idx,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'cd_ctrade') AS card_code,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'nm_ctrade') AS card_name,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'id_sa') AS card_no\n"
        f"      FROM atom_record WHERE source_file_id IN ({ids})\n"
        "     GROUP BY source_file_id, rec_idx\n"
        ")\n"
        "SELECT card_code, card_name, card_no, count(*) AS n\n"
        "  FROM p WHERE card_code IS NOT NULL AND card_no IS NOT NULL\n"
        " GROUP BY card_code, card_name, card_no ORDER BY card_code, n DESC"
    )
    master: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = (row.get("card_code") or "").strip()
        if not code or code in master:
            continue
        master[code] = {
            "card_code": code,
            "card_name": row.get("card_name") or "미등록 카드",
            "card_no_masked": _mask_pan(row.get("card_no")),
        }
    return master


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _journal_date(value: Optional[str], field: str) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} 날짜 형식이 올바르지 않습니다") from exc


@router.get("/journals")
async def list_journals(
    business_id: Optional[str] = Query(None, max_length=64),
    company_id: Optional[int] = Query(None),
    period_key: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    entry_date_from: Optional[str] = Query(None),
    entry_date_to: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1000000),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """List posted ACCT journals for one JWT-owned OBYS business only.

    A missing business scope intentionally returns an empty successful result;
    this keeps a newly provisioned tenant (with no ACCT company yet) readable
    without treating the absence of entries as an error.
    """
    scope = await _authorized_acct_scope(current_user, business_id)
    if scope is None:
        return {"journals": [], "count": 0, "limit": limit, "offset": offset}
    acct_tenant_id, mapped_company_id = scope
    if company_id is not None and company_id != mapped_company_id:
        raise HTTPException(status_code=403, detail="요청한 ACCT 회사는 현재 사업자에 매핑되지 않습니다")
    date_from = _journal_date(entry_date_from, "entry_date_from")
    date_to = _journal_date(entry_date_to, "entry_date_to")
    if period_key:
        date_from = date_from or f"{period_key}-01"
        year, month = (int(part) for part in period_key.split("-"))
        date_to = date_to or date(year + (month == 12), month % 12 + 1, 1).isoformat()
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="전표 시작일은 종료일보다 뒤일 수 없습니다")
    conditions = [f"e.company_id = {mapped_company_id}"]
    if date_from:
        conditions.append(f"e.entry_date >= {_lit(date_from)}::date")
    if date_to:
        # period_key's generated end is exclusive; explicit end dates include the day.
        operator = "<" if period_key and entry_date_to is None else "<="
        conditions.append(f"e.entry_date {operator} {_lit(date_to)}::date")
    rows = await _fetch_acct_journals(
        "SELECT to_jsonb(e) AS entry FROM journal_entry e WHERE "
        + " AND ".join(conditions)
        + f" ORDER BY e.entry_date DESC, e.created_at DESC LIMIT {limit} OFFSET {offset}",
        acct_tenant_id,
    )
    journals = [_journal_entry_summary(row) for row in rows]
    return {"journals": journals, "count": len(journals), "limit": limit, "offset": offset}


@router.get("/journal-entries/{entry_id}")
async def get_journal_entry(
    entry_id: str,
    business_id: Optional[str] = Query(None, max_length=64),
    company_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return one ACCT journal and all lines after tenant/company verification."""
    scope = await _authorized_acct_scope(current_user, business_id)
    if scope is None:
        return {"journal": None, "lines": [], "debit_total": 0, "credit_total": 0}
    acct_tenant_id, mapped_company_id = scope
    if company_id is not None and company_id != mapped_company_id:
        raise HTTPException(status_code=403, detail="요청한 ACCT 회사는 현재 사업자에 매핑되지 않습니다")
    safe_entry_id = _lit(entry_id)
    entry_rows = await _fetch_acct_journals(
        "SELECT to_jsonb(e) AS entry FROM journal_entry e "
        f"WHERE e.id::text = {safe_entry_id} AND e.company_id = {mapped_company_id} LIMIT 1",
        acct_tenant_id,
    )
    if not entry_rows:
        # Absence (including a foreign ID) is deliberately indistinguishable.
        return {"journal": None, "lines": [], "debit_total": 0, "credit_total": 0}
    line_rows = await _fetch_acct_journals(
        "SELECT to_jsonb(l) AS line FROM journal_line l "
        f"WHERE l.entry_id::text = {safe_entry_id} ORDER BY l.id",
        acct_tenant_id,
    )
    lines = []
    for row in line_rows:
        line = row.get("line") or row
        lines.append(json.loads(line) if isinstance(line, str) else dict(line))
    debit_total, credit_total = _journal_line_totals(lines)
    return {
        "journal": _journal_entry_summary(entry_rows[0]),
        "lines": lines,
        "debit_total": debit_total,
        "credit_total": credit_total,
    }


# 화면 자체는 데이터를 담고 있지 않다(빈 껍데기 + JS). 데이터 엔드포인트는 전부 인증을
# 요구하므로 이 라우트는 열어 둔다. aads.newtalk.kr 의 /static 은 대시보드가 가져가므로,
# 회계 데이터가 붙는 도메인에서 같은 출처로 화면을 서빙하기 위해 API 쪽에 둔다.
_UI_PATH = Path(__file__).resolve().parents[1] / "static" / "acct" / "purchase.html"


@router.get("/ui", include_in_schema=False)
async def ui() -> FileResponse:
    """매입자료 관리 화면(HTML 셸)."""
    if not _UI_PATH.exists():
        raise HTTPException(status_code=404, detail="화면 파일을 찾을 수 없습니다")
    return FileResponse(_UI_PATH, media_type="text/html")


@router.get("/snapshots")
async def list_snapshots(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """선택 가능한 카드매입 스냅샷 파일 목록."""
    rows = await _fetch(
        "SELECT id, abs_path, bytes, mtime_kst\n"
        "  FROM source_file\n"
        " WHERE domain = 'wehago' AND abs_path ILIKE '%card%'\n"
        " ORDER BY mtime_kst DESC LIMIT 30"
    )
    return {"default_source_file_id": _DEFAULT_SOURCE_FILE_ID, "snapshots": rows}


@router.get("/transactions")
async def list_transactions(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    ym: Optional[str] = None,
    status: Optional[str] = None,
    account: Optional[str] = None,
    vendor: Optional[str] = None,
    remark: Optional[str] = None,
    domestic: Optional[str] = None,
    card_code: Optional[str] = None,
    min_amount: Optional[int] = None,
    missing: Optional[str] = Query(
        None,
        description="부족 데이터만 조회. any 또는 bizno/bizno_format/remark/dept/account/bizcond/card",
    ),
    limit: int = 100,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """매입전표 리스트 — 계정과목·사용내역(적요)·카드번호·부가세까지 전 필드.

    각 행에 `missing_fields`(비어 있는 항목)를 함께 내려보내 화면이 결측을
    배지로 표시할 수 있게 한다. `missing` 파라미터로 결측 행만 골라 볼 수 있다.
    """
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    take = _int(limit, 1, 500, 100)
    skip = _int(offset, 0, 10**6, 0)
    where = _where(ym, status, account, vendor, remark, domestic, card_code,
                   min_amount, missing)

    base = f"WITH p AS (\n{_pivot_cte(sfid)}\n)"
    rows = await _fetch(
        f"{base}\nSELECT * FROM p{where}\n ORDER BY txn_date, rec_idx\n"
        f" LIMIT {take} OFFSET {skip}"
    )
    totals = await _fetch(
        f"{base}\nSELECT count(*) AS cnt,\n"
        "       sum(coalesce(supply_amount, 0))::bigint AS supply,\n"
        "       sum(coalesce(vat_amount, 0))::bigint AS vat,\n"
        "       sum(coalesce(total_amount, 0))::bigint AS total\n"
        f"  FROM p{where}"
    )
    cards = await _card_master()

    items = []
    for row in rows:
        code = (row.get("card_code") or "").strip()
        card = cards.get(code, {})
        status_code = (row.get("status_code") or "").strip()
        raw_date = (row.get("txn_date") or "").strip()
        items.append(
            {
                "rec_idx": row.get("rec_idx"),
                "txn_date": (
                    f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                    if len(raw_date) == 8
                    else raw_date
                ),
                "voucher_no": f"{raw_date}-{str(row.get('rec_idx') or 0).zfill(4)}",
                "card_code": code or None,
                "card_name": card.get("card_name"),
                "card_no_masked": card.get("card_no_masked"),
                "vendor": row.get("vendor"),
                "vendor_biz_no": row.get("vendor_biz_no"),
                "vendor_biz_cond": row.get("vendor_biz_cond"),
                "domestic": row.get("domestic"),
                "remark": row.get("remark"),
                "debit_code": row.get("debit_code"),
                "debit_name": row.get("debit_name"),
                "credit_code": row.get("credit_code"),
                "credit_name": row.get("credit_name"),
                "supply_amount": _to_int(row.get("supply_amount")),
                "vat_amount": _to_int(row.get("vat_amount")),
                "total_amount": _to_int(row.get("total_amount")),
                "status_code": status_code or None,
                "status_label": _STATUS_LABEL.get(status_code, "기타"),
                "missing_fields": _missing_fields(row),
            }
        )

    summary = totals[0] if totals else {}
    return {
        "source_file_id": sfid,
        "count": len(items),
        "offset": skip,
        "limit": take,
        "totals": {
            "count": _to_int(summary.get("cnt")),
            "supply_amount": _to_int(summary.get("supply")),
            "vat_amount": _to_int(summary.get("vat")),
            "total_amount": _to_int(summary.get("total")),
        },
        "items": items,
    }


@router.get("/summary")
async def summary(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """전표상태별 건수·금액 (KPI 스트립)."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    rows = await _fetch(
        f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
        "SELECT status_code, count(*) AS cnt,\n"
        "       sum(coalesce(supply_amount, 0))::bigint AS supply,\n"
        "       sum(coalesce(vat_amount, 0))::bigint AS vat,\n"
        "       sum(coalesce(total_amount, 0))::bigint AS total\n"
        "  FROM p GROUP BY status_code ORDER BY cnt DESC"
    )
    buckets = [
        {
            "status_code": row.get("status_code"),
            "status_label": _STATUS_LABEL.get(
                (row.get("status_code") or "").strip(), "기타"
            ),
            "count": _to_int(row.get("cnt")),
            "supply_amount": _to_int(row.get("supply")),
            "vat_amount": _to_int(row.get("vat")),
            "total_amount": _to_int(row.get("total")),
        }
        for row in rows
    ]
    return {
        "source_file_id": sfid,
        "total_count": sum(b["count"] for b in buckets),
        "total_amount": sum(b["total_amount"] for b in buckets),
        "buckets": buckets,
    }


@router.get("/accounts")
async def accounts(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """계정과목 마스터 — 차변 기준 실사용 계정과 사용빈도/금액."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    rows = await _fetch(
        f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
        "SELECT debit_code AS code, debit_name AS name, count(*) AS cnt,\n"
        "       sum(coalesce(total_amount, 0))::bigint AS total\n"
        "  FROM p WHERE debit_code IS NOT NULL\n"
        " GROUP BY debit_code, debit_name ORDER BY cnt DESC"
    )
    return {
        "source_file_id": sfid,
        "items": [
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "count": _to_int(row.get("cnt")),
                "total_amount": _to_int(row.get("total")),
            }
            for row in rows
        ],
    }


@router.get("/cards")
async def cards(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """법인카드 마스터 — 카드번호는 마스킹된 값만 내려간다."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    usage = await _fetch(
        f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
        "SELECT card_code, count(*) AS cnt,\n"
        "       sum(coalesce(total_amount, 0))::bigint AS total\n"
        "  FROM p WHERE card_code IS NOT NULL GROUP BY card_code"
    )
    master = await _card_master()
    used = {(row.get("card_code") or "").strip(): row for row in usage}
    items = []
    for code, card in master.items():
        row = used.get(code, {})
        items.append(
            {
                **card,
                "count": _to_int(row.get("cnt")),
                "total_amount": _to_int(row.get("total")),
            }
        )
    for code, row in used.items():
        if code not in master:
            items.append(
                {
                    "card_code": code,
                    "card_name": "마스터 미등록",
                    "card_no_masked": None,
                    "count": _to_int(row.get("cnt")),
                    "total_amount": _to_int(row.get("total")),
                }
            )
    items.sort(key=lambda x: x["count"], reverse=True)
    return {"source_file_id": sfid, "items": items}


@router.get("/vendors")
async def vendors(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """거래처 원장 — 금액 상위 순."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    take = _int(limit, 1, 300, 50)
    rows = await _fetch(
        f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
        "SELECT vendor, vendor_biz_no, vendor_biz_cond, domestic,\n"
        "       count(*) AS cnt, sum(coalesce(total_amount, 0))::bigint AS total\n"
        "  FROM p WHERE vendor IS NOT NULL\n"
        " GROUP BY vendor, vendor_biz_no, vendor_biz_cond, domestic\n"
        f" ORDER BY total DESC NULLS LAST LIMIT {take}"
    )
    return {
        "source_file_id": sfid,
        "items": [
            {
                "vendor": row.get("vendor"),
                "biz_no": row.get("vendor_biz_no"),
                "biz_cond": row.get("vendor_biz_cond"),
                "domestic": row.get("domestic"),
                "count": _to_int(row.get("cnt")),
                "total_amount": _to_int(row.get("total")),
            }
            for row in rows
        ],
    }


@router.get("/monthly")
async def monthly(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """회계월별 건수·확정금액·전체금액."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    rows = await _fetch(
        f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
        "SELECT substring(txn_date, 1, 6) AS ym, count(*) AS cnt,\n"
        "       sum(coalesce(total_amount, 0)) FILTER (WHERE status_code = '2')::bigint AS confirmed,\n"
        "       sum(coalesce(total_amount, 0))::bigint AS total\n"
        "  FROM p WHERE txn_date IS NOT NULL GROUP BY 1 ORDER BY 1"
    )
    return {
        "source_file_id": sfid,
        "items": [
            {
                "ym": row.get("ym"),
                "count": _to_int(row.get("cnt")),
                "confirmed_amount": _to_int(row.get("confirmed")),
                "total_amount": _to_int(row.get("total")),
            }
            for row in rows
        ],
    }


@router.get("/missing-summary")
async def missing_summary(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """부족(결측) 데이터 현황 — 항목별 건수·비율과 바로 쓸 검색 키."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    filters = ",\n".join(
        f"       count(*) FILTER (WHERE {rule[1]}) AS {key}"
        for key, rule in _MISSING_RULES.items()
    )
    any_cond = " OR ".join(f"({rule[1]})" for rule in _MISSING_RULES.values())
    row = (
        await _fetch(
            f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
            "SELECT count(*) AS total,\n"
            f"{filters},\n"
            f"       count(*) FILTER (WHERE {any_cond}) AS any_missing\n"
            "  FROM p"
        )
    )[0]

    total = _to_int(row.get("total"))
    items = []
    for key, (label, _cond, impact) in _MISSING_RULES.items():
        cnt = _to_int(row.get(key))
        items.append(
            {
                "key": key,
                "label": label,
                "impact": impact,
                "count": cnt,
                "ratio_percent": round(cnt / total * 100, 1) if total else 0.0,
                "filter": f"missing={key}",
            }
        )
    items.sort(key=lambda x: x["count"], reverse=True)
    any_missing = _to_int(row.get("any_missing"))
    return {
        "source_file_id": sfid,
        "total": total,
        "any_missing": any_missing,
        "complete": total - any_missing,
        "complete_ratio_percent": round((total - any_missing) / total * 100, 1) if total else 0.0,
        "items": items,
    }


@router.get("/validations")
async def validations(
    source_file_id: int = Query(_DEFAULT_SOURCE_FILE_ID),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """전표 확정 전 검증 규칙 — 건수는 스냅샷 실집계."""
    sfid = _int(source_file_id, 1, 10**9, _DEFAULT_SOURCE_FILE_ID)
    row = (
        await _fetch(
            f"WITH p AS (\n{_pivot_cte(sfid)}\n)\n"
            "SELECT count(*) FILTER (WHERE status_code = '3') AS dup_cancel,\n"
            "       count(*) FILTER (WHERE status_code = '1') AS unconfirmed,\n"
            "       count(*) FILTER (WHERE status_code = '5') AS on_hold,\n"
            "       count(*) FILTER (WHERE domestic = '해외'"
            " AND coalesce(vat_amount, 0) <> 0) AS oversea_vat,\n"
            "       count(*) FILTER (WHERE vendor_biz_no IS NULL) AS no_biz_no,\n"
            "       count(*) FILTER (WHERE vendor_biz_no IS NOT NULL"
            " AND vendor_biz_no NOT LIKE '%-%') AS biz_no_format,\n"
            "       count(*) FILTER (WHERE remark IS NULL OR remark = '') AS no_remark,\n"
            "       count(*) FILTER (WHERE card_code IS NULL) AS no_card,\n"
            "       count(*) AS total\n"
            "  FROM p"
        )
    )[0]

    def rule(code: str, title: str, level: str, count: int, action: str) -> Dict[str, Any]:
        return {
            "code": code,
            "title": title,
            "level": level,
            "count": count,
            "action": action,
        }

    return {
        "source_file_id": sfid,
        "total_count": _to_int(row.get("total")),
        "rules": [
            rule("V-01", "카드 중복/취소 전표", "error", _to_int(row.get("dup_cancel")),
                 "제외 처리 후 원전표만 확정"),
            rule("V-02", "미확인 전표(계정·거래처 미확정)", "warn", _to_int(row.get("unconfirmed")),
                 "자동분개 재실행 후 수기 보정"),
            rule("V-03", "보류 전표", "warn", _to_int(row.get("on_hold")),
                 "담당자 확인 요청"),
            rule("V-04", "해외 결제분에 부가세가 인식됨", "error", _to_int(row.get("oversea_vat")),
                 "대리납부/불공제 판정 후 세액 정정"),
            rule("V-05", "거래처 사업자번호 미등록", "warn", _to_int(row.get("no_biz_no")),
                 "거래처 마스터 신규 등록"),
            rule("V-06", "사업자번호 형식 불일치(하이픈 없음)", "warn",
                 _to_int(row.get("biz_no_format")), "정규화 배치 실행"),
            rule("V-07", "적요(사용내역) 누락", "warn", _to_int(row.get("no_remark")),
                 "사용 목적·부서 입력"),
            rule("V-08", "카드 미지정 전표", "warn", _to_int(row.get("no_card")),
                 "카드 마스터 매핑"),
        ],
    }
