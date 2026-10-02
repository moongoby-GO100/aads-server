"""추가 지시 중단을 모델 실패로 오판하지 않는다 + 이어쓰기는 턴 요청 모델 우선.

2026-10-02 13:01 KST 실측: tool_result 직후 CEO 추가 지시로 async for 를 끊자
재시도 루프가 다음 시도로 넘어가 "[claude-opus-5-5 실행 불가 → gpt-6-astra 전환]"
배너가 붙었다. 이어쓰기는 세션 current_model 을 먼저 봐서 Opus 5.5 요청 턴이
gpt-5.6-sol 로 이어졌다(24h 8건).
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "app" / "services" / "chat_service.py"
TEXT = SRC.read_text(encoding="utf-8")


def _retry_loop() -> ast.For:
    tree = ast.parse(TEXT)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_stream_attempt"
        ):
            return node
    raise AssertionError("_stream_attempt 재시도 루프를 찾지 못함")


def test_interrupt_break_sets_flag_before_break():
    idx = TEXT.index('logger.info(f"interrupt_after_tool_result session={session_id[:8]}")')
    tail = TEXT[idx: idx + 300]
    assert tail.index("_interrupt_stream_break = True") < tail.index("break")


def test_retry_loop_exits_on_interrupt_break():
    loop = _retry_loop()
    found = False
    for stmt in loop.body:
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Name)
            and stmt.test.id == "_interrupt_stream_break"
            and any(isinstance(b, ast.Break) for b in stmt.body)
        ):
            found = True
    assert found, "재시도 루프 본문 최상위에 'if _interrupt_stream_break: break' 가 있어야 한다"


def test_flag_reset_each_attempt():
    loop = _retry_loop()
    names = [
        t.id
        for stmt in loop.body
        if isinstance(stmt, ast.Assign)
        for t in stmt.targets
        if isinstance(t, ast.Name)
    ]
    assert "_interrupt_stream_break" in names


def test_resume_prefers_turn_requested_model_over_session_current():
    start = TEXT.index("async def _resume_single_stream(")
    body = TEXT[start:]
    assert body.index("resume_model_from_execution") < body.index("resume_model_from_session_current")
