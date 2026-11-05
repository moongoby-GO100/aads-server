"""러너 셸 → Antigravity(agy) 실행 분기 (1/2: 셸·실행기·동기화 목록).

`antigravity:*` 후보가 Claude 검증에서 거절되거나, Anthropic 자격증명이 없다는 이유로 시도 자체가
건너뛰어지던 문제를 막는다. pipeline-runner.sh 와 .sh.local 두 사본이 같은 동작을 해야 한다.
relay 서버 쪽(app/services·claude_relay_server.py)은 2/2 작업 범위이므로 여기서는 표준 라이브러리
가짜 relay 로 실행기를 검증한다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER_FILES = [ROOT / "scripts" / "pipeline-runner.sh", ROOT / "scripts" / "pipeline-runner.sh.local"]
RUNNER_SH = RUNNER_FILES[0]
EXEC = ROOT / "scripts" / "runner_antigravity_exec.py"
CONTRACT = ROOT / "scripts" / "claude_model_contract.py"

AGY_ID = "gemini-3.8-flash-high"
SHELL_ONLY_IDS = {
    "gemini-4-argon-high", "gemini-4-argon-medium", "gemini-4-argon-low", "gpt-oss-120b-medium",
    "claude-opus-5-5-high", "claude-opus-5-5-medium", "claude-opus-5-5-low",
    "claude-sonnet-5-5-high", "claude-sonnet-5-5-medium", "claude-sonnet-5-5-low",
}


def _catalog_ids() -> set[str]:
    try:
        from app.services import model_selector
    except Exception:  # pragma: no cover - 서비스 의존성 없는 환경
        return set()
    return set(model_selector._ANTIGRAVITY_MODELS)


runner_param = pytest.mark.parametrize("runner", RUNNER_FILES, ids=lambda p: p.name)


def _between(text: str, start: str, end: str, *, begin: int = 0) -> str:
    i = text.index(start, begin)
    return text[i:text.index(end, i)]


# ── 두 사본 동일성 ─────────────────────────────────────────────────────────
def test_runner_sh_and_local_copy_are_identical():
    assert RUNNER_FILES[0].read_text() == RUNNER_FILES[1].read_text()


@runner_param
def test_runner_script_syntax_is_valid(runner):
    subprocess.run(["bash", "-n", str(runner)], check=True, timeout=60)


# ── normalize_runner_model ────────────────────────────────────────────────
def _normalize(runner: Path, model: str) -> str:
    script = runner.read_text()
    functions = _between(script, "normalize_runner_model() {", "normalize_claude_cli_model() {")
    out = subprocess.run(
        ["bash", "-c", 'CLAUDE_MODEL_CONTRACT="$1"\n' + functions + '\nnormalize_runner_model "$2"',
         "t", str(CONTRACT), model],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


@runner_param
@pytest.mark.parametrize("model", sorted(_catalog_ids() | SHELL_ONLY_IDS | {"antigravity", "antigravity-pro", "antigravity-flash"}))
def test_shell_routes_every_antigravity_id_to_antigravity(runner, model):
    assert _normalize(runner, model) == f"antigravity:{model}"


@runner_param
def test_shell_keeps_explicit_prefix_and_does_not_hijack_litellm_gemini(runner):
    assert _normalize(runner, f"antigravity:{AGY_ID}") == f"antigravity:{AGY_ID}"
    assert _normalize(runner, "gemini-3.8-flash") == "litellm:gemini-3.8-flash"
    assert _normalize(runner, "gemini-3.8-flash-latest") == "litellm:gemini-3.8-flash-latest"


@runner_param
def test_shell_leaves_claude_and_codex_behavior_unchanged(runner):
    assert _normalize(runner, "claude-sonnet-5-5") == "claude-sonnet-5-5"
    assert _normalize(runner, "codex:gpt-5.5") == "codex:gpt-5.5"
    assert _normalize(runner, "litellm:minimax-m2.7") == "litellm:minimax-m2.7"
    assert _normalize(runner, "") == "auto"


@runner_param
def test_shell_antigravity_id_set_covers_python_catalog(runner):
    script = runner.read_text()
    body = _between(script, "normalize_runner_model() {", "normalize_claude_cli_model() {")
    case_block = _between(body, "antigravity|antigravity-pro", ')\n            echo "antigravity:${model}"')
    shell_ids = {part.strip() for part in re.split(r"[|\\\n]", case_block) if part.strip()}
    assert shell_ids >= _catalog_ids()
    assert shell_ids >= SHELL_ONLY_IDS


@runner_param
def test_shell_dedupe_keeps_antigravity_candidate(runner):
    script = runner.read_text()
    functions = _between(script, "dedupe_model_cycle_for_attempt_caps() {", "normalize_claude_cli_model() {")
    out = subprocess.run(
        ["bash", "-c", 'CLAUDE_MODEL_CONTRACT="$1"\n' + functions +
         f'\nMODEL_CYCLE=("antigravity:{AGY_ID}")\ndedupe_model_cycle_for_attempt_caps\nprintf "%s\\n" "${{MODEL_CYCLE[@]}}"',
         "t", str(CONTRACT)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert out.stdout.split() == [f"antigravity:{AGY_ID}"]


# ── 고정 사이클과 분기 위치 ────────────────────────────────────────────────
@runner_param
def test_shell_pinned_antigravity_cycle_has_no_fallback_candidates(runner):
    script = runner.read_text()
    block = _between(script, 'elif [[ "$job_model" == antigravity:* ]]', "    else\n")
    assert 'MODEL_CYCLE=("$job_model")' in block
    assert "append_model_for_attempts" not in block and "get_db_model_cycle" not in block


@runner_param
def test_shell_branch_precedes_claude_validation_and_uses_job_worktree(runner):
    script = runner.read_text()
    marker = script.index("# Antigravity Runner 분기")
    branch = script.index('elif [[ "$current_model" == antigravity:* ]]; then', marker)
    assert branch < script.index('normalize_claude_cli_model "$current_model"', marker)
    assert branch < script.index('elif [[ "$current_model" == litellm:* ]]; then', marker)
    section = script[branch:script.index("# LiteLLM Runner 분기", branch)]
    assert '--workspace "$workdir"' in section and '> "$output_file" 2> "$err_file" &' in section
    assert "dangerously-skip-permissions" not in section
    assert "ANTHROPIC_API_KEY" not in section


@runner_param
def test_shell_token_switch_bypass_precedes_credential_lookup(runner):
    """AGY 후보는 슬롯 자격증명·lease·고정 토큰 조회와 TOKEN_SWITCH_SKIP/continue 보다 먼저 분기한다."""
    script = runner.read_text()
    start = script.index('        local slot_cred_file="" leased_token=""')
    block = script[start:script.index("        # H6: instruction 크기 제한", start)]
    bypass = block.index('if [[ "$current_model" == antigravity:* ]]; then')
    assert bypass < block.index("TOKEN_SWITCH_SKIP") < block.index("continue")
    lookup = block[:bypass]
    assert 'if [[ "$current_model" != antigravity:* ]]; then' in lookup
    assert lookup.index('!= antigravity:*') < lookup.index("slot_credentials_file") < lookup.index("leased_slot_token")


# ── Anthropic 자격증명 가드 ────────────────────────────────────────────────
def _auth_guard(runner: Path, model_cycle: list[str], *, token_1="", token_2="", slot_auth=""):
    """run_job 의 실제 자격증명 가드 블록을 추출해 그대로 실행한다. (종료코드, 호출 기록)"""
    script = runner.read_text()
    start = script.index('    local TOKEN_1="${ANTHROPIC_AUTH_TOKEN:-}"')
    block = script[start:script.index("    local total_attempts=${#MODEL_CYCLE[@]}", start)]
    helper = _between(script, "model_cycle_requires_anthropic_auth() {", "# 슬롯 자격증명 경로를 stdout")
    harness = (
        helper
        + "\nCALLS=()\n"
        + "log() { CALLS+=(\"log:$*\"); }\n"
        + "db_update() { CALLS+=(\"db_update\"); }\n"
        + "record_runner_event() { CALLS+=(\"event:$4\"); }\n"
        + "post_to_chat() { CALLS+=(\"chat\"); }\n"
        + "slot_credentials_file() { return 1; }\n"
        + "leased_slot_token() { return 1; }\n"
        + "guard() {\n"
        + '    local job_id=j1 session_id=s1 job_model=m job_size=M\n'
        + block.replace(
            '_slot_auth_available="$(slot_credentials_file 1',
            '_slot_auth_available="$SLOT_AUTH"; : "$(slot_credentials_file 1',
        )
        + "\n    return 0\n}\n"
        + 'MODEL_CYCLE=("$@")\nguard; rc=$?\nprintf "rc=%s\\n" "$rc"\nprintf "%s\\n" "${CALLS[@]}"\n'
    )
    env = {
        "PATH": os.environ["PATH"], "SLOT_AUTH": slot_auth,
        "ANTHROPIC_AUTH_TOKEN": token_1, "ANTHROPIC_AUTH_TOKEN_2": token_2,
    }
    out = subprocess.run(["bash", "-c", harness, "t", *model_cycle],
                         check=True, capture_output=True, text=True, timeout=30, env=env)
    lines = out.stdout.splitlines()
    return int(lines[0].split("=")[1]), lines[1:]


@runner_param
def test_auth_guard_still_blocks_claude_cycle_without_any_anthropic_credential(runner):
    rc, calls = _auth_guard(runner, ["claude-sonnet-5-5", "claude-sonnet-5-5"])
    assert rc == 1 and "db_update" in calls and "event:token_missing" in calls


@runner_param
def test_auth_guard_still_blocks_mixed_cycle_when_any_candidate_needs_anthropic(runner):
    rc, calls = _auth_guard(runner, [f"antigravity:{AGY_ID}", "claude-sonnet-5-5"])
    assert rc == 1 and "db_update" in calls


@runner_param
def test_auth_guard_lets_pinned_antigravity_run_without_anthropic_credential(runner):
    rc, calls = _auth_guard(runner, [f"antigravity:{AGY_ID}"])
    assert rc == 0
    assert "db_update" not in calls and "chat" not in calls and "event:token_missing" not in calls
    assert any(c.startswith("log:  ANTIGRAVITY_NO_ANTHROPIC_AUTH") for c in calls)


@runner_param
def test_auth_guard_unchanged_for_claude_cycle_with_a_token(runner):
    rc, calls = _auth_guard(runner, ["claude-sonnet-5-5"], token_1="t1")
    assert rc == 0 and "db_update" not in calls
    assert not any("ANTIGRAVITY_NO_ANTHROPIC_AUTH" in c for c in calls)


@runner_param
def test_shell_helper_treats_empty_cycle_as_requiring_anthropic_auth(runner):
    script = runner.read_text()
    helper = _between(script, "model_cycle_requires_anthropic_auth() {", "# 슬롯 자격증명 경로를 stdout")

    def rc(*args):
        return subprocess.run(["bash", "-c", helper + '\nmodel_cycle_requires_anthropic_auth "$@"', "t", *args],
                              timeout=30).returncode

    assert rc() == 0 and rc("claude-sonnet-5-5") == 0 and rc("antigravity:x", "codex:gpt-5.5") == 0
    assert rc("antigravity:x") == 1 and rc("antigravity:x", "antigravity:y") == 1


# ── 가짜 relay (표준 라이브러리) ───────────────────────────────────────────
class _FakeRelay:
    def __init__(self, events: list[dict] | None = None, status: int = 200, raw_lines: list[str] | None = None):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({
                    "path": self.path, "body": body,
                    "secret": self.headers.get("X-Claude-Relay-Secret"),
                })
                self.send_response(status)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                lines = raw_lines if raw_lines is not None else [json.dumps(e, ensure_ascii=False) for e in (events or [])]
                for line in lines:
                    self.wfile.write((line + "\n").encode("utf-8"))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


_OK_EVENTS = [
    {"type": "assistant", "subtype": "text", "text": "부분"},
    {"type": "result", "result": "작업 완료: target.py 수정"},
]


def _run_exec(relay_url: str, tmp_path: Path, *, instruction="수정하라", model=AGY_ID, workspace="/tmp/aads-wt-runner-x"):
    instr = tmp_path / "instr.txt"
    instr.write_text(instruction)
    return subprocess.run(
        [sys.executable, str(EXEC), "--model", model, "--workspace", workspace,
         "--instruction-file", str(instr), "--session-id", "runner-x", "--relay-url", relay_url, "--timeout", "10"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "CLAUDE_RELAY_SHARED_SECRET": "test-secret"}, stdin=subprocess.DEVNULL,
    )


def test_helper_posts_workspace_and_collects_result_on_stdout(tmp_path):
    with _FakeRelay(_OK_EVENTS) as relay:
        proc = _run_exec(relay.url, tmp_path, instruction="target.py 의 VALUE 를 2로")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "작업 완료: target.py 수정\n"
    (request,) = relay.requests
    assert request["path"] == "/antigravity-stream" and request["secret"] == "test-secret"
    assert request["body"]["model"] == AGY_ID
    assert request["body"]["workspace"] == "/tmp/aads-wt-runner-x"
    assert request["body"]["messages_text"] == "target.py 의 VALUE 를 2로"


def test_helper_falls_back_to_streamed_deltas_when_result_text_is_empty(tmp_path):
    events = [{"type": "assistant", "subtype": "text", "text": "델타"}, {"type": "result", "result": ""}]
    with _FakeRelay(events) as relay:
        proc = _run_exec(relay.url, tmp_path)
    assert proc.returncode == 0 and proc.stdout == "델타\n"


def test_helper_error_event_is_failure_with_failed_marker(tmp_path):
    with _FakeRelay([{"type": "error", "content": "agy exited with code 4"}]) as relay:
        proc = _run_exec(relay.url, tmp_path)
    assert proc.returncode == 1 and proc.stdout == ""
    assert proc.stderr.startswith("FAILED:") and "code 4" in proc.stderr


def test_helper_stream_without_result_is_failure(tmp_path):
    with _FakeRelay([{"type": "assistant", "subtype": "text", "text": "x"}]) as relay:
        proc = _run_exec(relay.url, tmp_path)
    assert proc.returncode == 1 and "without result" in proc.stderr and proc.stdout == ""


def test_helper_http_error_is_failure(tmp_path):
    with _FakeRelay(status=400, raw_lines=['{"error":"invalid_antigravity_workspace"}']) as relay:
        proc = _run_exec(relay.url, tmp_path)
    assert proc.returncode == 1 and "400" in proc.stderr and "invalid_antigravity_workspace" in proc.stderr


def test_helper_connection_refused_is_failure_not_success(tmp_path):
    proc = _run_exec("http://127.0.0.1:9", tmp_path)
    assert proc.returncode == 1 and proc.stderr.startswith("FAILED:") and proc.stdout == ""


def test_helper_bad_arguments_exit_2(tmp_path):
    empty = tmp_path / "e.txt"
    empty.write_text("  \n")
    base = [sys.executable, str(EXEC), "--model", AGY_ID, "--workspace", "/tmp/aads-wt-x"]
    assert subprocess.run(base + ["--instruction-file", str(empty)], capture_output=True, timeout=30).returncode == 2
    missing = tmp_path / "missing.txt"
    assert subprocess.run(base + ["--instruction-file", str(missing)], capture_output=True, timeout=30).returncode == 2


def test_helper_never_touches_anthropic_keys_or_skip_permissions():
    source = EXEC.read_text()
    assert "ANTHROPIC" not in source and "dangerously-skip-permissions" not in source


# ── 시도 루프 통합: Anthropic 자격증명이 전혀 없어도 AGY 가 실제로 실행된다 ──────────
_ATTEMPT_LOOP = r'''
log() { echo "log:$*" >&2; }
slot_credentials_file() { return 1; }
leased_slot_token() { return 1; }
retry_execution_charge_or_abort() { echo "charge:$6" >&2; return 0; }
OTHER_RAN=()

run() {
    local attempt=0 exit_code=0 TOKEN_1="${T1:-}" TOKEN_2="${T2:-}" EXECUTED=0 job_id=runner-x session_id=s1 project=AADS parallel_group=''
    local instruction="$INSTRUCTION" safe_instruction="$INSTRUCTION" workdir="$WORKDIR"
    local ARTIFACT_DIR="$ART" MAX_RUNTIME=60 _worker_nice_cmd=() runner_kind=claude_cli
    local TOKEN_CYCLE=(1 2 1 2) MODEL_CYCLE=("$@") total_attempts=$#
    local output_file="$ART/out.txt" err_file="$ART/err.txt" effective_model claude_pid
    while [[ $attempt -lt $total_attempts ]]; do
        exit_code=0
        local current_model="${MODEL_CYCLE[$attempt]}"
        local effective_model="$current_model" token_slot="${TOKEN_CYCLE[$attempt]}"
@@TOKEN_SWITCH@@
        if false; then
            :
@@AGY_BRANCH@@
        else
            runner_kind=claude_cli
            effective_model=unverified
            OTHER_RAN+=("$current_model")
            echo "other ran" > "$output_file"
            ( exit "${OTHER_EXIT:-0}" ) &
            claude_pid=$!
        fi
        EXECUTED=$((EXECUTED + 1))
        wait "$claude_pid" || exit_code=$?
@@AGY_CLEANUP@@
@@AGY_CONTENT_GUARD@@
        if [[ $exit_code -eq 0 ]]; then
            echo "ACTUAL_MODEL:$effective_model"
            break
        fi
        attempt=$((attempt + 1))
    done
    echo "FINAL_EXIT:$exit_code"
    echo "EXECUTED:$EXECUTED"
    echo "OTHER_RAN:${OTHER_RAN[*]:-}"
    return $exit_code
}
run "$@"
'''


def _loop_harness(runner: Path) -> str:
    script = runner.read_text()
    switch_start = script.index('        local slot_cred_file="" leased_token=""')
    token_switch = script[switch_start:script.index("        # H6: instruction 크기 제한", switch_start)]
    marker = script.index("# Antigravity Runner 분기")
    branch_start = script.index('        elif [[ "$current_model" == antigravity:* ]]; then', marker)
    agy_branch = script[branch_start:script.index("        # LiteLLM Runner 분기", branch_start)]
    cleanup = _between(script, "        # Antigravity instruction temp file 정리", "        # LiteLLM instruction temp file 정리")
    guard = _between(script, "        # 방어: Antigravity 출력에 FAILED:/ERROR:", "        local attempt_finished_ms attempt_duration_ms")
    return (_ATTEMPT_LOOP
            .replace("@@TOKEN_SWITCH@@", token_switch)
            .replace("@@AGY_BRANCH@@", agy_branch)
            .replace("@@AGY_CLEANUP@@", cleanup)
            .replace("@@AGY_CONTENT_GUARD@@", guard))


def _run_attempt_loop(runner: Path, tmp_path: Path, relay_url: str, cycle: list[str], *, other_exit=0, workdir="/tmp/aads-wt-runner-x", tokens=False):
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "ART": str(art), "WORKDIR": workdir, "INSTRUCTION": "target.py 를 고쳐라",
        "RUNNER_ANTIGRAVITY_EXEC": str(EXEC),
        "CLAUDE_RELAY_URL": relay_url, "CLAUDE_RELAY_SHARED_SECRET": "test-secret",
        "OTHER_EXIT": str(other_exit),
    }
    if tokens:
        env.update({"T1": "tok-1", "T2": "tok-2"})
    assert not any(k.startswith("ANTHROPIC") or k == "CLAUDE_CODE_OAUTH_TOKEN" for k in env)
    assert tokens or not (env.get("T1") or env.get("T2"))
    harness = 'RUNNER_ANTIGRAVITY_EXEC="$RUNNER_ANTIGRAVITY_EXEC"\n' + _loop_harness(runner)
    proc = subprocess.run(["bash", "-c", harness, "t", *cycle], capture_output=True, text=True, timeout=120, env=env)
    return proc, art


@runner_param
def test_pinned_antigravity_attempt_runs_exec_without_any_anthropic_credential(runner, tmp_path):
    """리뷰 P1 회귀: 자격증명이 전혀 없어도 runner_antigravity_exec.py 가 실제로 호출되고 AGY 성공으로 기록된다."""
    with _FakeRelay(_OK_EVENTS) as relay:
        proc, art = _run_attempt_loop(runner, tmp_path, relay.url, [f"antigravity:{AGY_ID}"])
    assert proc.returncode == 0, proc.stderr
    assert len(relay.requests) == 1
    assert relay.requests[0]["body"]["workspace"] == "/tmp/aads-wt-runner-x"
    assert relay.requests[0]["body"]["messages_text"] == "target.py 를 고쳐라"
    assert (art / "out.txt").read_text() == "작업 완료: target.py 수정\n"
    assert f"ACTUAL_MODEL:antigravity:{AGY_ID}" in proc.stdout and "FINAL_EXIT:0" in proc.stdout
    assert "EXECUTED:1" in proc.stdout
    assert "OTHER_RAN:\n" in proc.stdout
    assert "TOKEN_SWITCH_SKIP" not in proc.stderr and "TOKEN_SWITCH_NOT_NEEDED" in proc.stderr
    assert not list(art.glob(".agy_instr_*"))


@runner_param
def test_every_antigravity_attempt_in_cycle_calls_exec(runner, tmp_path):
    cycle = [f"antigravity:{AGY_ID}"] * 2
    with _FakeRelay([{"type": "error", "content": "agy down"}]) as relay:
        proc, _ = _run_attempt_loop(runner, tmp_path, relay.url, cycle)
    assert proc.returncode == 1
    assert len(relay.requests) == 2
    assert "TOKEN_SWITCH_SKIP" not in proc.stderr and "OTHER_RAN:\n" in proc.stdout


@runner_param
def test_pinned_antigravity_failure_does_not_fall_back_or_record_success(runner, tmp_path):
    with _FakeRelay([{"type": "error", "content": "agy exited with code 4"}]) as relay:
        proc, _ = _run_attempt_loop(runner, tmp_path, relay.url, [f"antigravity:{AGY_ID}"])
    assert proc.returncode != 0 and len(relay.requests) == 1
    assert "ACTUAL_MODEL" not in proc.stdout and "EXECUTED:1" in proc.stdout
    assert "OTHER_RAN:\n" in proc.stdout


@runner_param
def test_antigravity_failure_marker_in_output_forces_failure(runner, tmp_path):
    with _FakeRelay([{"type": "result", "result": "FAILED: 작업공간 쓰기 실패"}]) as relay:
        proc, _ = _run_attempt_loop(runner, tmp_path, relay.url, [f"antigravity:{AGY_ID}"])
    assert proc.returncode != 0 and "ANTIGRAVITY_CONTENT_FAIL" in proc.stderr
    assert "ACTUAL_MODEL" not in proc.stdout


@runner_param
def test_fallback_success_after_antigravity_failure_is_not_recorded_as_antigravity(runner, tmp_path):
    """혼합 사이클: AGY 실패 뒤 다른 모델이 성공하면 actual_model 은 그 모델이고 antigravity 가 아니다."""
    cycle = [f"antigravity:{AGY_ID}", "claude-sonnet-5-5"]
    with _FakeRelay([{"type": "error", "content": "agy down"}]) as relay:
        proc, _ = _run_attempt_loop(runner, tmp_path, relay.url, cycle, tokens=True)
    assert len(relay.requests) == 1
    assert "OTHER_RAN:claude-sonnet-5-5" in proc.stdout
    assert "ACTUAL_MODEL:unverified" in proc.stdout
    assert "ACTUAL_MODEL:antigravity" not in proc.stdout


@runner_param
def test_token_switch_still_skips_claude_attempt_without_any_credential(runner, tmp_path):
    with _FakeRelay(_OK_EVENTS) as relay:
        proc, _ = _run_attempt_loop(runner, tmp_path, relay.url, ["claude-sonnet-5-5"])
    assert "TOKEN_SWITCH_SKIP" in proc.stderr and "EXECUTED:0" in proc.stdout
    assert "ACTUAL_MODEL" not in proc.stdout and "OTHER_RAN:\n" in proc.stdout and not relay.requests


@runner_param
def test_token_switch_skips_only_non_antigravity_attempts_in_mixed_cycle(runner, tmp_path):
    cycle = ["claude-sonnet-5-5", f"antigravity:{AGY_ID}"]
    with _FakeRelay(_OK_EVENTS) as relay:
        proc, _ = _run_attempt_loop(runner, tmp_path, relay.url, cycle)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("TOKEN_SWITCH_SKIP") == 1 and "EXECUTED:1" in proc.stdout
    assert len(relay.requests) == 1 and f"ACTUAL_MODEL:antigravity:{AGY_ID}" in proc.stdout
