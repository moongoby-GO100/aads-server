"""승인 게이트 3단계 — 진짜 승인 대상만 막는다.

2026-09-15 대표님 지시: "완전 진짜 내 승인을 받아야하는것만 승인 받게".

그 전 실측에서 대기 8건 중 진짜 승인 대상은 **2건**이었다. 나머지는 읽기
명령, 문서 작성, 목표 정리였는데 전부 `[실매매] high` 로 올라왔다.
오탐이 여섯이면 다음번엔 내용을 안 보고 누르게 되고, 그때 진짜 하나가 같이
통과한다 — 오탐은 규칙을 죽인다.
"""
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "live_trading_guard_under_test", REPO / "app" / "services" / "live_trading_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


PASS_CASES = [
    # 읽기는 `2>/dev/null` 이 붙어도 읽기다. 이것이 막혀서 담당이 grep 한 번에
    # 승인을 기다렸다.
    ("run_remote_command", {"command": "grep -n 'def ' /srv/go100/live_trading/live_engine.py 2>/dev/null"}),
    ("run_remote_command", {"command": "psql -c 'select * from v4_orders limit 5' 2>/dev/null"}),
    ("run_remote_command", {"command": "cat /srv/live_trading/config.yaml"}),
    ("run_remote_command", {"command": "tail -100 /var/log/scalping.log 2>&1"}),
    # 파일명에 risk·scalping 이 있다는 것과 그 파일이 주문을 바꾼다는 것은 다르다.
    ("patch_remote_file", {"file_path": "docs/risk_report.md"}),
    ("write_remote_file", {"file_path": "reports/scalping_backtest_result.md"}),
    ("write_remote_file", {"file_path": "tests/test_live_engine.py"}),
    # 실매매 코드를 **시험하는 파일 이름**에 live_trading 이 들어 있다는 이유로
    # 시험 실행이 막혔다. 시험을 돌리는 것과 실매매를 바꾸는 것은 다른 일이다.
    ("run_remote_command",
     {"command": "bash scripts/run_unit_tests.sh tests/unit/test_live_trading_guard.py > /tmp/t.log 2>&1"}),
    ("run_remote_command",
     {"command": "pytest /root/aads/aads-server/tests/unit/test_live_engine.py"}),
    # 헬스체크와 상태 조회는 읽기다.
    ("run_remote_command",
     {"command": "curl -s http://127.0.0.1:8002/health 2>/dev/null; systemctl is-active go100.service"}),
    ("write_remote_file", {"file_path": "docs/plans/live_trading_plan.md"}),
]

APPROVE_CASES = [
    ("patch_remote_file",
     {"file_path": "backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": "sed -i 's/a/b/' /srv/live_trading/params.py"}, "high"),
    # 돈이 직접 걸린 것은 critical 로 올린다.
    ("run_remote_command", {"command": "systemctl restart go100-kiwoom-scalping"}, "critical"),
    ("db_safe_write",
     {"sql": "UPDATE go100_strategy_cards SET is_live=true WHERE id=310"}, "critical"),
    ("db_safe_write",
     {"sql": "UPDATE go100_strategy_cards SET allocated_amount=10000000"}, "critical"),
]


@pytest.mark.parametrize("tool,inp", PASS_CASES)
def test_reads_documents_and_tests_pass(tool, inp):
    tier, _risk, _why = guard.classify_tier(tool, inp)
    assert tier == guard.TIER_PASS, (tool, inp, tier)


@pytest.mark.parametrize("tool,inp,risk", APPROVE_CASES)
def test_money_and_live_code_still_blocked(tool, inp, risk):
    tier, got_risk, why = guard.classify_tier(tool, inp)
    assert tier == guard.TIER_APPROVE, (tool, inp)
    assert got_risk == risk, (tool, got_risk, risk)
    assert why


def test_null_redirect_is_not_a_write():
    assert guard._has_write_redirect("grep x y 2>/dev/null") is False
    assert guard._has_write_redirect("cmd 2>&1") is False
    assert guard._has_write_redirect("cmd > /srv/live_trading/out.txt") is True


def test_risk_word_alone_no_longer_matches():
    """`risk` 는 심볼로만 본다."""
    assert guard._GUARDED_PATH.search("docs/risk_report.md") is None
    assert guard._GUARDED_PATH.search("app/core/risk_limit.py") is not None


def test_classify_stays_compatible():
    """옛 호출부는 (막을것인가, 사유) 두 값을 그대로 받는다."""
    blocked, why = guard.classify(
        "patch_remote_file",
        {"file_path": "backend/app/services/go100/live_trading/live_engine.py"},
    )
    assert blocked is True and why
    blocked, _ = guard.classify("run_remote_command", {"command": "ls /tmp"})
    assert blocked is False
