"""승인 키는 프로세스가 달라도 같아야 한다.

2026-09-15 대표님 전달 — "승인 카드는 뜨지만 실행되지 않는 상황이 반복된다".

원인은 `work_key` 가 파이썬 내장 `hash()` 로 만들어졌다는 것이다. 문자열
해시는 프로세스마다 무작위로 바뀐다(PYTHONHASHSEED). API 워커가 여러 개라,
요청을 남긴 워커와 승인을 확인하는 워커가 다르면 **같은 명령인데 키가
달라졌다.** 그래서

  · 승인을 눌러도 다음 호출에서 그 승인을 못 찾고 다시 막혔다.
  · 같은 명령이 중복 요청으로 계속 쌓였다(대기 목록에 같은 SQL 이 4건).
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO / "app" / "services" / "live_trading_guard.py"

_spec = importlib.util.spec_from_file_location("guard_under_test", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _key_in_fresh_process(session: str, tool: str, summary: str) -> str:
    code = (
        "import importlib.util;"
        f"s=importlib.util.spec_from_file_location('g', {str(GUARD)!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        f"print(m._work_key({session!r}, {tool!r}, {summary!r}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-400:]
    return out.stdout.strip()


def test_work_key_is_identical_across_processes():
    """워커가 달라도 같은 키가 나와야 승인이 맞물린다."""
    args = ("5090a247-47f7-4a05", "patch_remote_file", "live_engine.py 진입 조건 수정")
    mine = guard._work_key(*args)
    others = {_key_in_fresh_process(*args) for _ in range(3)}
    assert others == {mine}, (mine, others)


def test_work_key_still_separates_different_work():
    a = guard._work_key("sess-a", "patch_remote_file", "파일1")
    b = guard._work_key("sess-a", "patch_remote_file", "파일2")
    c = guard._work_key("sess-b", "patch_remote_file", "파일1")
    d = guard._work_key("sess-a", "run_remote_command", "파일1")
    assert len({a, b, c, d}) == 4


def test_builtin_hash_is_gone():
    """내장 hash() 가 다시 들어오면 같은 증상이 재발한다."""
    src = GUARD.read_text()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "hash(summary)" not in body


def test_approval_lookup_matches_by_scope_not_only_work_key():
    """미션·골 승인이 다음 요청에도 이어져야 한다.

    옛 조회는 work_key 하나만 봤다. 그 키에 명령 본문이 섞여 있어, 명령이
    한 글자만 달라도 새 승인을 요구했다.
    """
    import inspect

    body = inspect.getsource(guard.is_approved)
    assert "'scope' = 'mission'" in body
    assert "'scope' = 'goal'" in body
    # 횟수를 세지 않으면 상한이 장식이 된다.
    assert "used" in body


def test_mission_scope_is_bound_to_a_target():
    """미션 승인은 **같은 대상**일 때만 이어져야 한다.

    2026-09-15 실측 — AADS `live_trading_guard.py` 1건에 대한 "이 미션 동안"
    승인이 같은 세션의 `patch_remote_file` 전부를 통과시켰다. 미션 범위가
    (세션 + 도구) 로만 정의돼 GO100 `live_engine.py` 수정까지 열렸다.
    """
    import inspect

    body = inspect.getsource(guard.is_approved)
    assert "'scope' = 'mission'" in body
    assert "approval_scope->>'target'" in body, "미션 범위가 대상을 보지 않는다"


def test_target_key_separates_different_files():
    aads = guard._target_key(
        {"project": "AADS", "file_path": "app/services/live_trading_guard.py"})
    go100 = guard._target_key(
        {"project": "GO100",
         "file_path": "backend/app/services/go100/live_trading/live_engine.py"})
    assert aads != go100


def test_target_key_survives_command_wording_changes():
    """같은 파일을 다른 명령으로 고쳐도 승인 하나로 이어져야 한다.

    여기서 명령 본문까지 보면 한 글자 차이로 다시 묻게 되고, 그러면
    대표님이 화면을 계속 쳐다봐야 했던 예전 상태로 되돌아간다.
    """
    path = "/root/kis/backend/app/services/go100/live_trading/live_engine.py"
    sed = guard._target_key({"project": "GO100", "command": f"sed -i s/x/y/ {path}"})
    tee = guard._target_key({"project": "GO100", "command": f"tee -a {path}"})
    assert sed == tee


def test_target_key_is_never_empty():
    """빈 지문끼리 맞으면 아무 대상이나 통과한다."""
    for payload in ({}, {"project": ""}, {"command": ""}, {"file_path": ""}):
        assert guard._target_key(payload)


def test_target_key_normalizes_relative_prefix():
    plain = guard._target_key({"project": "AADS", "file_path": "app/services/x.py"})
    dotted = guard._target_key({"project": "AADS", "file_path": "./app/services/x.py"})
    assert plain == dotted


def test_decide_endpoint_preserves_target():
    """승인 시점에 대상 지문을 덮어 없애면 범위가 다시 벌어진다."""
    api = REPO / "app" / "api" / "project_docs.py"
    src = api.read_text()
    assert "'target', COALESCE(approval_scope->>'target', '')" in src


def test_goal_policy_defaults_critical_off():
    """코드 수정을 미리 허락하는 것과 주문을 미리 허락하는 것은 다른 얘기다."""
    import inspect

    body = inspect.getsource(guard.goal_policy_allows)
    assert "auto_approve_critical" in body
    assert "auto_approve_high" in body
    assert "false" in body  # COALESCE(..., false) — 기본은 꺼짐
