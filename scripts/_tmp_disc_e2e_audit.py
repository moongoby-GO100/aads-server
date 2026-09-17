import asyncio, time, json
from app.services.discussion_orchestrator import orchestrator, DiscussionMode


async def main():
    t0 = time.time()
    gen = orchestrator.start_discussion(
        "e2e-audit", "토론 기능 점검", mode=DiscussionMode.MANUAL, preset="standard", budget_usd=1.0
    )
    async for chunk in gen:
        ev = json.loads(chunk[6:])
        print(
            "%6dms %-20s %s | %s"
            % (
                int((time.time() - t0) * 1000),
                ev.get("event"),
                str(ev.get("participant") or ev.get("message") or "")[:25],
                str(ev.get("content") or "")[:70],
            ),
            flush=True,
        )


try:
    asyncio.run(asyncio.wait_for(main(), timeout=180))
except Exception as e:
    print("TIMEOUT_OR_FAIL", type(e).__name__, flush=True)
