"""캡챠 비전 솔버 단독 검증 — 저장된 챌린지 스크린샷으로 숫자 판독 확인."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def main() -> int:
    from app.services.captcha_vision_solver import solve_captcha_with_vision

    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target:
        base = Path("app/data/yeoljeong_finance/delivery_auth_challenges")
        shots = sorted(base.glob("*ddangyo*.png"), key=lambda p: p.stat().st_mtime)
        if not shots:
            print("NO_SCREENSHOT")
            return 2
        target = str(shots[-1])

    print(f"TARGET={target}")
    digits = await solve_captcha_with_vision(None, screenshot_path=target, max_retries=1)
    print(f"RESULT={digits or 'EMPTY'}")
    return 0 if digits else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
