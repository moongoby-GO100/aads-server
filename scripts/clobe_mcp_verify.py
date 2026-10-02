#!/usr/bin/env python3
"""CEO 동의 완료 후 실행: tools/list → 회사 목록류 조회 1회(읽기만).

토큰·응답 본문은 출력하지 않는다. 동의 전이면 BLOCK(CEO 동의 대기)로 종료(코드 3).
실행: docker exec aads-server python3 /app/scripts/clobe_mcp_verify.py   (timeout 필수: R-BG)
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")


async def main() -> int:
    from app.core.db_pool import close_pool, init_pool
    from app.services import clobe_mcp_client as clobe

    await init_pool()
    try:
        status = await clobe.get_status()
        if status["status"] != clobe.STATUS_CONNECTED:
            print(json.dumps({"verdict": "BLOCK", "reason": "CEO 동의 대기", **status}, ensure_ascii=False))
            return 3
        try:
            result = await clobe.verify_connection()
        except clobe.ClobeError as exc:
            print(json.dumps({"verdict": "FAIL", "reason": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"verdict": "PASS", **result}, ensure_ascii=False))
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
