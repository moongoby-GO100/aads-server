"""Baemin review collector for FOOD full backfill."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any


BAEMIN_REVIEW_URL = "https://self.baemin.com/reviews"
SCHEMA_VERSION = "baemin_review.v1"
ORDER_NO_RE = re.compile(r"\b[A-Z][0-9A-Z]{8,}\b")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(_clean(part) for part in parts).encode("utf-8")).hexdigest()


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I | re.S)
    return _clean(match.group(1)) if match else ""


def _rating(text: str) -> int | None:
    match = re.search(r"(?:별점|평점|rating)\s*[:：]?\s*([1-5])", text, re.I)
    if not match:
        match = re.search(r"([1-5])\s*점", text)
    return int(match.group(1)) if match else None


def _reviewed_at(text: str) -> str:
    match = re.search(r"(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _segments(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n|(?=리뷰\s*(?:ID|번호)[:：]?)", str(text or ""))
    return [part for part in parts if "리뷰" in part or "별점" in part or ORDER_NO_RE.search(part)]


def parse_baemin_review_text(
    source_text: str,
    business_id: str,
    branch: str,
    *,
    collected_at: str | None = None,
) -> dict[str, Any]:
    collected_at = collected_at or datetime.now().astimezone().isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    for index, segment in enumerate(_segments(source_text), start=1):
        order_no = (ORDER_NO_RE.search(segment or "") or [None])[0] if ORDER_NO_RE.search(segment or "") else ""
        review_id = _first(r"리뷰\s*(?:ID|번호)\s*[:：]?\s*([^\n]+)", segment) or order_no or f"review-{index}"
        review_text = (
            _first(r"리뷰\s*내용\s*[:：]?\s*(.+?)(?:\n\s*(?:사장님|답글|메뉴|주문|별점|평점)|$)", segment)
            or _first(r"고객\s*리뷰\s*[:：]?\s*(.+?)(?:\n\s*(?:사장님|답글|메뉴|주문|별점|평점)|$)", segment)
        )
        if not review_text and "리뷰" not in segment and not order_no:
            continue
        owner_reply = _first(r"(?:사장님\s*)?답글\s*[:：]?\s*(.+?)(?:\n\s*(?:사진|이미지|메뉴|주문|별점|평점)|$)", segment)
        rating = _rating(segment)
        reviewed_at = _reviewed_at(segment)
        image_count_match = re.search(r"(?:사진|이미지)\s*([0-9]+)\s*(?:장|개)", segment)
        image_count = int(image_count_match.group(1)) if image_count_match else 0
        record = {
            "id": _stable_id(business_id, branch, "baemin", "reviews", review_id),
            "source_id": review_id,
            "business_id": business_id,
            "branch": branch,
            "service": "baemin",
            "platform": "baemin",
            "record_type": "reviews",
            "occurred_on": reviewed_at,
            "collected_at": collected_at,
            "review_id": review_id,
            "order_no": order_no,
            "rating": rating,
            "review_text": review_text[:4000],
            "reviewed_at": reviewed_at,
            "menu_summary": _first(r"메뉴\s*[:：]?\s*([^\n]+)", segment),
            "owner_reply_text": owner_reply[:4000],
            "reply_status": "답변완료" if owner_reply else "미답변",
            "image_count": image_count,
            "match_confidence": 0.95 if order_no and reviewed_at else 0.7,
            "source_url": BAEMIN_REVIEW_URL,
            "source_collected_at": collected_at,
            "schema_version": SCHEMA_VERSION,
        }
        records.append(record)
    return {
        "records": {"sales": [], "settlements": [], "reviews": records, "ads": []},
        "diagnostics": {
            "source": "baemin_review_text",
            "schema_version": SCHEMA_VERSION,
            "reviews_seen": len(records),
            "reviews_saved": len(records),
        },
    }


async def _body_text(page: Any) -> str:
    try:
        return str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
    except Exception:
        return ""


async def collect_reviews(
    page: Any,
    *,
    business_id: str,
    branch: str,
    max_records: int = 300,
) -> dict[str, Any]:
    try:
        await page.goto(BAEMIN_REVIEW_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except Exception:
        pass
    parsed = parse_baemin_review_text(await _body_text(page), business_id, branch)
    records = parsed.get("records") or {}
    reviews = list((records.get("reviews") or [])[: max(1, min(300, int(max_records or 300)))])
    records["reviews"] = reviews
    return {
        "status": "succeeded" if reviews else "partial",
        "error_code": "" if reviews else "BAEMIN_REVIEW_NO_ROWS",
        "records": records,
        "diagnostics": parsed.get("diagnostics") or {},
        "message": "" if reviews else "배민 리뷰 페이지에서 리뷰 데이터를 찾지 못했습니다.",
    }
