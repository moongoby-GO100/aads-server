"""클로브 수집 계약의 순수 로직(DB·네트워크 없음): 정규화·해시·대사·회사 연결 판정.

원장 확정과 무관하다 — 여기서 나온 값은 모두 검토함 등록 전의 후보다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# 수집 종류 → 클로브 조회 도구. 도구 이름의 정본은 clobe_mcp_client.COLLECTION_DATA_TOOLS.
KIND_BANK = "bank_transaction"
KIND_TAX = "tax_invoice"
KIND_CASH = "cash_receipt"
KIND_CARD = "card_approval"
DATA_KINDS = (KIND_BANK, KIND_TAX, KIND_CASH, KIND_CARD)
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
        return Decimal(str(value)).quantize(Decimal("0.01"))
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
            "counterparty": str(raw.get("transactionName") or ""),
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
            "amount": _dec(_first(raw, "usedAmount", "amount")),
            "direction": "OUT",
            "counterparty": str(_first(raw, "merchantName", "storeName") or ""),
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
    KIND_CARD: (("totalUsedAmountSum", "usedAmount"), ("totalVatAmountSum", "vatAmount")),
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
    """현금영수증 취소거래. 클로브 합계 필드는 취소분을 뺀 순액이다(2026-10-03 실측: 승인 3,875,829 − 취소 55,000)."""
    if kind != KIND_CASH:
        return False
    text = str(raw.get("transactionType") or "")
    return "취소" in text or "cancel" in text.lower()


#: 위 규칙의 SQL 표현. 합계 대사(_stream_totals)가 같은 판정을 쓰도록 한 곳에 둔다.
CANCEL_SIGN_SQL = {
    KIND_CASH: "CASE WHEN payload->>'transactionType' LIKE '%취소%' OR lower(payload->>'transactionType') LIKE '%cancel%' "
               "THEN -1 ELSE 1 END",
}


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
    category = SCRAPE_CATEGORY[kind]
    assets = [a for a in (status_payload.get("assets") or []) if isinstance(a, dict) and a.get("category") == category]
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
