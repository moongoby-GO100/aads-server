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
    # 2026-09-15 범위 확장으로 UPDATE 가 `... a FROM eff` 형태가 되면서
    # 컬럼 참조에 별칭이 붙었다. 보존한다는 사실 자체는 그대로다.
    assert "'target', COALESCE(a.approval_scope->>'target', '')" in src


def test_goal_policy_defaults_critical_off():
    """코드 수정을 미리 허락하는 것과 주문을 미리 허락하는 것은 다른 얘기다."""
    import inspect

    body = inspect.getsource(guard.goal_policy_allows)
    assert "auto_approve_critical" in body
    assert "auto_approve_high" in body
    assert "false" in body  # COALESCE(..., false) — 기본은 꺼짐


def test_goal_policy_is_bound_to_the_goal_project():
    """GO100 목표에 켠 설정이 AADS 파일까지 열면 안 된다.

    2026-09-15 실측 — 자동 승인이 켜진 목표 3건은 전부 GO100 인데 각각
    채팅 세션 8~10 개가 묶여 있었다. 조회가 프로젝트를 보지 않아 그
    세션의 AADS 변경까지 같은 설정으로 통과했다.
    """
    import inspect

    body = inspect.getsource(guard.goal_policy_allows)
    assert "UPPER(COALESCE(g.project, '')) = $3" in body, "목표 정책이 프로젝트를 보지 않는다"
    # 대상 프로젝트를 모르는 호출은 통과시키지 않는다.
    assert "$3 <> ''" in body


def test_session_scope_ignores_target():
    """이 대화 동안 승인은 대상이 달라도 이어져야 한다.

    2026-09-15 실측 — 미션 범위가 대상 지문까지 맞아야 해서 파일을 옮길
    때마다 새 카드가 떴다(12시간 카드 89장, 실사용 18회). 세션 범위는
    그 대상 조건을 빼는 것이 존재 이유다.
    """
    import inspect

    body = inspect.getsource(guard.is_approved)
    assert "approval_scope->>'scope' = 'session'" in body
    session_clause = body.split("= 'session'", 1)[1].split(")", 1)[0]
    assert "target" not in session_clause, "세션 범위가 대상을 다시 묻고 있다"


def test_project_scope_requires_a_known_project():
    """프로젝트 범위는 세션을 넘는다. 대상 프로젝트를 모르면 열지 않는다."""
    import inspect

    body = inspect.getsource(guard.is_approved)
    assert "approval_scope->>'scope' = 'project'" in body
    assert "UPPER(COALESCE(approval_scope->>'project', '')) = $7" in body
    assert "$7 <> ''" in body


def test_wide_scopes_are_closed_for_critical():
    """주문·자금은 대화·프로젝트 범위로 통과하지 않는다.

    화면에서도 그 버튼을 감추지만, 화면은 바뀐다. 조회에서 한 번 더 막는다.
    """
    import inspect

    body = inspect.getsource(guard.is_approved)
    assert 'wide_ok = (risk_level or "") != "critical"' in body
    # 넓은 두 절은 전부 그 스위치 뒤에 있어야 한다.
    for scope in ("'session'", "'project'"):
        clause = body.split("= " + scope, 1)[0].rsplit("OR", 1)[1]
        assert "$6" in clause, f"{scope} 범위가 critical 스위치를 거치지 않는다"


def test_check_passes_risk_level_to_lookup():
    """risk_level 을 넘기지 않으면 critical 차단이 조용히 꺼진다."""
    import inspect

    body = inspect.getsource(guard.check)
    assert "is_approved(tool_name, tool_input, session_id, risk_level)" in body


def test_request_records_project_for_later_project_scope():
    """승인 시점에는 tool_input 이 없다. 요청 시점에 박아 두어야 한다."""
    import inspect

    body = inspect.getsource(guard.request_approval)
    assert '"project": str(tool_input.get("project") or "").strip().upper()' in body


def test_decide_endpoint_accepts_the_new_scopes():
    """화면이 보내도 서버가 받지 않으면 버튼은 장식이다."""
    api = REPO / "app" / "api" / "project_docs.py"
    src = api.read_text()
    assert "^(single|mission|session|project)$" in src
    # critical 은 거절이 아니라 미션으로 낮춘다 — 거절하면 다시 눌러야 한다.
    assert "THEN 'mission' ELSE $5 END AS scope" in src
    assert "'project', COALESCE(a.approval_scope->>'project', '')" in src
