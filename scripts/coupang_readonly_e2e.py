#!/usr/bin/env python3
"""쿠팡 검색 결과를 읽기 전용으로 훑어 상위 상품을 추려낸다.

M11 이 요구하는 "읽기전용 쇼핑 검색" 을 **실제 사이트**로 수행한다.
기존 `smart_browser_readonly_e2e.py` 는 `page.set_content()` 로 만든 가짜
상점 HTML 을 썼다 — 그래서 동작 로직은 검증됐지만 실제 쇼핑몰의 동적 렌더링·
봇 차단·무한 스크롤은 하나도 거치지 않았다(2026-09-21 확인).

읽기만 한다. 로그인·장바구니·주문·쓰기 동작은 하지 않는다.

    docker exec aads-server python3 scripts/coupang_readonly_e2e.py \
        --query 무선청소기 --limit 5 --output-dir /tmp/coupang-e2e
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

SEARCH_URL = "https://www.coupang.com/np/search?q={q}&channel=user"

# 쿠팡은 목록 마크업을 자주 바꾼다. 하나에 걸지 않고 순서대로 시도한다.
ITEM_SELECTORS = [
    "li.search-product",
    "li[class*='search-product']",
    "ul#productList > li",
    "[data-product-id]",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def _won(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


async def collect(query: str, limit: int, outdir: Path) -> dict:
    from playwright.async_api import async_playwright

    outdir.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    items: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            locale="ko-KR",
            viewport={"width": 1440, "height": 1000},
        )
        page = await context.new_page()
        try:
            url = SEARCH_URL.format(q=quote(query))
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            steps.append({"seq": 1, "action": "navigate", "url": page.url, "title": await page.title()})

            used = ""
            for selector in ITEM_SELECTORS:
                try:
                    await page.wait_for_selector(selector, timeout=12_000)
                except Exception:
                    continue
                if await page.locator(selector).count() > 0:
                    used = selector
                    break
            counts = {s: await page.locator(s).count() for s in ITEM_SELECTORS}
            steps.append({"seq": 2, "action": "locate_list", "selector": used, "counts": counts})

            shot1 = outdir / "coupang_01_results.png"
            await page.screenshot(path=str(shot1), timeout=30_000)
            steps.append({"seq": 3, "action": "capture", "path": str(shot1)})

            if not used:
                steps.append({"seq": 4, "action": "abort", "reason": "no_product_list_selector_matched"})
                return {"status": "failed", "query": query, "steps": steps, "items": []}

            # 지연 로딩 목록을 끝까지 펼친다.
            for _ in range(3):
                await page.mouse.wheel(0, 2400)
                await page.wait_for_timeout(1200)
            shot2 = outdir / "coupang_02_scrolled.png"
            await page.screenshot(path=str(shot2), timeout=30_000)
            steps.append({"seq": 5, "action": "scroll_capture", "path": str(shot2)})

            rows = page.locator(used)
            total = await rows.count()
            for i in range(min(total, max(limit * 6, 30))):
                row = rows.nth(i)
                try:
                    text = (await row.inner_text(timeout=4_000)).strip()
                except Exception:
                    continue
                if not text:
                    continue
                name = text.split("\n")[0][:120]
                price = None
                for line in text.split("\n"):
                    if "원" in line and _won(line):
                        price = _won(line)
                        break
                rating = None
                m = re.search(r"([0-5]\.[0-9])", text)
                if m:
                    rating = float(m.group(1))
                reviews = None
                m = re.search(r"\(\s*([0-9,]+)\s*\)", text)
                if m:
                    reviews = _won(m.group(1))
                href = ""
                try:
                    href = await row.locator("a").first.get_attribute("href", timeout=2_000) or ""
                except Exception:
                    pass
                if href.startswith("/"):
                    href = "https://www.coupang.com" + href
                items.append({
                    "rank_on_page": i + 1,
                    "name": name,
                    "price_krw": price,
                    "rating": rating,
                    "review_count": reviews,
                    "url": href.split("?")[0],
                    "raw_preview": text[:200],
                })
            steps.append({"seq": 6, "action": "extract", "rows_seen": total, "rows_parsed": len(items)})
        finally:
            await context.close()
            await browser.close()

    return {"status": "passed" if items else "failed", "query": query, "steps": steps, "items": items}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--output-dir", default="/tmp/coupang-e2e")
    args = ap.parse_args()
    result = asyncio.run(collect(args.query, args.limit, Path(args.output_dir)))
    Path(args.output_dir, "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False))
    print(f"items={len(result['items'])}")


if __name__ == "__main__":
    main()
