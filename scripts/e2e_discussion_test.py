"""E2E test for discussion orchestrator SSE flow (mocked LLM)."""
import asyncio
import json
from unittest.mock import AsyncMock, patch


async def main():
    results = []

    # Test 1: Manual mode SSE flow
    with patch(
        "app.services.discussion_orchestrator.call_llm_with_fallback",
        new_callable=AsyncMock,
        return_value="테스트 응답입니다.",
    ):
        from app.services.discussion_orchestrator import (
            DiscussionMode,
            DiscussionOrchestrator,
        )

        orch = DiscussionOrchestrator()
        events = []
        gen = orch.start_discussion(
            session_id="e2e-manual",
            topic="E2E 모의 토론",
            mode=DiscussionMode.MANUAL,
            preset="light",
            budget_usd=1.0,
        )
        async for chunk in gen:
            if chunk.startswith("data: "):
                ev = json.loads(chunk[6:].strip())
                events.append(ev["event"])

        required = {"discussion_start", "round_start", "participant_reply", "round_complete", "wait_ceo"}
        missing = required - set(events)
        ok = not missing
        results.append(("Manual SSE flow", ok, events, list(missing)))
        print(f"[{'PASS' if ok else 'FAIL'}] Test 1: Manual SSE — events={events}")

    # Test 2: Continue + stop → synthesis
    with patch(
        "app.services.discussion_orchestrator.call_llm_with_fallback",
        new_callable=AsyncMock,
        return_value="종합 결과입니다.",
    ):
        orch2 = DiscussionOrchestrator()
        # Start
        async for _ in orch2.start_discussion(
            session_id="e2e-stop",
            topic="종료 테스트",
            mode=DiscussionMode.MANUAL,
            preset="light",
            budget_usd=1.0,
        ):
            pass

        # Continue with stop command
        events2 = []
        async for chunk in orch2.continue_discussion("e2e-stop", "그만"):
            if chunk.startswith("data: "):
                ev = json.loads(chunk[6:].strip())
                events2.append(ev["event"])

        required2 = {"ceo_stop", "synthesis_start", "synthesis_complete"}
        missing2 = required2 - set(events2)
        ok2 = not missing2
        results.append(("Stop + Synthesis", ok2, events2, list(missing2)))
        print(f"[{'PASS' if ok2 else 'FAIL'}] Test 2: Stop+Synthesis — events={events2}")

    # Test 3: inject/cancel/status on non-existent session
    orch3 = DiscussionOrchestrator()
    t3a = orch3.inject_ceo_directive("no-exist", "test") is False
    t3b = orch3.cancel_discussion("no-exist") is False
    t3c = orch3.get_discussion_status("no-exist") is None
    t3d = orch3.get_active_discussion("no-exist") is None
    ok3 = t3a and t3b and t3c and t3d
    results.append(("Non-existent session guards", ok3, [t3a, t3b, t3c, t3d], []))
    print(f"[{'PASS' if ok3 else 'FAIL'}] Test 3: Guards — {[t3a, t3b, t3c, t3d]}")

    # Test 4: Presets import
    from app.services.discussion_presets import (
        DISCUSSION_PRESETS,
        estimate_round_cost,
        get_preset,
        resolve_model_name,
    )

    t4a = set(DISCUSSION_PRESETS.keys()) == {"standard", "deep", "light"}
    t4b = get_preset("unknown")["name"] == "standard"
    t4c = resolve_model_name("옵스") == "claude-opus-4-6"
    t4d = estimate_round_cost(get_preset("standard")) > 0
    ok4 = t4a and t4b and t4c and t4d
    results.append(("Presets module", ok4, [t4a, t4b, t4c, t4d], []))
    print(f"[{'PASS' if ok4 else 'FAIL'}] Test 4: Presets — {[t4a, t4b, t4c, t4d]}")

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r[1])
    print(f"\n{'='*40}")
    print(f"E2E Result: {passed}/{total} PASSED")
    if passed == total:
        print("ALL TESTS PASSED ✓")
    else:
        for name, ok, _, missing in results:
            if not ok:
                print(f"  FAILED: {name} — missing: {missing}")


if __name__ == "__main__":
    asyncio.run(main())
