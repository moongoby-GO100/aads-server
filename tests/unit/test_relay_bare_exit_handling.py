"""출력 없이 사라진 CLI 를 계정 탓으로 돌리지 않는다.

2026-09-15 확인 — `CLI exited 255` 가 하루 62번 났고 **전부 슬롯 2**였다.
그리고 62번 모두 asyncio 경고와 짝을 이뤘다.

    asyncio: child process pid … exit status already read:
             will report returncode 255

자식 프로세스를 다른 곳에서 먼저 거둬 가면 asyncio 는 실제 종료 상태 대신
255 를 **지어낸다.** 그 지어낸 값을 보고 슬롯에 쿨다운을 걸고 있었다.

쓸 수 있는 계정이 하나뿐인 날에는 그 쿨다운 한 번이 대화 하나를 죽인다 —
15:11 에 실제로 3분간 조용히 죽었다(슬롯1 주간 한도 소진, 슬롯3 꺼짐).
"""
import inspect
import pathlib

import pytest

from app.services import model_selector as ms

REPO = pathlib.Path(__file__).resolve().parents[2]
RELAY = (REPO / "scripts" / "claude_relay_server.py").read_text()


def _body(fn) -> str:
    return "\n".join(
        l for l in inspect.getsource(fn).splitlines() if not l.strip().startswith("#")
    )


def test_relay_marks_no_output_failures():
    """앱이 '진짜 오류' 와 '출력 없이 사라짐' 을 구분할 수 있어야 한다."""
    assert "cli_no_output:" in RELAY
    assert "elapsed=%.1fs events=%d" in RELAY


def test_bare_exit_detector():
    assert ms._is_bare_cli_exit("cli_no_output: CLI 가 출력 한 줄 없이 종료했습니다 (code=255 elapsed=7.1s events=0 stderr=없음)")
    # 진짜 사유가 있는 실패는 여기 걸리면 안 된다 — 그건 계정 문제일 수 있다.
    assert not ms._is_bare_cli_exit("CLI exited with code 1: invalid api key")
    assert not ms._is_bare_cli_exit("429 rate limit")
    assert not ms._is_bare_cli_exit("")


def test_bare_exit_does_not_cool_down_the_slot():
    body = _body(ms.call_stream) if hasattr(ms, "call_stream") else ""
    src = inspect.getsource(ms)
    # 맨 CLI 종료 분기가 쿨다운보다 **먼저** 와야 한다. 뒤에 오면 영영 안 걸린다.
    idx_bare = src.index("elif _is_bare_cli_exit(_err_msg):")
    idx_cool = src.index('elif any(k in _err_lower for k in ("cli exited"')
    assert idx_bare < idx_cool


def test_bare_exit_retries_the_same_slot_once():
    src = inspect.getsource(ms)
    assert "cli_no_output_retry:" in src
    # 재시도가 또 실패하면 그때는 슬롯을 의심한다 — 무한 재시도는 안 된다.
    assert "cli_no_output_retry_failed:" in src


def test_no_slots_left_is_never_silent():
    """폴백이 0개가 되는 날은 드물다. 드물어서 아무도 대비하지 않는다."""
    body = _body(ms._alert_no_slots_left)
    assert "ohvis_alert" in body
    assert "CRITICAL" in body
    assert "dedupe_minutes" in body
    src = inspect.getsource(ms)
    assert "await _alert_no_slots_left(" in src
