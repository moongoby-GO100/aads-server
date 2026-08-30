"""AADS-LOOP-FP-001 검증: detect_loop_intent 오탐/정탐 회귀 테스트."""
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
sys.path.insert(0, "/app")
from app.services.loop_chat_handler import detect_loop_intent  # noqa: E402

QUOTED_INCIDENT = (
    "[CEO가 지정한 이전 AI 응답 (reply_to)]\n"
    "루프 시스템 구현 상태 보고입니다. Phase 1 코드 완료. "
    "AI 리뷰 REQUEST_CHANGES로 중단되었으며 루프 실행은 중지 상태입니다.\n"
    "\n[CEO 추가 지시]\n이어서 진행해"
)

CASES = [
    # (입력, 기대값, 설명)
    ("이어서 진행해", None, "인용 제거된 실제 CEO 입력"),
    (QUOTED_INCIDENT, None, "reply_to 인용문 원문(2000자 오염) — 오탐 차단"),
    ("루프 구현상태 보고해", None, "루프 구현 보고 요청 — 오탐 차단"),
    ("루프기능 구현을 모두 구현하고 e2e테스트까지 검증하고 보고해", None, "구현 지시 — 오탐 차단"),
    ("여기 세션에 활성 루프가 없습니다. 라는 메시지가 나오는데 뭐지?", None, "질의 — 오탐 차단"),
    ("루프와 일반 지시의 차이가 뭐지", None, "개념 질문 — 오탐 차단"),
    (
        "해당 버블 안에서 작업은 진행되는데 저장중으로 버블 하단 아이콘이 바뀌면 "
        "버블 하트비트 커서가 사라지고 버블이 멈춘듯보이도 채팅창 스크롤이 상단으로 "
        "이동되고 잠시있다 다시 생성중으로 바뀌면서 버블이 이어서 내용을 바꾸는과정이 "
        "완료시까지 이어진다 이부분 증상 확인하고 원인 문제점 개선안 보고해",
        None,
        "증상 설명의 완료시까지 — START 오탐 차단",
    ),
    ("서버 상태 10분마다 감시해", "loop_start_confirm", "정탐: 시작 확인"),
    ("루프 중지", "loop_stop", "정탐: 중지"),
    ("루프 #3 취소", "loop_stop", "정탐: 특정 루프 취소"),
    ("루프 상태", "loop_status", "정탐: 상태"),
    ("활성 루프", "loop_status", "정탐: 활성 루프 목록"),
    ("루프 #2 재개", "loop_resume", "정탐: 재개"),
]

fail = 0
for text, expected, desc in CASES:
    got = detect_loop_intent(text)
    ok = got == expected
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'} | expected={expected!s:12} got={got!s:12} | {desc}")

print(f"\nTOTAL={len(CASES)} PASS={len(CASES) - fail} FAIL={fail}")
sys.exit(1 if fail else 0)
