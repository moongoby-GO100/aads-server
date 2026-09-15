"""실매매 승인 게이트 회귀 테스트.

2026-09-14 CEO 지시 — "실매매조건은 나의 승인후 진행해야지".

**이 테스트가 존재하는 이유는 프롬프트를 못 믿기 때문이다.** 역할 지침에
"승인 후" 라고 적어 뒀지만 같은 날 하루에 에이전트가 자기 규칙을 세 번
어겼고, 그중 하나는 오류 사전에 등록해 둔 항목을 **다섯 번** 밟은 것이다.

AGENTS.md: "에이전트는 자기가 쓴 규칙도 어긴다. 두 번 이상 어긴 규칙은
코드 차단으로 옮긴다."

지켜야 할 것 넷.
1. 실매매 경로를 **쓰는** 도구는 막힌다.
2. **읽는** 도구는 안 막힌다 — 담당들이 일을 못 하면 하네스가 무의미하고,
   정상 작업을 막으면 우회할 길을 찾는다.
3. 게이트가 고장 나면 **막는 쪽**으로 실패한다.
4. 승인은 **사람만** 할 수 있다. 에이전트 도구로 노출되면 게이트가 없는 것과 같다.
"""
import inspect

import pytest

from app.services.live_trading_guard import classify


BLOCK = [
    ("patch_remote_file", {"file_path": "backend/app/services/go100/live_trading/live_engine.py"}),
    ("patch_remote_file", {"file_path": "backend/app/services/go100/live_trading/scalping_entry_engine.py"}),
    ("patch_remote_file", {"file_path": "backend/app/services/go100/strategy/card_service.py"}),
    ("write_remote_file", {"file_path": "backend/app/services/execution/order_executor.py"}),
    ("patch_remote_file", {"file_path": "backend/app/routers/go100/card_trades_router.py"}),
    ("db_safe_write", {"query": "UPDATE live_promotion_conditions SET threshold=0.05"}),
    ("run_remote_command", {"command": "sed -i 's/0.03/0.05/' backend/app/services/go100/live_trading/live_engine.py"}),
    ("run_remote_command", {"command": "echo 0.05 > backend/app/services/go100/live_trading/threshold.conf"}),
    ("run_remote_command", {"command": "systemctl restart go100-kiwoom-scalping"}),
]

PASS = [
    # 읽기 — 아무리 민감한 경로여도 막지 않는다
    ("read_remote_file", {"file_path": "backend/app/services/go100/live_trading/live_engine.py"}),
    ("run_remote_command", {"command": "cat backend/app/services/go100/live_trading/live_engine.py"}),
    ("run_remote_command", {"command": "grep -n threshold backend/app/services/go100/live_trading/live_engine.py"}),
    ("run_remote_command", {"command": "tail -100 /var/log/go100/scalping.log"}),
    ("query_project_database", {"query": "SELECT * FROM card_trades WHERE card_id=310"}),
    # 실매매 경로가 아닌 곳은 쓰기여도 통과
    ("patch_remote_file", {"file_path": "backend/app/services/go100/analysis/backtest.py"}),
    ("write_remote_file", {"file_path": "docs/go100/card310-report.md"}),
]


@pytest.mark.parametrize("tool,inp", BLOCK)
def test_write_to_live_trading_is_blocked(tool, inp):
    blocked, reason = classify(tool, inp)
    assert blocked, f"막혔어야 한다: {tool} {inp}"
    assert reason


@pytest.mark.parametrize("tool,inp", PASS)
def test_reads_and_non_trading_paths_pass(tool, inp):
    blocked, _ = classify(tool, inp)
    assert not blocked, f"통과했어야 한다 — 정상 작업을 막으면 우회한다: {tool} {inp}"


def test_gate_fails_closed():
    """게이트가 고장 나도 실매매 변경은 막혀야 한다.

    돈이 걸린 경로에서 "검사를 못 했으니 통과" 는 안 된다.
    """
    from app.services import tool_executor

    src = inspect.getsource(tool_executor.ToolExecutor.execute)
    assert "live_trading_guard" in src, "게이트가 도구 실행 경로에 연결돼 있지 않다"
    assert "failclosed" in src or "fail-closed" in src or "_should_block" in src, (
        "게이트 예외 시 통과시키고 있다"
    )
    # 예외 처리 블록 안에서 다시 classify 로 판정하는지
    idx = src.find("except Exception as _lt_err")
    assert idx > 0
    assert "classify" in src[idx: idx + 900]


def test_approval_is_not_an_agent_tool():
    """에이전트가 자기 요청을 스스로 승인할 수 없어야 한다."""
    from app.services import tool_executor

    src = inspect.getsource(tool_executor)
    for name in ("approvals_decide", "approve_request", "decide_approval"):
        assert f'"{name}"' not in src, f"승인이 도구로 노출돼 있다: {name}"


def test_gate_can_be_switched_off_without_deploy():
    """게이트가 정상 작업을 막을 때 배포 없이 풀 수 있어야 한다."""
    from app.services import live_trading_guard

    src = inspect.getsource(live_trading_guard)
    assert "LIVE_TRADING_GATE_ENABLED" in src


def test_pending_uses_pending_not_null():
    """`decision` 은 NOT NULL 이고 기본값이 'pending' 이다.

    2026-09-14 첫 구현에서 `WHERE decision IS NULL` 로 찾아 **대기 목록이
    항상 비어 있었다.** 차단은 되는데 승인할 대상이 안 보이니 담당은
    막히기만 하고 풀릴 길이 없었다 — 게이트가 막다른 길이 된다.
    """
    import inspect

    from app.services import live_trading_guard
    from app.api import project_docs

    for mod in (live_trading_guard, project_docs):
        src = inspect.getsource(mod)
        assert "decision IS NULL" not in src, f"{mod.__name__} 이 NULL 로 대기를 찾고 있다"

    src = inspect.getsource(live_trading_guard.request_approval)
    # 2026-09-15: 알림 등급이 생기면서 대기 판정이 `decision = ANY($2)` 로
    # 바뀌었다. 지키려는 것은 문자열이 아니라 **NULL 로 찾지 않는다** 는 것과
    # 대기 상태를 명시적으로 나열한다는 것이다.
    assert "decision = ANY($2::text[])" in src
    assert '"pending"' in src


def test_request_outlives_default_expiry():
    """기본 만료가 10분이다. CEO 가 그 안에 못 보면 요청이 사라진다."""
    import inspect

    from app.services import live_trading_guard

    src = inspect.getsource(live_trading_guard.request_approval)
    assert "expires_at" in src and "24 hours" in src, (
        "요청이 기본 10분 만료를 그대로 쓰고 있다"
    )


def test_tenant_is_resolved_not_null():
    """tenant_id 는 NOT NULL 이다. 없으면 요청 자체가 안 남는다."""
    import inspect

    from app.services import live_trading_guard

    src = inspect.getsource(live_trading_guard.request_approval)
    assert "FROM chat_sessions" in src, "세션에서 tenant 를 못 가져오고 있다"
