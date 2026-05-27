#!/usr/bin/env python3
"""
에이블리/지그재그 카테고리 베스트 상품 스크래핑 (PC Agent 활용)
Usage: python3 scrape_platform_ranking.py [--platform ably|zigzag|all]
"""
import asyncio
import argparse
import json
import hashlib
import logging
import httpx
import sys
import os

# AADS 프로젝트 경로 추가
sys.path.insert(0, '/root/aads/aads-server')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

NTV2_IMPORT_URL = "https://newtalk.kr/api/external/auto-sourcing/trends/import"
NTV2_IMPORT_KEY = os.environ.get("AUTO_SOURCING_IMPORT_TOKEN", "")

# 에이블리 카테고리별 랭킹 URL
ABLY_CATEGORIES = {
    "상의": "https://m.a-bly.com/ranking?category=top",
    "하의": "https://m.a-bly.com/ranking?category=bottom",
    "원피스": "https://m.a-bly.com/ranking?category=dress",
    "아우터": "https://m.a-bly.com/ranking?category=outer",
    "신발": "https://m.a-bly.com/ranking?category=shoes",
    "가방": "https://m.a-bly.com/ranking?category=bag",
}

# 지그재그 카테고리별 랭킹 URL
ZIGZAG_CATEGORIES = {
    "상의": "https://zigzag.kr/categories/474?title=%EC%83%81%EC%9D%98&sort=popular",
    "하의": "https://zigzag.kr/categories/547?title=%ED%95%98%EC%9D%98&sort=popular",
    "원피스": "https://zigzag.kr/categories/546?title=%EC%9B%90%ED%94%BC%EC%8A%A4&sort=popular",
    "아우터": "https://zigzag.kr/categories/475?title=%EC%95%84%EC%9A%B0%ED%84%B0&sort=popular",
    "가방": "https://zigzag.kr/categories/548?title=%EA%B0%80%EB%B0%A9&sort=popular",
}

# JS extraction scripts
ABLY_EXTRACT_JS = """
(() => {
    const products = [];
    // 에이블리 랭킹 페이지에서 상품 추출
    const items = document.querySelectorAll('[class*="ranking"] [class*="item"], [class*="product-card"], [class*="goods-item"]');
    if (items.length === 0) {
        // 대체 셀렉터
        const allLinks = document.querySelectorAll('a[href*="/goods/"]');
        allLinks.forEach((el, idx) => {
            if (idx >= 50) return;
            const img = el.querySelector('img');
            const priceEl = el.closest('[class*="item"]')?.querySelector('[class*="price"], [class*="won"]');
            products.push({
                name: img?.alt || el.textContent?.trim()?.substring(0, 100) || '',
                price: priceEl ? parseInt(priceEl.textContent.replace(/[^0-9]/g, '')) : null,
                rank: idx + 1,
                image_url: img?.src || '',
                product_url: el.href || '',
                brand: ''
            });
        });
    } else {
        items.forEach((el, idx) => {
            if (idx >= 50) return;
            const nameEl = el.querySelector('[class*="name"], [class*="title"]');
            const priceEl = el.querySelector('[class*="price"], [class*="won"]');
            const imgEl = el.querySelector('img');
            const linkEl = el.querySelector('a[href*="/goods/"]') || el.closest('a');
            products.push({
                name: nameEl?.textContent?.trim() || imgEl?.alt || '',
                price: priceEl ? parseInt(priceEl.textContent.replace(/[^0-9]/g, '')) : null,
                rank: idx + 1,
                image_url: imgEl?.src || '',
                product_url: linkEl?.href || '',
                brand: ''
            });
        });
    }
    return JSON.stringify(products);
})()
"""

ZIGZAG_EXTRACT_JS = """
(() => {
    const products = [];
    const items = document.querySelectorAll('[class*="product"], [class*="goods"], [class*="ranking-item"]');
    if (items.length === 0) {
        const allLinks = document.querySelectorAll('a[href*="/catalog/products/"]');
        allLinks.forEach((el, idx) => {
            if (idx >= 50) return;
            const img = el.querySelector('img');
            const priceEl = el.closest('[class*="item"]')?.querySelector('[class*="price"]');
            products.push({
                name: img?.alt || el.textContent?.trim()?.substring(0, 100) || '',
                price: priceEl ? parseInt(priceEl.textContent.replace(/[^0-9]/g, '')) : null,
                rank: idx + 1,
                image_url: img?.src || '',
                product_url: el.href || '',
                brand: ''
            });
        });
    } else {
        items.forEach((el, idx) => {
            if (idx >= 50) return;
            const nameEl = el.querySelector('[class*="name"], [class*="title"]');
            const priceEl = el.querySelector('[class*="price"]');
            const imgEl = el.querySelector('img');
            const linkEl = el.querySelector('a') || el.closest('a');
            products.push({
                name: nameEl?.textContent?.trim() || imgEl?.alt || '',
                price: priceEl ? parseInt(priceEl.textContent.replace(/[^0-9]/g, '')) : null,
                rank: idx + 1,
                image_url: imgEl?.src || '',
                product_url: linkEl?.href || '',
                brand: ''
            });
        });
    }
    return JSON.stringify(products);
})()
"""


_AGENT_ID_CACHE: str | None = None


def _resolve_agent_id() -> str | None:
    """현재 연결된 PC Agent 중 chrome_cdp/interactive_browser 보유 에이전트 자동 선택."""
    global _AGENT_ID_CACHE
    if _AGENT_ID_CACHE:
        return _AGENT_ID_CACHE
    try:
        from app.services.pc_agent_manager import pc_agent_manager
        agents = pc_agent_manager.list_agents()
        if not agents:
            return None
        for info in agents:
            caps = set(getattr(info, "capabilities", []) or [])
            if {"chrome_cdp", "interactive_browser"} & caps:
                _AGENT_ID_CACHE = info.agent_id
                return info.agent_id
        _AGENT_ID_CACHE = agents[0].agent_id
        return _AGENT_ID_CACHE
    except Exception as e:
        logger.error(f"PC Agent 탐색 실패: {e}")
        return None


async def send_pc_agent_command(command: dict, timeout: int = 30) -> dict | None:
    """PC Agent에 명령을 전송하고 결과를 반환.

    command = {"type": <command_type>, ...params}
    내부적으로 send_command(agent_id, command_type, params) -> command_id 호출 후
    get_result(command_id, timeout)으로 결과 대기.
    """
    agent_id = _resolve_agent_id()
    if not agent_id:
        logger.error("PC Agent 미연결 (list_agents=빈배열). client 재실행 필요.")
        return None
    try:
        from app.services.pc_agent_manager import pc_agent_manager
        cmd = dict(command)
        command_type = cmd.pop("type", None)
        if not command_type:
            logger.error(f"command type 누락: {command}")
            return None
        command_id = await pc_agent_manager.send_command(agent_id, command_type, cmd)
        result = await pc_agent_manager.get_result(command_id, timeout=float(timeout))
        return {
            "status": getattr(result, "status", None),
            "data": getattr(result, "result", None) or getattr(result, "data", None),
            "error": getattr(result, "error", None),
        }
    except Exception as e:
        logger.error(f"PC Agent 명령 실패: {e}")
        return None


async def scrape_platform(platform: str, categories: dict, extract_js: str) -> dict:
    """플랫폼의 카테고리별 랭킹을 스크래핑."""
    result = {"source": platform, "platform": "pc_agent", "categories": []}

    for cat_name, url in categories.items():
        logger.info(f"[{platform}] {cat_name} 스크래핑 시작: {url}")

        # 1. 페이지 이동
        nav_result = await send_pc_agent_command({
            "type": "browser_navigate",
            "url": url
        }, timeout=30)

        if not nav_result:
            logger.warning(f"[{platform}] {cat_name}: 페이지 이동 실패")
            continue

        # 2. 페이지 로딩 대기 (3초)
        await asyncio.sleep(3)

        # 3. 스크롤 다운 (더 많은 상품 로딩)
        await send_pc_agent_command({
            "type": "browser_eval",
            "script": "window.scrollTo(0, document.body.scrollHeight / 2); void(0);"
        }, timeout=10)
        await asyncio.sleep(2)

        await send_pc_agent_command({
            "type": "browser_eval",
            "script": "window.scrollTo(0, document.body.scrollHeight); void(0);"
        }, timeout=10)
        await asyncio.sleep(2)

        # 4. 상품 데이터 추출
        eval_result = await send_pc_agent_command({
            "type": "browser_eval",
            "script": extract_js
        }, timeout=15)

        if not eval_result:
            logger.warning(f"[{platform}] {cat_name}: JS 실행 실패")
            continue

        try:
            data = eval_result.get("data", {})
            raw = data.get("result", "[]") if isinstance(data, dict) else str(data)
            products = json.loads(raw) if isinstance(raw, str) else raw

            if not products:
                logger.warning(f"[{platform}] {cat_name}: 추출된 상품 0건")
                continue

            # 유효한 상품만 필터링
            valid_products = [
                p for p in products
                if p.get("name") and len(p["name"]) > 2
            ]

            result["categories"].append({
                "name": cat_name,
                "products": valid_products[:50]
            })

            logger.info(f"[{platform}] {cat_name}: {len(valid_products)}건 추출")

        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[{platform}] {cat_name}: 파싱 실패 - {e}")
            continue

    return result


async def send_to_ntv2(data: dict) -> bool:
    """스크래핑 결과를 NTV2 외부 Import API로 전송."""
    try:
        headers = {"Content-Type": "application/json"}
        if NTV2_IMPORT_KEY:
            headers["X-Import-Key"] = NTV2_IMPORT_KEY
        data["scraper"] = data.pop("platform", "pc_agent")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(NTV2_IMPORT_URL, json=data, headers=headers)
            if response.status_code == 200:
                result = response.json()
                logger.info(f"NTV2 전송 성공: {result}")
                return True
            else:
                logger.error(f"NTV2 전송 실패: HTTP {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"NTV2 전송 예외: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser(description="플랫폼 랭킹 스크래핑")
    parser.add_argument("--platform", choices=["ably", "zigzag", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="NTV2 전송 없이 결과만 출력")
    args = parser.parse_args()

    platforms = []
    if args.platform in ("ably", "all"):
        platforms.append(("ably", ABLY_CATEGORIES, ABLY_EXTRACT_JS))
    if args.platform in ("zigzag", "all"):
        platforms.append(("zigzag", ZIGZAG_CATEGORIES, ZIGZAG_EXTRACT_JS))

    for platform, categories, extract_js in platforms:
        logger.info(f"=== {platform} 스크래핑 시작 ===")
        result = await scrape_platform(platform, categories, extract_js)

        total_products = sum(len(c["products"]) for c in result["categories"])
        logger.info(f"[{platform}] 총 {len(result['categories'])}개 카테고리, {total_products}건 상품")

        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif total_products > 0:
            success = await send_to_ntv2(result)
            if not success:
                # NTV2 전송 실패 시 파일로 저장
                backup_path = f"/root/aads/aads-server/data/scrape_{platform}_{int(asyncio.get_event_loop().time())}.json"
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                with open(backup_path, "w") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                logger.info(f"백업 저장: {backup_path}")


if __name__ == "__main__":
    asyncio.run(main())
