import asyncio
import sys

sys.path.insert(0, "/app")
from app.services.loop_chat_handler import detect_loop_intent, handle_loop_start_confirm  # noqa: E402
from app.services import loop_chat_handler  # noqa: E402

CASES = [
    # (원문, 기대값)
    ("스캘핑 전략 10선 각 전략카드 보완 백서 모두 작성후 보고해 완료시까지 진행하고 모두 완료하고 보고해", None),
    ("완료시까지 진행하고 보고해", None),
    ("서버 10분마다 감시해 이상 있으면 보고해", "loop_start_confirm"),
    ("서버 10분마다 감시해", "loop_start_confirm"),
    ("매 10분 감시해", "loop_start_confirm"),
    ("끝날 때까지 계속 진행해", None),
    ("루프 시작", "loop_start_confirm"),
    ("루프 진행", "loop_start_confirm"),
    ("루프 중지", "loop_stop"),
    ("루프 상태", "loop_status"),
    ("진행해", None),
    ("이어서 진행해", None),
    ("확인했다 보고해", None),
    ("매출 보고해", None),
    ("매일 보고해", None),
]

fail = 0
for text, expect in CASES:
    got = detect_loop_intent(text, session_id="verify-loop")
    ok = got == expect
    if not ok:
        fail += 1
    print(f"{'OK ' if ok else 'FAIL'} | expect={expect!s:20} got={got!s:20} | {text[:45]}")

loop_chat_handler._remember_pending("verify-loop-confirm", "서버 10분마다 감시해 이상 있으면 보고해")
got = detect_loop_intent("루프 진행", session_id="verify-loop-confirm")
ok = got == "loop_start"
if not ok:
    fail += 1
print(f"{'OK ' if ok else 'FAIL'} | expect={'loop_start':20} got={got!s:20} | pending confirm 승인")

print("-" * 70)


async def _confirm():
    r = await handle_loop_start_confirm("서버 10분마다 감시해 이상 있으면 보고해", "test-session")
    print("confirm.loop_type      =", r["loop_type"])
    print("confirm.interval_secs  =", r["interval_seconds"])
    print("confirm.pending_confirm=", r["pending_confirm"])
    print("confirm.message:")
    print(r["message"])


asyncio.run(_confirm())
print("-" * 70)
print(f"RESULT: {len(CASES) - fail}/{len(CASES)} passed, {fail} failed")
sys.exit(1 if fail else 0)
