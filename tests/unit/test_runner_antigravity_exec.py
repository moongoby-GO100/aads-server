"""runner_antigravity_exec.py — 러너 작업 폴더 모드 영수증·타임아웃 (AADS-AGY-TOOLS-AND-PRO-ALIAS-20261106).

relay 는 workspace 가 오면 워크트리에서 agy 를 파일 수정·셸 허용 모드로 돌리고(최대 3600초),
도구 호출을 Claude 형식 tool_use/tool_result 이벤트로 내보낸다. 실행기는 그 결과를 성공으로
간주하지 않고 실제 모델·도구 호출 수를 AGY_RECEIPT 로 남겨 러너의 diff·테스트 판정을 돕는다
(오류 사전 runner.antigravity-mapping-dispatch-gap 예방 조항).
기본 흐름(요청 본문·stdout·종료코드)은 test_runner_antigravity_execution.py 가 다룬다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.unit.test_runner_antigravity_execution import _FakeRelay

ROOT = Path(__file__).resolve().parents[2]
EXEC = ROOT / "scripts" / "runner_antigravity_exec.py"
AGY_ID = "gemini-4-argon-high"


def _run(relay_url: str, tmp_path: Path, *extra: str):
    instr = tmp_path / "instr.txt"
    instr.write_text("app.py 를 고쳐라")
    return subprocess.run(
        [sys.executable, str(EXEC), "--model", AGY_ID, "--workspace", "/tmp/aads-wt-runner-y",
         "--instruction-file", str(instr), "--session-id", "runner-y", "--relay-url", relay_url, *extra],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "CLAUDE_RELAY_SHARED_SECRET": "test-secret"}, stdin=subprocess.DEVNULL,
    )


def _tool_use(tool_id: str, name: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": {}}]}}


def _tool_result(tool_id: str, is_error: bool) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "x", "is_error": is_error}]}}


def test_receipt_counts_tool_calls_and_reports_relay_model_and_mode(tmp_path):
    events = [
        _tool_use("agy_1_1", "write_file"), _tool_result("agy_1_1", False),
        _tool_use("agy_1_2", "run_command"), _tool_result("agy_1_2", True),
        {"type": "result", "result": "", "agy_model": AGY_ID, "mode": "runner", "tool_calls": 2, "tool_errors": 1},
    ]
    with _FakeRelay(events) as relay:
        proc = _run(relay.url, tmp_path, "--timeout", "10")
    assert proc.returncode == 0, proc.stderr
    assert f"AGY_RECEIPT model={AGY_ID} agy_model={AGY_ID} mode=runner tool_calls=2 tool_errors=1" in proc.stderr
    # 도구 이벤트는 본문(stdout)에 섞이지 않는다.
    assert proc.stdout == "\n"


def test_receipt_marks_unknown_mode_when_relay_is_old(tmp_path):
    with _FakeRelay([{"type": "result", "result": "완료"}]) as relay:
        proc = _run(relay.url, tmp_path, "--timeout", "10")
    assert proc.returncode == 0 and proc.stdout == "완료\n"
    assert "agy_model=? mode=? tool_calls=0" in proc.stderr


def test_relay_warning_events_are_surfaced_on_stderr_not_stdout(tmp_path):
    events = [
        {"type": "system", "subtype": "warning", "code": "antigravity_denied_actions", "content": "Antigravity 권한 거부: git push"},
        {"type": "result", "result": "끝", "mode": "runner"},
    ]
    with _FakeRelay(events) as relay:
        proc = _run(relay.url, tmp_path, "--timeout", "10")
    assert proc.returncode == 0 and proc.stdout == "끝\n"
    assert "AGY_WARNING: Antigravity 권한 거부: git push" in proc.stderr


def test_default_timeout_outlasts_relay_runner_timeout():
    import importlib.util

    spec = importlib.util.spec_from_file_location("runner_antigravity_exec_t", EXEC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = {}

    def fake_run(model, workspace, instruction, relay_url, session_id, timeout):
        seen["timeout"] = timeout
        return 0

    instr = Path(os.environ.get("TMPDIR", "/tmp")) / ("agy-exec-timeout-%d.txt" % os.getpid())
    instr.write_text("x")
    try:
        module.run = fake_run
        assert module.main(["--model", AGY_ID, "--workspace", "/tmp/aads-wt-z", "--instruction-file", str(instr)]) == 0
    finally:
        instr.unlink()
    # relay 러너 모드 하드 타임아웃(AGY_RELAY_RUNNER_HARD_TIMEOUT_SEC 기본 3600)보다 길어야 relay 의 오류 이벤트를 받는다.
    assert seen["timeout"] > 3600


def test_runner_sends_workspace_so_relay_uses_worktree_mode(tmp_path):
    with _FakeRelay([{"type": "result", "result": "ok"}]) as relay:
        _run(relay.url, tmp_path, "--timeout", "10")
    body = relay.requests[0]["body"]
    assert body["workspace"] == "/tmp/aads-wt-runner-y" and body["session_id"] == "runner-y"
    assert json.dumps(body, ensure_ascii=False).count("allow_tools") == 0
