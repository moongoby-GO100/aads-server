"""캡챠 비전 헬퍼 단독 검증.

승인 없는 CAPTCHA 판독은 금지한다. 운영자가 명시적으로 검증을 승인한
경우에만 `--approved`와 함께 실행한다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def main() -> int:
    from app.services.captcha_vision_solver import solve_captcha_with_vision

    approved = "--approved" in sys.argv
    args = [item for item in sys.argv[1:] if item != "--approved"]
    target = args[0] if args else ""
    if not approved:
        print("APPROVAL_REQUIRED")
        return 2
    if not target:
        base = Path("app/data/yeoljeong_finance/delivery_auth_challenges")
        shots = sorted(base.glob("*ddangyo*.png"), key=lambda p: p.stat().st_mtime)
        if not shots:
            print("NO_SCREENSHOT")
            return 2
        target = str(shots[-1])

    print(f"TARGET={target}")
    digits = await solve_captcha_with_vision(
        None,
        screenshot_path=target,
        max_retries=1,
        approval_context={
            "approved": True,
            "approved_by": "operator_cli",
            "challenge_kind": "captcha",
            "automation": "llm_vision_read_and_fill",
        },
    )
    print(f"RESULT={'DIGITS_DETECTED' if digits else 'EMPTY'}")
    return 0 if digits else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
