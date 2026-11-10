#!/usr/bin/env python3
"""AGY runner executor: --local-cli runs tools in this host's worktree.

--preflight-local is read-only admission, before retry budget is charged.
The legacy HTTP transport remains available for compatible callers.

pipeline-runner.sh 가 antigravity:* 후보를 만나면 codex/litellm 과 같은 자리에서 이 스크립트를 띄운다.
결과 텍스트는 stdout, 사유는 stderr, 종료코드 0=성공 / 1=실패 / 2=인자 오류.
다른 제공자로 바꾸지 않는다 — 폴백 여부는 호출한 러너가 정한다.

relay 는 workspace 를 받으면 그 워크트리를 cwd 로 agy 를 파일 수정·셸 허용 모드로 돌린다.
HTTP 200·result 이벤트만으로 작업 성공을 판정하지 않는다 — 실제 변경은 러너가 diff·테스트로 본다.
그 판정을 돕도록 실제 실행 모델·도구 호출 수를 stderr 에 AGY_RECEIPT 한 줄로 남긴다.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
from datetime import datetime, timezone
import sys
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_RELAY_URL = "http://127.0.0.1:8199"
_SECRET_FILE_CANDIDATES = (
    Path(__file__).resolve().parent / "claude_relay_secret.txt",
    Path("/root/aads/aads-server/scripts/claude_relay_secret.txt"),
)


def _load_secret() -> str:
    secret = (os.getenv("CLAUDE_RELAY_SHARED_SECRET") or "").strip()
    if secret:
        return secret
    explicit = os.getenv("CLAUDE_RELAY_SHARED_SECRET_FILE")
    for path in ((Path(explicit),) if explicit else ()) + _SECRET_FILE_CANDIDATES:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def _fail(message: str, code: int = 1) -> int:
    print(f"FAILED: {message}", file=sys.stderr)
    return code


def run(model: str, workspace: str, instruction: str, relay_url: str, session_id: str, timeout: float) -> int:
    body = json.dumps({
        "model": model,
        "system_prompt": "",
        "messages_text": instruction,
        "session_id": session_id,
        "workspace": workspace,
    }).encode("utf-8")
    request = urllib.request.Request(
        relay_url.rstrip("/") + "/antigravity-stream", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Claude-Relay-Secret": _load_secret()},
    )
    deltas: list[str] = []
    result_text: str | None = None
    result_event: dict = {}
    tool_uses = 0
    tool_errors = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "error":
                    return _fail(str(event.get("content") or "Antigravity error")[:500])
                if kind == "assistant" and event.get("subtype") == "text":
                    deltas.append(str(event.get("text") or ""))
                elif kind in ("assistant", "user"):
                    for block in (event.get("message") or {}).get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            tool_uses += 1
                        elif block.get("type") == "tool_result" and block.get("is_error"):
                            tool_errors += 1
                elif kind == "system" and event.get("subtype") == "warning":
                    print(f"AGY_WARNING: {str(event.get('content') or '')[:300]}", file=sys.stderr)
                elif kind == "result":
                    result_text = str(event.get("result") or "")
                    result_event = event
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return _fail(f"relay HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _fail(f"relay connect/read failed: {exc}")
    if result_text is None:
        return _fail("relay stream ended without result event")
    print(
        "AGY_RECEIPT model={} agy_model={} mode={} tool_calls={} tool_errors={}".format(
            model, result_event.get("agy_model") or "?", result_event.get("mode") or "?", tool_uses, tool_errors),
        file=sys.stderr,
    )
    text = result_text or "".join(deltas)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0



# Remote runners must execute tools in their OWN worktree. A tunnel to the
# central relay is suitable for chat, but not for remote filesystem execution.
_LOCAL_ALIASES = {"antigravity": "gemini-3.8-flash-medium",
                  "antigravity-flash": "gemini-3.8-flash-medium",
                  "antigravity-pro": "gemini-4-argon-high"}
_LOCAL_DENY = (
    "git add", "git commit", "git push", "git worktree", "git reset", "git checkout", "git stash",
    "docker build", "docker compose", "docker restart", "docker-compose",
    "npm run build", "npm start", "next build", "supervisorctl", "systemctl", "service", "kill", "pkill",
)


def local_preflight(workspace: str = "") -> tuple[bool, str]:
    binary = shutil.which(os.getenv("AGY_BIN", "agy"))
    if not binary:
        return False, "agy_binary_missing"
    credential = Path(os.getenv("ANTIGRAVITY_HOME_ROOT", str(Path.home() / ".gemini"))) / "antigravity-cli/antigravity-oauth-token"
    try:
        if credential.stat().st_size > 65536:
            return False, "agy_credential_invalid"
        token = json.loads(credential.read_text()).get("token") or {}
        if not token.get("refresh_token"):
            expiry = datetime.fromisoformat(str(token.get("expiry", "")).replace("Z", "+00:00"))
            if not (token.get("access_token") and expiry.tzinfo and expiry > datetime.now(timezone.utc)):
                return False, "agy_login_required"
    except (OSError, ValueError, TypeError, AttributeError):
        return False, "agy_login_required"
    if workspace:
        path = Path(workspace).resolve()
        if not path.is_dir() or path.parent != Path("/tmp") or not path.name.startswith("aads-wt-"):
            return False, "agy_workspace_not_local_worktree"
    return True, "local_cli_ready"


def run_local(model: str, workspace: str, instruction: str, timeout: float) -> int:
    """Called after shell admission, without a second network/preflight probe.

    No shared credentials or relay required. Tool commands execute on this host.
    Only an explicit successful result AND a zero CLI exit code are successful.
    """
    native = _LOCAL_ALIASES.get(model, model)
    settings = {"permissions": {"allow": ["command(*)"],
                                "deny": ["command(%s)" % prefix for prefix in _LOCAL_DENY]}}
    command = [os.getenv("AGY_BIN", "agy"), "--input-format", "stream-json",
               "--output-format", "stream-json", "--model", native,
               "--print-timeout", "%ds" % max(1, int(timeout) - 5),
               "--mode", "accept-edits", "--settings", json.dumps(settings), "-p="]
    payload = (json.dumps({"event": "user", "message": {"content": instruction}}) + "\n").encode()
    proc = None
    previous = {}

    def stop_group(*_args):
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def interrupted(signum, _frame):
        stop_group()
        raise SystemExit(128 + signum)

    try:
        proc = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, interrupted)
        stdout, _stderr = proc.communicate(payload, timeout=timeout)
        if proc.returncode:
            return _fail("local agy CLI exited %s" % proc.returncode)
        if len(stdout) > 8 * 1024 * 1024:
            return _fail("local agy output limit exceeded")
        result = None
        tool_ids = set()
        tool_errors = 0
        deltas = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "result":
                result = event.get("result")
            step = event.get("step_update") or {}
            if not isinstance(step, dict):
                continue
            if step.get("step_type") == "tool":
                tool_ids.add((step.get("conversation_id"), step.get("step_index")))
                tool_errors += str(step.get("state", "")).upper() == "ERROR"
            elif step.get("step_type") == "agent_response" and isinstance(step.get("text_delta"), str):
                deltas.append(step["text_delta"])
        if not isinstance(result, dict) or str(result.get("status", "")).upper() not in ("SUCCESS", "DONE", "COMPLETED"):
            return _fail("local agy ended without successful result")
        if result.get("denied_actions"):
            return _fail("local agy tool actions denied")
        text = result.get("response") or "".join(deltas)
        if not isinstance(text, str) or not text.strip():
            return _fail("local agy empty result")
        print("AGY_RECEIPT model=%s agy_model=%s mode=runner_local tool_calls=%d tool_errors=%d" %
              (model, native, len(tool_ids), tool_errors), file=sys.stderr)
        print(text)
        return 0
    except subprocess.TimeoutExpired:
        return _fail("local agy timed out")
    except OSError:
        return _fail("local agy launch/read failed")
    finally:
        stop_group()
        if proc is not None:
            proc.communicate()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="", help="antigravity: 접두사를 뗀 모델 id")
    parser.add_argument("--workspace", default="", help="/tmp/aads-wt-<job_id>")
    parser.add_argument("--instruction-file", default="")
    parser.add_argument("--local-cli", action="store_true", help="execute agy on the runner host")
    parser.add_argument("--preflight-local", action="store_true", help="read-only host/worktree admission; no LLM call")
    parser.add_argument("--relay-url", default=os.getenv("CLAUDE_RELAY_URL", _DEFAULT_RELAY_URL))
    parser.add_argument("--session-id", default="")
    parser.add_argument("--timeout", type=float, default=3700.0,
                        help="relay 러너 모드 hard timeout(AGY_RELAY_RUNNER_HARD_TIMEOUT_SEC 기본 3600s)보다 길게")
    args = parser.parse_args(argv)
    if args.preflight_local:
        ok, reason = local_preflight(args.workspace)
        print(json.dumps({"ok": ok, "reason": reason, "transport": "local_cli"}))
        return 0 if ok else 78
    if not args.model or not args.workspace or not args.instruction_file:
        parser.error("--model, --workspace and --instruction-file are required for execution")
    try:
        instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"cannot read instruction file: {exc}", 2)
    if not instruction.strip():
        return _fail("empty instruction", 2)
    if args.local_cli:
        return run_local(args.model, args.workspace, instruction, args.timeout)
    return run(args.model, args.workspace, instruction, args.relay_url, args.session_id, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
