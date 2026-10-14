"""클로브 수집 계약의 순수 로직(DB·네트워크 없음): 정규화·해시·대사·회사 연결 판정.

원장 확정과 무관하다 — 여기서 나온 값은 모두 검토함 등록 전의 후보다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# 수집 종류 → 클로브 조회 도구. 도구 이름의 정본은 clobe_mcp_client.COLLECTION_DATA_TOOLS.
KIND_BANK = "bank_transaction"
KIND_TAX = "tax_invoice"
KIND_CASH = "cash_receipt"
KIND_CARD = "card_approval"
DATA_KINDS = (KIND_BANK, KIND_TAX, KIND_CASH, KIND_CARD)
# 판매채널 매출(배달앱·카드·PG·오픈마켓)은 매출 분석용이다. 장부 확정·전표 대상이 아니므로 DATA_KINDS(확정 가능 종류)에
# 넣지 않고, 수집 대상 목록(COLLECTION_KINDS)에만 둔다. 확정 경로는 DATA_KINDS 만 허용한다.
KIND_CHANNEL = "channel_revenue"
COLLECTION_KINDS = DATA_KINDS + (KIND_CHANNEL,)
CHANNEL_TOOL = "get_monthly_revenue"
#: 채널을 클로브에 연결하지 않아 빈 응답을 받았다는 표시(collection_state.last_error_code·status 로 쓰이며 화면에서 연결 안내가 된다).
CHANNEL_NOT_CONNECTED = "channel_not_connected"
SCRAPE_CATEGORY = {
    KIND_BANK: "BANK",
    KIND_TAX: "TAX_INVOICE",
    KIND_CASH: "CASH_RECEIPT",
    KIND_CARD: "CARD_APPROVAL",
}
# 위택스 납부확인서는 클로브 MCP 에 조회 도구가 없다 — 정확한 증빙을 흉내 내지 않고 파일 입력으로 돌린다.
UNSUPPORTED_SOURCES = (
    {
        "kind": "wetax_tax_payment_certificate",
        "supported": False,
        "reason": "clobe_mcp_has_no_tool",
        "input_mode": "file_upload",
        "status": "file_input_required",
    },
)

PAGE_SIZE = 100
MAX_PAGES = 500
MAX_WINDOW_DAYS = 366
DEFAULT_WINDOW_DAYS = 90
SUM_TOLERANCE = Decimal("0.01")

#: 사람이 확정한 제외 범위. 환경변수로 늘릴 수는 있어도 기본값 아래로는 못 줄인다.
_DEFAULT_BLOCKED_TENANTS = frozenset({"d1695f15-6b68-4929-bc8d-646827363ff9"})  # 라일론
_DEFAULT_BLOCKED_BUSINESSES = frozenset({"biz-lylon-e2e"})
_DEFAULT_ALLOWED_TENANTS = frozenset({"15055cac-71b0-45ec-b714-7093dde189ff"})  # 열정국밥 계열 레거시 테넌트

_PLACEHOLDERS = frozenset({"-", "미등록", "기초등록 필요"})


def _env_set(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


def blocked_tenants() -> frozenset[str]:
    return _DEFAULT_BLOCKED_TENANTS | _env_set("OBYS_COLLECTION_BLOCKED_TENANT_IDS")


def blocked_businesses() -> frozenset[str]:
    return _DEFAULT_BLOCKED_BUSINESSES | _env_set("OBYS_COLLECTION_BLOCKED_BUSINESS_IDS")


def allowed_tenants() -> frozenset[str]:
    return (_DEFAULT_ALLOWED_TENANTS | _env_set("OBYS_CLOBE_COLLECTION_TENANT_IDS")) - blocked_tenants()


def tenant_in_scope(tenant_id: Any) -> bool:
    t = str(tenant_id or "").strip()
    return bool(t) and t in allowed_tenants() and t not in blocked_tenants()


# ── 회사 ↔ 사업자 연결 판정 ───────────────────────────────

def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def reg_no_usable(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text not in _PLACEHOLDERS and len(digits(text)) == 10


def _name_key(value: Any) -> str:
    text = re.sub(r"\(주\)|주식회사|\(유\)|유한회사|㈜", "", str(value or ""))
    return re.sub(r"[\s_\-.()]+", "", text).lower()


def names_compatible(a: Any, b: Any) -> bool:
    ka, kb = _name_key(a), _name_key(b)
    return bool(ka) and bool(kb) and (ka == kb or (min(len(ka), len(kb)) >= 3 and (ka in kb or kb in ka)))


def decide_link(company: dict[str, Any], businesses: list[dict[str, Any]]) -> dict[str, Any]:
    """clobe 회사 하나에 대한 연결 판정. 자동 연결은 '사업자번호 정확 일치 + 상호 호환' 한 경우뿐이다.

    - 차단 범위(라일론 테넌트·사업자)와 사업자번호가 겹치면 blocked.
    - 번호만 같고 상호가 다르면(다른 사업장일 수 있다) review.
    - 상호만 닮은 경우는 후보로만 올리고 review. 사람이 근거를 남겨 승인해야 한다.
    """
    reg = digits(company.get("reg_no"))
    name = company.get("name")
    bt, bb = blocked_tenants(), blocked_businesses()
    scoped = [b for b in businesses if str(b.get("tenant_id")) not in bt and b.get("id") not in bb]
    excluded = [b for b in businesses if str(b.get("tenant_id")) in bt or b.get("id") in bb]
    if reg and any(digits(b.get("registration_no")) == reg for b in excluded if reg_no_usable(b.get("registration_no"))):
        return {"status": "blocked", "basis": "excluded_scope", "business": None, "candidates": []}

    in_scope = [b for b in scoped if tenant_in_scope(b.get("tenant_id"))]
    exact = [b for b in in_scope if reg and reg_no_usable(b.get("registration_no")) and digits(b.get("registration_no")) == reg]
    exact_named = [b for b in exact if names_compatible(name, b.get("name"))]
    if len(exact_named) == 1 and len(exact) == 1:
        return {"status": "linked", "basis": "reg_no_exact", "business": exact_named[0], "candidates": []}
    if exact:
        return {"status": "review", "basis": "reg_no_match_needs_review",
                "business": None, "candidates": [b["id"] for b in exact]}
    named = [b for b in in_scope if names_compatible(name, b.get("name"))]
    if named:
        return {"status": "review", "basis": "name_only_candidate",
                "business": None, "candidates": [b["id"] for b in named]}
    return {"status": "review", "basis": "no_candidate", "business": None, "candidates": []}


def check_admin_approval(company: dict[str, Any], business: dict[str, Any], *, name_evidence: bool) -> tuple[bool, str]:
    """관리자 승인 가능 여부. 사업자번호가 서로 다르면 어떤 근거로도 거부한다."""
    if str(business.get("tenant_id")) in blocked_tenants() or business.get("id") in blocked_businesses():
        return False, "business_blocked"
    if not tenant_in_scope(business.get("tenant_id")):
        return False, "tenant_not_in_collection_scope"
    c_reg, b_reg = digits(company.get("reg_no")), business.get("registration_no")
    if c_reg and reg_no_usable(b_reg):
        if digits(b_reg) != c_reg:
            return False, "reg_no_conflict"
        return True, "reg_no_exact_admin"
    if not names_compatible(company.get("name"), business.get("name")):
        return False, "name_mismatch"
    if not name_evidence:
        return False, "name_evidence_required"
    return True, "admin_name_evidence"


# ── 항목 정규화 ──────────────────────────────────────────

# 클로브 쪽에서 분류·매칭 작업으로 바뀌는 값. 해시에서 빼야 같은 거래가 새 버전으로 쌓이지 않는다.
_VOLATILE = {
    KIND_BANK: {
        "labelAmount", "labelAmountKrw", "categoryId", "category", "businessEntityId", "businessEntityName",
        "groupLabelId", "groupLabelPath", "customLabel", "hasEvidence", "evidenceType",
        "linkedTaxInvoiceId", "linkedTaxInvoiceType", "isUnclassified", "memo",
    },
    KIND_TAX: {"settlementStatus", "mappedAmount", "unmappedAmount", "matchedCount", "matchedTransactions", "memo"},
    KIND_CASH: {"memo"},
    KIND_CARD: {"memo"},
    KIND_CHANNEL: {"source"},
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def item_hash(kind: str, raw: dict[str, Any]) -> str:
    stable = {k: v for k, v in raw.items() if k not in _VOLATILE.get(kind, set())}
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # SQL round(numeric, 2) 와 같은 규칙
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if raw.get(key) not in (None, ""):
            return raw[key]
    return None


_CARD_VAT_KEYS = ("vat", "vatAmount")


def is_card_cancelled(raw: dict[str, Any]) -> bool:
    """클로브 카드승인의 cancelType 이 NONE(또는 비어 있음)이 아니면 취소 건이다."""
    return str(raw.get("cancelType") or "NONE").strip().upper() not in ("", "NONE")


def _card_is_foreign(raw: dict[str, Any]) -> bool:
    return str(raw.get("homeForeignType") or "DOMESTIC").strip().upper() not in ("", "DOMESTIC")


def _card_signed(value: Decimal | None, raw: dict[str, Any]) -> Decimal | None:
    """취소 건은 -abs(값). 원본 부호가 양수든 음수든 같은 결과라 이중 반전이 없다."""
    return -abs(value) if value is not None and is_card_cancelled(raw) else value


def card_amount(raw: dict[str, Any]) -> Decimal | None:
    """카드승인 금액(원화). 실측 키는 usedAmountKrw(2026-10-12 운영 154건).

    해외(homeForeignType 이 DOMESTIC 이 아님)는 usedAmount 가 외화일 수 있어 usedAmountKrw 만 쓴다.
    취소 건은 -abs(금액): 현금영수증 취소와 같은 '취소는 음수' 관례이고 tax_invoice 의 음수 금액과도 맞는다.
    취소 건의 원본 부호는 운영 표본이 없어 abs 로 정규화한다(원본이 이미 음수여도 이중 반전하지 않는다).
    합계 대사(row_sum_value / row_sum_sql)도 이 선택·부호 규칙을 그대로 쓴다.
    """
    keys = ("usedAmountKrw",) if _card_is_foreign(raw) else ("usedAmountKrw", "usedAmount", "amount")
    return _card_signed(_dec(_first(raw, *keys)), raw)


def card_vat(raw: dict[str, Any]) -> Decimal | None:
    return _card_signed(_dec(_first(raw, *_CARD_VAT_KEYS)), raw)


_BANK_COUNTERPARTY_KEYS = ("businessEntityName", "transactionDescription", "transactionName")


def bank_counterparty(raw: dict[str, Any]) -> str:
    """은행거래 거래처. 2026-10-14 운영 실측: transactionName 은 항상 ''이고 businessEntityName(있을 때만) /
    transactionDescription(항상 값 있음)에 실제 값이 있다. 빈 문자열·공백만 있는 값은 건너뛴다."""
    for key in _BANK_COUNTERPARTY_KEYS:
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_item(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    """종류별 필드 → 공통 열. source_key 가 None 이면 호출부가 fingerprint+ordinal 로 채운다."""
    if kind == KIND_BANK:
        in_amt, out_amt = _dec(raw.get("inAmount")) or Decimal("0"), _dec(raw.get("outAmount")) or Decimal("0")
        direction = "IN" if in_amt > 0 else ("OUT" if out_amt > 0 else "")
        return {
            "source_key": str(raw["transactionId"]) if raw.get("transactionId") is not None else None,
            "institution": str(raw.get("bankName") or ""),
            "occurred_on": _date(raw.get("transactionAt")),
            "amount": in_amt if direction == "IN" else out_amt,
            "direction": direction,
            "counterparty": bank_counterparty(raw),
        }
    if kind == KIND_TAX:
        direction = str(raw.get("type") or "").upper()
        other = raw.get("contractorCompanyName") if direction == "SALES" else raw.get("supplierCompanyName")
        return {
            "source_key": str(raw["id"]) if raw.get("id") else None,
            "institution": "NTS",
            "occurred_on": _date(_first(raw, "issueDate", "reportingDate")),
            "amount": _dec(raw.get("totalAmount")),
            "direction": direction,
            "counterparty": str(other or ""),
        }
    if kind == KIND_CASH:
        return {
            "source_key": str(raw["id"]) if raw.get("id") else None,
            "institution": "NTS",
            "occurred_on": _date(_first(raw, "usedDateTime", "issueDate", "transactionDate")),
            "amount": _dec(_first(raw, "totalAmount", "amount")),
            "direction": str(raw.get("type") or "").upper(),
            "counterparty": str(raw.get("counterpartName") or ""),
        }
    if kind == KIND_CARD:
        return {
            "source_key": str(_first(raw, "id", "approvalId", "approvalNo")) if _first(raw, "id", "approvalId", "approvalNo") else None,
            "institution": str(_first(raw, "cardCompanyName", "cardName", "issuerName") or "card"),
            "occurred_on": _date(_first(raw, "usedAt", "approvedAt")),
            "amount": card_amount(raw),
            "direction": "OUT",
            "counterparty": str(_first(raw, "memberStoreName", "merchantName", "storeName") or ""),
        }
    if kind == KIND_CHANNEL:
        # raw 는 normalize_channel_payload 가 만든 정규화 레코드다. 채널 코드는 direction 열, 기관명은 institution 열에 둔다.
        return {
            "source_key": raw.get("source_key"),
            "institution": str(raw.get("institution") or ""),
            "occurred_on": _date(raw.get("date")),
            "amount": _dec(raw.get("gross")),
            "direction": str(raw.get("channel") or ""),
            "counterparty": "",
        }
    raise ValueError("unknown_kind")


def assign_source_keys(kind: str, rows: list[dict[str, Any]]) -> None:
    """안정 id 가 없는 항목은 fingerprint+ordinal 로 키를 만든다(같은 지문의 n 번째 항목).

    rows 는 normalize 결과에 'raw' 가 붙은 dict. 순서는 호출한 쪽 조회 순서에 의존한다.
    """
    seen: dict[str, int] = {}
    for row in rows:
        if row.get("source_key"):
            continue
        fp = item_hash(kind, row["raw"])[:32]
        seen[fp] = seen.get(fp, 0) + 1
        row["source_key"] = f"fp:{fp}:{seen[fp]}"


# ── 대사(reconciliation) ─────────────────────────────────

_SUM_FIELDS = {
    KIND_TAX: (("supplyValueSum", "supplyValue"), ("taxAmountSum", "taxAmount"), ("totalAmountSum", "totalAmount")),
    KIND_CASH: (("totalAmountSum", "totalAmount"), ("supplyAmountSum", "supplyAmount"), ("vatAmountSum", "vatAmount")),
    # 카드: 총계 하나에 대사 키 하나. 행 값은 row_sum_value/row_sum_sql 이 고른다
    # (used: usedAmountKrw → usedAmount → amount, 해외는 usedAmountKrw 만 / vat: vat → vatAmount).
    # 같은 총계를 키 두 개로 동시에 대사하면 해외 거래(usedAmount=외화)가 오탐 mismatch 가 된다.
    KIND_CARD: (("totalUsedAmountSum", "usedAmountKrw"), ("totalVatAmountSum", "vat")),
}


def reconcile(kind: str, collected: int, sums: dict[str, Decimal | None], first_page_meta: dict[str, Any]) -> dict[str, Any]:
    """저장된 고유 항목 vs 응답이 보고한 총계. 건수 불일치는 페이지 누락·중복의 신호다.

    collected 는 수신 행 수가 아니라 고유 source_key 수여야 한다(페이지가 밀려 중복+누락이
    상쇄돼도 잡히도록). sums 의 값이 None 이면 그 필드가 원본에 없어 대사하지 못한 것이다.
    합계는 실측상 기간 전체 값이다(2026-10-03: 페이지를 넘겨도 같은 값). 은행거래는 합계 필드가
    라벨 기준이라 원본 입출금과 일치하지 않아 건수만 대사한다.
    """
    reported = first_page_meta.get("totalElements")
    out: dict[str, Any] = {
        "reported_total": reported,
        "collected_total": collected,
        "count_ok": isinstance(reported, int) and reported == collected,
        "sum_checks": [],
    }
    mismatches = [] if out["count_ok"] else ["count_mismatch"]
    for meta_key, item_key in _SUM_FIELDS.get(kind, ()):
        got = sums.get(item_key)
        if meta_key not in first_page_meta or got is None:
            out["sum_checks"].append({"field": meta_key, "status": "skipped"})
            continue
        want = _dec(first_page_meta.get(meta_key)) or Decimal("0")
        ok = abs(want - got) <= SUM_TOLERANCE
        out["sum_checks"].append({"field": meta_key, "status": "ok" if ok else "mismatch"})
        if not ok:
            mismatches.append(f"sum_mismatch:{meta_key}")
    out["mismatches"] = mismatches
    out["ok"] = not mismatches
    return out


def is_cancellation(kind: str, raw: dict[str, Any]) -> bool:
    """현금영수증 취소거래. 클로브 합계 필드는 취소분을 뺀 순액이다(2026-10-03 실측: 승인 3,875,829 − 취소 55,000).

    카드는 여기서 다루지 않는다: 카드 행 값은 card_amount/card_vat 가 -abs 로 정규화하므로 -1 곱셈 규칙이 맞지 않는다.
    """
    if kind != KIND_CASH:
        return False
    text = str(raw.get("transactionType") or "")
    return "취소" in text or "cancel" in text.lower()


#: 위 규칙의 SQL 표현. 합계 대사(_stream_totals)가 같은 판정을 쓰도록 한 곳에 둔다.
CANCEL_SIGN_SQL = {
    KIND_CASH: "CASE WHEN payload->>'transactionType' LIKE '%취소%' OR lower(payload->>'transactionType') LIKE '%cancel%' "
               "THEN -1 ELSE 1 END",
}

_CARD_CANCELLED_SQL = "upper(btrim(COALESCE(NULLIF(payload->>'cancelType', ''), 'NONE'))) NOT IN ('', 'NONE')"
_CARD_FOREIGN_SQL = "upper(btrim(COALESCE(NULLIF(payload->>'homeForeignType', ''), 'DOMESTIC'))) NOT IN ('', 'DOMESTIC')"


def _sql_first(keys: tuple[str, ...]) -> str:
    return "COALESCE(" + ", ".join(f"NULLIF(payload->>'{k}', '')" for k in keys) + ")"


def row_sum_value(kind: str, item_key: str, raw: dict[str, Any]) -> Decimal | None:
    """대사 합계에 들어가는 행 값(부호 포함). None 이면 원본에 그 값이 없어 대사할 수 없다."""
    if kind == KIND_CARD and item_key == "usedAmountKrw":
        return card_amount(raw)
    if kind == KIND_CARD and item_key == "vat":
        return card_vat(raw)
    value = _dec(raw.get(item_key))
    if value is None:
        return None
    return -value if is_cancellation(kind, raw) else value


def row_sum_sql(kind: str, item_key: str) -> str:
    """row_sum_value 의 SQL 표현(payload 컬럼 기준 numeric 식). 두 곳의 규칙이 어긋나지 않게 이 모듈에 함께 둔다.

    Python 은 _dec 에서 행마다 소수 둘째 자리로 반올림한 뒤 합산하므로 SQL 도 합산 전에 행마다 round(.., 2) 한다.
    """
    if kind == KIND_CARD and item_key in ("usedAmountKrw", "vat"):
        if item_key == "usedAmountKrw":
            picked = (f"CASE WHEN {_CARD_FOREIGN_SQL} THEN NULLIF(payload->>'usedAmountKrw', '') "
                      f"ELSE {_sql_first(('usedAmountKrw', 'usedAmount', 'amount'))} END")
        else:
            picked = _sql_first(_CARD_VAT_KEYS)
        rounded = f"round(({picked})::numeric, 2)"
        return f"(CASE WHEN {_CARD_CANCELLED_SQL} THEN -abs({rounded}) ELSE {rounded} END)"
    return f"(round(NULLIF(payload->>'{item_key}', '')::numeric, 2) * {CANCEL_SIGN_SQL.get(kind, '1')})"


def sum_item_keys(kind: str) -> tuple[str, ...]:
    return tuple(item_key for _meta, item_key in _SUM_FIELDS.get(kind, ()))


#: 종류별 하위 스트림. 현금영수증은 type 기본값이 PURCHASE 라 매출을 따로 읽어야 빠지지 않는다.
KIND_STREAMS: dict[str, tuple[dict[str, str], ...]] = {
    KIND_BANK: ({},),
    KIND_TAX: ({},),
    KIND_CASH: ({"type": "PURCHASE"}, {"type": "SALES"}),
    KIND_CARD: ({},),
}


# ── 기간·시각 ────────────────────────────────────────────

_KST = timezone(timedelta(hours=9))


def parse_source_time(value: Any) -> datetime | None:
    """클로브 scrapedAt 은 시간대 없는 문자열이다. 한국 서비스라 KST 로 읽는다(가정)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_KST)


def scrape_summary(status_payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """get_scraping_status 응답에서 해당 종류의 최신성·오류 자산 수를 뽑는다."""
    category = SCRAPE_CATEGORY.get(kind)
    assets = [a for a in (status_payload.get("assets") or []) if category and isinstance(a, dict) and a.get("category") == category]
    times = [t for t in (parse_source_time(a.get("scrapedAt")) for a in assets) if t]
    errors = [a for a in assets if str(a.get("status") or "").upper() == "ERROR"]
    return {
        "asset_total": len(assets),
        "asset_error": len(errors),
        "source_as_of": min(times) if times else None,
        "failure_categories": sorted({str(a.get("failureCategory")) for a in errors if a.get("failureCategory")}),
    }


def resolve_period(start: Any, end: Any, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    e = _date(end) if end else today
    s = _date(start) if start else e - timedelta(days=DEFAULT_WINDOW_DAYS)
    if e is None or s is None or s > e:
        raise ValueError("invalid_period")
    if (e - s).days > MAX_WINDOW_DAYS:
        raise ValueError("period_too_long")
    return s, e


def retry_delay_seconds(consecutive_failures: int) -> int:
    return min(3600, 60 * (2 ** max(0, min(consecutive_failures, 6) - 1)))


# ── 판매채널 매출(get_monthly_revenue) ───────────────────
# 입력 스키마·응답 필드는 클로브 tools/list 가 정본이고 아직 운영에서 실측하지 못했다. 그래서 입력은 DB 에 저장된
# input_schema 를 읽어 맞추고(build_channel_calls), 응답은 필드명 별칭 표로 읽는다(parse_channel_payload).
# 알 수 없는 형태는 '미연결(빈 응답)'로 오인하지 않고 channel_unexpected_shape 로 올린다.

CHANNEL_CODES = ("DELIVERY", "CARD", "PG", "MKP")
_CHANNEL_ALIASES = {
    "DELIVERY": "DELIVERY", "DELIVERY_APP": "DELIVERY", "DELIVERYAPP": "DELIVERY", "배달": "DELIVERY", "배달앱": "DELIVERY",
    "CARD": "CARD", "CREDIT_CARD": "CARD", "카드": "CARD",
    "PG": "PG",
    "MKP": "MKP", "MARKETPLACE": "MKP", "MARKET_PLACE": "MKP", "OPEN_MARKET": "MKP", "OPENMARKET": "MKP",
    "오픈마켓": "MKP", "마켓플레이스": "MKP",
}
_GROSS_KEYS = ("grossAmount", "gross", "grossSales", "totalAmount", "salesAmount", "totalSales", "sales", "amount")
_NET_KEYS = ("netAmount", "net", "netSales", "settlementAmount", "settleAmount", "payoutAmount")
_FEE_KEYS = ("feeAmount", "fee", "totalFee", "feeTotal", "commission", "commissionAmount")
_COUNT_KEYS = ("count", "salesCount", "transactionCount", "orderCount", "txCount", "totalCount", "cnt")
_METRIC_KEYS = frozenset(_GROSS_KEYS + _NET_KEYS + _FEE_KEYS + _COUNT_KEYS)
_CHANNEL_KEYS = ("channel", "channelType", "channelCode", "channelCategory", "type", "category")
_INSTITUTION_KEYS = ("institutionName", "institution", "orgName", "organizationName", "agencyName", "platformName",
                     "issuerName", "cardCompanyName", "merchantName", "mallName", "channelName", "name")
_DATE_KEYS = ("date", "day", "salesDate", "settlementDate", "settleDate", "baseDate", "targetDate", "aggregateDate",
              "yearMonth", "month", "period")
_BASIS_KEYS = ("dateBasis", "date_basis")
_META_NUMERIC = frozenset({"page", "size", "number", "totalElements", "totalPages", "year", "month", "numberOfElements"})
_SUMMARY_KEYS = frozenset({"summary", "total", "totals", "overall", "aggregate", "grandTotal"})
_DATE_KEY_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def channel_code(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).upper()
    if not text:
        return "UNKNOWN"
    return _CHANNEL_ALIASES.get(text) or _CHANNEL_ALIASES.get(str(value).strip()) or text


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    return _dec(str(value).replace(",", "")) if value not in (None, "") else None


def _parse_channel_date(value: Any) -> tuple[date, str] | None:
    """(날짜, 'DAY'|'MONTH'). 'YYYY-MM' 이나 'YYYYMM' 은 월 단위로 그 달 1일을 쓴다."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) or re.fullmatch(r"\d{8}", text):
        d = _date(text) if "-" in text else _date(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
        return (d, "DAY") if d else None
    m = re.fullmatch(r"(\d{4})-?(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1), "MONTH"
        except ValueError:
            return None
    return None


def _first_present(ctx: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if ctx.get(key) not in (None, ""):
            return ctx[key]
    return None


def _walk_channel(node: Any, ctx: dict[str, Any], out: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_channel(item, ctx, out, stats)
        return
    if not isinstance(node, dict):
        return
    scalars = {k: v for k, v in node.items() if not isinstance(v, (dict, list)) and v not in (None, "")}
    here = {**ctx, **scalars}
    children = [(k, v) for k, v in node.items() if isinstance(v, (dict, list)) and k not in _SUMMARY_KEYS]
    if not children:
        has_metric = any(k in node for k in _METRIC_KEYS)
        if has_metric:
            out.append(here)
        elif any(isinstance(v, (int, float)) and not isinstance(v, bool) and k not in _META_NUMERIC
                 for k, v in node.items()):
            stats["unknown_numeric_leaf"] += 1
        return
    child_out: list[dict[str, Any]] = []
    for key, value in children:
        sub = dict(here)
        if _DATE_KEY_RE.match(str(key)):
            sub["date"] = key
        elif channel_code(key) in CHANNEL_CODES and not _first_present(here, _CHANNEL_KEYS):
            sub["channel"] = key
        _walk_channel(value, sub, child_out, stats)
    if child_out:
        out.extend(child_out)
    elif any(k in node for k in _METRIC_KEYS):
        out.append(here)


def parse_channel_payload(
    payload: Any, *, company_id: str, date_basis: str | None = None, default_month: date | None = None,
) -> dict[str, Any]:
    """get_monthly_revenue 응답 → 정규화 레코드. 반환: records(비어 있을 수 있음), connected(False 면 명시적 미연결), skipped.

    레코드가 하나도 없고 숫자 값을 가진 알 수 없는 잎 객체가 있거나, 지표는 있는데 날짜를 못 읽으면 ValueError
    ('channel_unexpected_shape') — 형식 변경을 '미연결'로 덮지 않는다.
    """
    connected: bool | None = None
    if isinstance(payload, dict):
        for key in ("connected", "isConnected", "hasChannels"):
            if isinstance(payload.get(key), bool):
                connected = payload[key]
    found: list[dict[str, Any]] = []
    stats = {"unknown_numeric_leaf": 0}
    _walk_channel(payload, {}, found, stats)
    records: list[dict[str, Any]] = []
    skipped = 0
    for ctx in found:
        raw_date = _first_present(ctx, _DATE_KEYS)
        parsed = _parse_channel_date(raw_date) if raw_date is not None else None
        if parsed is None and default_month is not None and (raw_date is None or not _DATE_KEY_RE.match(str(raw_date))):
            parsed = (default_month, "MONTH")
        if parsed is None:
            skipped += 1
            continue
        day, grain = parsed
        institution = re.sub(r"\s+", " ", str(_first_present(ctx, _INSTITUTION_KEYS) or "")).strip()
        channel = channel_code(_first_present(ctx, _CHANNEL_KEYS))
        basis = str(_first_present(ctx, _BASIS_KEYS) or date_basis or "DEFAULT").strip().upper()
        gross, net = _num(_first_present(ctx, _GROSS_KEYS)), _num(_first_present(ctx, _NET_KEYS))
        fee, count = _num(_first_present(ctx, _FEE_KEYS)), _num(_first_present(ctx, _COUNT_KEYS))
        date_text = day.isoformat() if grain == "DAY" else day.strftime("%Y-%m")
        records.append({
            "source_key": f"ch|{company_id}|{channel}|{institution}|{date_text}|{basis}",
            "channel": channel, "institution": institution, "date": day.isoformat(), "grain": grain,
            "date_basis": basis, "gross": gross, "net": net, "fee": fee,
            "count": int(count) if count is not None else None,
            "source": {k: v for k, v in ctx.items() if not isinstance(v, (dict, list))},
        })
    if not records and (skipped or stats["unknown_numeric_leaf"]):
        raise ValueError("channel_unexpected_shape")
    return {"records": merge_channel_records(records), "connected": connected, "skipped": skipped}


def merge_channel_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 source_key(회사·채널·기관·날짜·기준)는 한 행으로 합산한다 — 하위 차원(매장·카드사)이 응답에 섞여 있어도 키가 유일하다."""
    merged: dict[str, dict[str, Any]] = {}
    for rec in records:
        cur = merged.get(rec["source_key"])
        if cur is None:
            merged[rec["source_key"]] = {**rec, "source": dict(rec["source"])}
            continue
        for field in ("gross", "net", "fee", "count"):
            a, b = cur.get(field), rec.get(field)
            cur[field] = b if a is None else (a if b is None else a + b)
    return list(merged.values())


_RANGE_PAIRS = (("startDate", "endDate"), ("fromDate", "toDate"), ("from", "to"), ("start", "end"),
                ("startMonth", "endMonth"), ("fromMonth", "toMonth"), ("startYearMonth", "endYearMonth"))
_MONTH_KEYS = ("yearMonth", "targetMonth", "baseMonth", "ym", "month", "period")
_GRANULARITY_KEYS = ("granularity", "unit", "groupBy", "interval", "aggregation", "aggregateBy", "periodType",
                     "timeUnit", "resolution")


def schema_input_props(schema: Any) -> dict[str, Any]:
    """저장된 input_schema 의 속성 맵. 도구 인자가 {"input": {...}} 로 감싸져 있으면 안쪽을 본다."""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    inner = props.get("input")
    if len(props) == 1 and isinstance(inner, dict) and isinstance(inner.get("properties"), dict):
        return inner["properties"]
    return props


def _months_between(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        out.append(cur)
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return out


def pick_date_basis(props: dict[str, Any], preferred: str | None = None) -> tuple[str | None, str | None]:
    """(인자 이름, 값). enum 이 있으면 환경변수 선호 → SETTL* → 스키마 기본값 순. 값을 알 수 없으면 서버 기본값에 맡긴다."""
    key = next((k for k in _BASIS_KEYS if k in props), None)
    if key is None:
        return None, None
    prop = props[key] if isinstance(props[key], dict) else {}
    enum = [str(v) for v in prop.get("enum") or []]
    pref = (preferred or os.getenv("OBYS_CLOBE_CHANNEL_DATE_BASIS") or "").strip().upper()
    if enum:
        by_upper = {v.upper(): v for v in enum}
        if pref in by_upper:
            return key, by_upper[pref]
        settle = next((v for v in enum if v.upper().startswith("SETTL")), None)
        if settle:
            return key, settle
    if pref:
        return key, pref
    if prop.get("default") is not None:
        return key, str(prop["default"])
    return None, None


def build_channel_calls(
    schema: Any, company_id: str, start: date, end: date, *, preferred_basis: str | None = None,
) -> list[dict[str, Any]]:
    """저장된 input_schema 에 맞춘 호출 목록. 각 항목: args, month(월 단위 호출이면 그 달 1일), basis, page_key.

    범위 인자(startDate/endDate 등)가 있으면 한 번, 월 인자만 있으면 기간이 걸친 달마다 한 번 부른다.
    스키마에 없는 인자는 보내지 않는다. 일 단위 집계를 고를 수 있으면(DAY/DAILY enum) 일 단위로 부른다.
    """
    props = schema_input_props(schema)
    base: dict[str, Any] = {next((k for k in ("companyId", "company_id") if k in props), "companyId"): company_id}
    basis_key, basis_val = pick_date_basis(props, preferred_basis)
    if basis_key and basis_val is not None:
        base[basis_key] = basis_val
    for key in _GRANULARITY_KEYS:
        prop = props.get(key)
        if isinstance(prop, dict) and prop.get("enum"):
            day = next((str(v) for v in prop["enum"] if str(v).upper() in ("DAY", "DAILY", "DATE")), None)
            if day:
                base[key] = day
                break
    page_key = "page" if "page" in props else None
    common = {"basis": basis_val, "page_key": page_key}
    pair = next(((a, b) for a, b in _RANGE_PAIRS if a in props and b in props), None)
    if pair is not None:
        monthly = "month" in pair[0].lower()
        fmt = (lambda d: d.strftime("%Y-%m")) if monthly else (lambda d: d.isoformat())
        return [{"args": {**base, pair[0]: fmt(start), pair[1]: fmt(end)}, "month": None, **common}]
    months = _months_between(start, end)
    if "year" in props and "month" in props:
        return [{"args": {**base, "year": m.year, "month": m.month}, "month": m, **common} for m in months]
    month_key = next((k for k in _MONTH_KEYS if k in props), None)
    if month_key:
        return [{"args": {**base, month_key: m.strftime("%Y-%m")}, "month": m, **common} for m in months]
    return [{"args": {**base, "startDate": start.isoformat(), "endDate": end.isoformat()}, "month": None, **common}]
