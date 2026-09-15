"""읽기 전용 진단 — 리뷰 모델 경로별 실제 소요시간 측정 (AI 리뷰 인프라 점검용)."""
import asyncio
import sys
import time

sys.path.insert(0, "/app")

PROMPT = "다음 diff 를 리뷰하고 JSON 으로만 답하라.\n" + ("# sample line of code change\n" * 400)
SYSTEM = "You are a code reviewer. Reply with JSON only."


async def probe(model: str, timeout: float = 120.0):
    from app.services.code_reviewer import _call_review_model

    t0 = time.monotonic()
    try:
        text = await asyncio.wait_for(
            _call_review_model(model=model, prompt=PROMPT, system=SYSTEM, max_tokens=1024),
            timeout=timeout,
        )
        dt = time.monotonic() - t0
        print(f"MODEL={model} elapsed={dt:.1f}s len={len(text or '')} preview={(text or '')[:80]!r}", flush=True)
    except asyncio.TimeoutError:
        print(f"MODEL={model} elapsed=TIMEOUT>{timeout}s", flush=True)
    except Exception as exc:  # noqa: BLE001
        dt = time.monotonic() - t0
        print(f"MODEL={model} elapsed={dt:.1f}s ERROR={type(exc).__name__}: {str(exc)[:160]}", flush=True)


async def main():
    for model in sys.argv[1:]:
        await probe(model)


asyncio.run(main())
