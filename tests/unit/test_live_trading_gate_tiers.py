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
    # 2026-10-15 오탐 실측 3건(agent_permission_requests 309f32ac / fa494c4d / 0be75bd9)
    # 309f32ac: 따옴표 안 `<> ` 의 `>` 를 쓰기 리다이렉트로 봤다.
    ("run_remote_command",
     {"command": 'cd /tmp/aads-wt-runner-dac7de61 && grep -n -e "DISTINCT FROM" -e "event_type = ANY" '
                 '-e "event_type <> " backend/app/services/go100/live_trading/card_lane_scheduler.py'}),
    # fa494c4d: 조회 전용 git/echo/cut/head 묶음 (워크트리 안, 따옴표 문구 포함).
    ("run_remote_command",
     {"command": 'cd /tmp/aads-wt-runner-37afb44c && git log --oneline -2 | cut -c1-70; '
                 'echo origin=$(git ls-remote origin main | cut -c1-9); git status --short | head -5; '
                 'echo "--- diff vs b4e276172 (8 files) -> live_trading"; '
                 'git diff --stat b4e276172 -- backend/app/services/go100/live_trading/card310_hot_pool.py'}),
    # 0be75bd9: 테스트 실행 + /tmp 로그 리다이렉트.
    ("run_remote_command",
     {"command": "cd /tmp/aads-wt-runner-a6187af3 && git show HEAD -- backend/app/services/go100/live_trading/live_engine.py "
                 "| grep -E '^[+-]' | grep -v '^+++\\|^---' | cut -c1-150; cd backend && "
                 "(nohup timeout 300 nice -n 19 /root/venv/bin/python -m pytest -q tests/go100/test_card310_hot_pool.py "
                 "> /tmp/t310.log 2>&1 &)"}),
    ("run_remote_command",
     {"command": "git blame -L1,5 backend/app/services/go100/live_trading/live_engine.py >> /tmp/blame.out"}),
    ("run_remote_command",
     {"command": 'echo "a; b" && git grep -n "x > y" -- backend/app/services/go100/live_trading/'}),
    # 리뷰 지적 2: 검색어(따옴표 안)에 실행 도구 이름이 들어 있어도 읽기 명령이다.
    ("run_remote_command",
     {"command": 'git grep -n "awk x > y" -- backend/app/services/go100/live_trading/'}),
    ("run_remote_command",
     {"command": 'git grep -n "ssh host > y" -- backend/app/services/go100/live_trading/'}),
    ("run_remote_command",
     {"command": "grep -rn 'bash -c \"a > b\"' backend/app/services/go100/live_trading/"}),
    ("run_remote_command",
     {"command": 'grep -n "xargs <> eval" backend/app/services/go100/live_trading/live_engine.py | head -5'}),
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
    # 오탐 완화 후에도 막혀야 하는 것 (약화 금지).
    ("run_remote_command",
     {"command": "cd /root/kis-autotrade-v4 && echo x > backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": "sed -i 's/a/b/' backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": "git push origin main  # backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": "systemctl restart go100-card-lane@360"}, "critical"),
    # 러너 워크트리라는 이유만으로 통과시키지 않는다.
    ("run_remote_command",
     {"command": "cd /tmp/aads-wt-runner-37afb44c && git push origin main && git log backend/app/services/go100/live_trading/"}, "high"),
    ("run_remote_command",
     {"command": "cd /tmp/aads-wt-runner-37afb44c && echo x > backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    # /tmp 아래여도 실매매 소스에 쓰면 막는다.
    ("run_remote_command",
     {"command": "echo x > /tmp/aads-wt-runner-37afb44c/backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": "echo x >> /tmp/live_trading/params.py"}, "high"),
    # 경로 순회·치환·따옴표로 쪼갠 /tmp 대상.
    ("run_remote_command", {"command": 'echo x > /tmp/".."/srv/live_trading/params.py'}, "high"),
    ("run_remote_command", {"command": "echo x > /tmp/../srv/live_trading/params.py"}, "high"),
    ("run_remote_command", {"command": "echo x > /tmp/$(echo ../srv)/live_trading/p.py"}, "high"),
    ("run_remote_command", {"command": 'echo x > "/srv/live_trading/params.py"'}, "high"),
    # 중첩 따옴표가 든 명령 치환 속 변경 명령.
    ("run_remote_command", {"command": 'echo "$(printf "%s" x; rm /srv/live_trading/params.py)"'}, "high"),
    ("run_remote_command", {"command": 'echo "$(printf "%s" x; echo y > /srv/live_trading/p.py)"'}, "high"),
    ("run_remote_command", {"command": 'echo "`echo y > /srv/live_trading/p.py`"'}, "high"),
    # 리뷰 지적 1: 따옴표 문자열이 실행될 셸 명령인 경우(셸 이름을 따옴표로 쪼개도, stdin 으로 넘겨도).
    ("run_remote_command", {"command": 'bash "-c" "echo x > /srv/live_trading/params.py"'}, "high"),
    ("run_remote_command", {"command": 'bash -c "echo x > /srv/live_trading/params.py"'}, "high"),
    ("run_remote_command", {"command": '"sh" "-c" "echo x > /srv/live_trading/params.py"'}, "high"),
    ("run_remote_command", {"command": "ba''sh -c 'echo x > /srv/live_trading/params.py'"}, "high"),
    ("run_remote_command", {"command": "\\sh -c 'echo x > /srv/live_trading/params.py'"}, "high"),
    ("run_remote_command", {"command": "printf '%s\\n' 'echo x > /srv/live_trading/params.py' | sh"}, "high"),
    ("run_remote_command", {"command": "echo 'echo x > /srv/live_trading/params.py' | bash -s"}, "high"),
    ("run_remote_command", {"command": "echo 'echo x > /srv/live_trading/params.py' | $SHELL"}, "high"),
    ("run_remote_command", {"command": "x=sh; $x -c 'echo x > /srv/live_trading/params.py'"}, "high"),
    ("run_remote_command", {"command": "eval 'echo x > /srv/live_trading/params.py'"}, "high"),
    ("run_remote_command", {"command": "ssh host 'echo x > /srv/live_trading/params.py'"}, "high"),
    ("run_remote_command", {"command": "echo 'echo x > /srv/live_trading/params.py' | xargs -I{} sh -c {}"}, "high"),
    ("run_remote_command", {"command": "awk 'BEGIN{print 1 > \"/srv/live_trading/params.py\"}'"}, "high"),
    ("run_remote_command", {"command": "echo \"$(sh -c 'echo x > /srv/live_trading/p.py')\""}, "high"),
    ("run_remote_command", {"command": "cat <<'EOF' | sh\necho x > /srv/live_trading/p.py\nEOF"}, "high"),
    ("run_remote_command", {"command": "git -c alias.l='!echo x > /srv/live_trading/p.py' l"}, "high"),
    ("run_remote_command", {"command": "git grep -O'sh -c \"echo x > /srv/live_trading/p.py\"' foo"}, "high"),
    # 3차 반려 우회 3종(2026-10-15): 환경변수 실행·git 옵션 축약/묶음·/tmp 스크립트 작성.
    ("run_remote_command",
     {"command": "GIT_EXTERNAL_DIFF='sh -c \"echo x > /root/kis-autotrade-v4/backend/app/services/go100/live_trading/live_engine.py\"' git diff HEAD~1"}, "high"),
    ("run_remote_command",
     {"command": "GIT_SSH_COMMAND='sh -c \"echo x > /root/kis-autotrade-v4/backend/app/services/go100/live_trading/live_engine.py\"' git ls-remote ssh://h/r"}, "high"),
    ("run_remote_command",
     {"command": "git grep -nO'sh -c \"echo x > backend/app/services/go100/live_trading/live_engine.py\"' -e foo"}, "high"),
    ("run_remote_command",
     {"command": "git grep --open-files='sh -c \"echo x > backend/app/services/go100/live_trading/live_engine.py\"' -e foo"}, "high"),
    ("run_remote_command",
     {"command": "git ls-remote --upload='sh -c \"echo x > backend/app/services/go100/live_trading/live_engine.py\"' ."}, "high"),
    ("run_remote_command",
     {"command": "printf 'echo x > backend/app/services/go100/live_trading/live_engine.py' > /tmp/e.sh"}, "high"),
    # 같은 계열의 변형: 옵션과 인자를 떼어 쓰기, 파이프 끝에서 파일로 쓰기, 다른 실행 환경변수.
    ("run_remote_command",
     {"command": "git grep -O 'sh -c \"echo x > backend/app/services/go100/live_trading/p.py\"' -e foo"}, "high"),
    ("run_remote_command",
     {"command": "echo 'echo x > backend/app/services/go100/live_trading/p.py' | cat > /tmp/e.sh"}, "high"),
    ("run_remote_command",
     {"command": "PAGER='sh -c \"echo x > backend/app/services/go100/live_trading/p.py\"' git log -1"}, "high"),
    ("run_remote_command",
     {"command": "LC_ALL=C GIT_PAGER='sh -c \"echo x > backend/app/services/go100/live_trading/p.py\"' git log -1"}, "high"),
    ("run_remote_command",
     {"command": "GIT_EXTERNAL_DIFF='sh -c \"echo x > backend/app/services/go100/live_trading/p.py\"'; git diff"}, "high"),
    ("run_remote_command",
     {"command": "date '+echo x > backend/app/services/go100/live_trading/p.py' > /tmp/e.sh"}, "high"),
    ("run_remote_command",
     {"command": "git ls-remote 'ssh://h/echo x > backend/app/services/go100/live_trading/p.py'"}, "high"),
    # 그 밖의 쓰기 형태.
    ("run_remote_command", {"command": "echo x >/tmp/log 2>/dev/null; echo y > /srv/live_trading/p.py"}, "high"),
    ("run_remote_command", {"command": "echo x >& /srv/live_trading/p.py"}, "high"),
    ("run_remote_command", {"command": "echo x >| /srv/live_trading/p.py"}, "high"),
    ("run_remote_command", {"command": "echo x &> /srv/live_trading/p.py"}, "high"),
    ("run_remote_command", {"command": "echo x > >(tee /srv/live_trading/p.py)"}, "high"),
    ("run_remote_command", {"command": "cd /srv/live_trading && rm -f live_engine.py"}, "high"),
    ("run_remote_command", {"command": "cp /tmp/x /srv/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": "cat a | tee /srv/live_trading/params.py"}, "high"),
    ("run_remote_command", {"command": "git reset --hard HEAD~1  # live_trading"}, "high"),
    ("run_remote_command", {"command": "git checkout -- backend/app/services/go100/live_trading/live_engine.py"}, "high"),
    ("run_remote_command", {"command": 'psql -c "UPDATE card_trades SET qty=1" # live_trading'}, "high"),
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


def test_redirect_rules():
    w = guard._has_write_redirect
    assert w('grep -e "event_type <> " f.py') is False
    assert w("echo '>' | cat") is False
    assert w("cmd > /tmp/x.log 2>&1") is False
    assert w("cmd >> /tmp/x.log") is False
    assert w("cmd &> /tmp/x.log") is False
    assert w("(cmd > /tmp/x.log 2>&1 &)") is False
    assert w("cmd > backend/x.py") is True          # 상대경로는 /tmp 예외가 아니다
    assert w("cmd > /tmpfoo/x") is True
    assert w("cmd > /tmp/../etc/x") is True
    assert w("cmd > /root/kis-autotrade-v4/x") is True
    assert w("cmd > /tmp/live_trading/x") is True
    assert w('cmd > "/tmp/x.log"') is True          # 따옴표 대상은 보수적으로 쓰기
    assert w('echo "unterminated > /root/x') is True


def test_quotes_masked_only_for_readonly_commands():
    w = guard._has_write_redirect
    # 읽기 명령의 따옴표 인자는 코드가 아니다.
    assert w('git grep -n "awk x > y" -- a/') is False
    assert w("git log --grep 'a > b'") is False
    # 모르는 명령이 하나라도 있으면 따옴표 속도 코드로 본다.
    assert w('bash "-c" "echo x > /srv/p"') is True
    assert w("echo 'echo x > /srv/p' | sh") is True
    assert w("git push 'a > b'") is True
    assert w("sed -n 'p' > /srv/p") is True


def test_masking_requires_whitelisted_env_git_options_and_no_emitter_file_output():
    w = guard._has_write_redirect
    # 명시 목록의 환경변수 접두와 git 의 따옴표 검색어는 계속 가린다.
    assert w('LC_ALL=C TZ=Asia/Seoul grep -e "a <> b" f') is False
    assert w('LANG=C.UTF-8 git log --grep "a > b"') is False
    assert w('git log -2 --oneline --grep "a > b"') is False
    assert w('git grep -ni -e "a > b" -- a/') is False
    # grep/cut/head/tail 의 /tmp 출력은 그대로 허용(echo/printf/date 가 없을 때).
    assert w('grep -rn "a > b" d | head -5 > /tmp/out.txt 2>&1') is False
    assert w('echo "a > b" 2>&1 | cut -c1-5') is False
    assert w('echo "a > b" > /dev/null') is False
    # A: 목록 밖 환경변수가 하나라도 있으면 원문 판정.
    assert w("GIT_EXTERNAL_DIFF='sh -c \"echo x > /srv/p\"' git diff") is True
    assert w("FOO='a > b' grep x f") is True
    assert w("LC_ALL=C FOO='a > b' grep x f") is True
    # B: git 옵션 축약·묶음·분리, 따옴표가 낀 옵션/원격 인자.
    assert w("git grep -nO'sh -c \"echo x > /srv/p\"' -e foo") is True
    assert w("git grep --open-files='a > b' -e foo") is True
    assert w("git grep -O 'a > b' -e foo") is True
    assert w("git ls-remote --upload='a > b' .") is True
    assert w("git ls-remote 'a > b'") is True
    assert w("git log --format='a > b' -1") is True
    # C: echo/printf/date 출력이 파일로 가면(파이프 끝 포함) 원문 판정.
    assert w("printf 'echo x > /srv/p' > /tmp/e.sh") is True
    assert w("echo 'a > b' | cat > /tmp/e.sh") is True
    assert w("date '+a > b' > /tmp/e.sh") is True
    assert w("git rev-parse -- 'a > b' > /tmp/e.sh") is True


def test_mask_quoted_keeps_command_substitution():
    m = guard._mask_quoted
    assert m("grep -e \"a <> b\" f") == 'grep -e "QQQQQQ" f'
    nested = 'echo "$(printf "%s" x; rm /srv/p.py)"'
    assert "rm /srv/p.py" in m(nested)
    assert len(m(nested)) == len(nested)
    assert m("echo 'a") is None
    assert m('echo "$(x"') is None


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
