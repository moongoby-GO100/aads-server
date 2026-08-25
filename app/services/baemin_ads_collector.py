"""Baemin ads collector for FOOD full backfill."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any


BAEMIN_ADS_URL = "https://self.baemin.com/advertisements"
SCHEMA_VERSION = "baemin_ads.v1"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(_clean(part) for part in parts).encode("utf-8")).hexdigest()


def _money(value: Any) -> int:
    match = re.search(r"-?\s*[\d,]+", str(value or ""))
    if not match:
        return 0
    return int(match.group(0).replace(" ", "").replace(",", ""))


def _number_after(text: str, labels: tuple[str, ...]) -> int:
    for label in labels:
        match = re.search(re.escape(label) + r"\s*[:：]?\s*([\d,]+)", text)
        if match:
            return int(match.group(1).replace(",", ""))
    return 0


def _money_after(text: str, labels: tuple[str, ...]) -> int:
    for label in labels:
        match = re.search(re.escape(label) + r"\s*[:：]?\s*(-?\s*[\d,]+)\s*원?", text)
        if match:
            return _money(match.group(1))
    return 0


def _rate_after(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        match = re.search(re.escape(label) + r"\s*[:：]?\s*([\d.]+)\s*%", text)
        if match:
            return float(match.group(1))
    return None


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I | re.S)
    return _clean(match.group(1)) if match else ""


def _segments(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n|(?=캠페인\s*[:：]?)", str(text or ""))
    return [part for part in parts if "캠페인" in part or "광고" in part or "ROAS" in part.upper()]


def parse_baemin_ads_text(
    source_text: str,
    business_id: str,
    branch: str,
    *,
    collected_at: str | None = None,
) -> dict[str, Any]:
    collected_at = collected_at or datetime.now().astimezone().isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    for index, segment in enumerate(_segments(source_text), start=1):
        campaign = _first(r"캠페인\s*[:：]?\s*([^\n]+)", segment) or _first(r"광고명\s*[:：]?\s*([^\n]+)", segment)
        if not campaign:
            campaign = f"baemin-ad-{index}"
        spend = _money_after(segment, ("소진금액", "광고비", "비용", "spend"))
        impressions = _number_after(segment, ("노출수", "impressions"))
        clicks = _number_after(segment, ("클릭수", "clicks"))
        orders = _number_after(segment, ("주문수", "orders"))
        order_amount = _money_after(segment, ("주문금액", "매출", "order_amount"))
        ctr = _rate_after(segment, ("CTR", "클릭률"))
        conversion_rate = _rate_after(segment, ("전환율", "conversion_rate"))
        roas = _rate_after(segment, ("ROAS", "roas"))
        record = {
            "id": _stable_id(business_id, branch, "baemin", "ads", campaign, _first(r"기간\s*[:：]?\s*([^\n]+)", segment)),
            "source_id": campaign,
            "business_id": business_id,
            "branch": branch,
            "service": "baemin",
            "platform": "baemin",
            "record_type": "ads",
            "occurred_on": _first(r"(20\d{2}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})", segment).replace(".", "-").replace("/", "-"),
            "collected_at": collected_at,
            "campaign_name": campaign,
            "campaign": campaign,
            "status": _first(r"상태\s*[:：]?\s*([^\n]+)", segment),
            "period": _first(r"기간\s*[:：]?\s*([^\n]+)", segment),
            "budget": _money_after(segment, ("예산", "budget")),
            "spend": spend,
            "cost_amount": spend,
            "impressions": impressions,
            "clicks": clicks,
            "orders": orders,
            "ctr": ctr,
            "conversion_rate": conversion_rate,
            "order_amount": order_amount,
            "roas": roas,
            "source_url": BAEMIN_ADS_URL,
            "source_collected_at": collected_at,
            "schema_version": SCHEMA_VERSION,
        }
        records.append(record)
    return {
        "records": {"sales": [], "settlements": [], "reviews": [], "ads": records},
        "diagnostics": {
            "source": "baemin_ads_text",
            "schema_version": SCHEMA_VERSION,
            "ads_seen": len(records),
            "ads_saved": len(records),
        },
    }


async def _body_text(page: Any) -> str:
    try:
        return str(await page.evaluate("document.body ? document.body.innerText : ''") or "")
    except Exception:
        return ""


async def collect_ads(page: Any, *, business_id: str, branch: str) -> dict[str, Any]:
    try:
        await page.goto(BAEMIN_ADS_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except Exception:
        pass
    parsed = parse_baemin_ads_text(await _body_text(page), business_id, branch)
    records = parsed.get("records") or {}
    ads = records.get("ads") or []
    return {
        "status": "succeeded" if ads else "partial",
        "error_code": "" if ads else "BAEMIN_ADS_NO_ROWS",
        "records": records,
        "diagnostics": parsed.get("diagnostics") or {},
        "message": "" if ads else "배민 광고 페이지에서 광고 데이터를 찾지 못했습니다.",
    }
