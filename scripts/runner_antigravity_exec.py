#!/usr/bin/env python3
"""러너 → relay /antigravity-stream 작업공간 실행기 (표준 라이브러리만 사용).

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="antigravity: 접두사를 뗀 모델 id")
    parser.add_argument("--workspace", required=True, help="/tmp/aads-wt-<job_id>")
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--relay-url", default=os.getenv("CLAUDE_RELAY_URL", _DEFAULT_RELAY_URL))
    parser.add_argument("--session-id", default="")
    parser.add_argument("--timeout", type=float, default=3700.0,
                        help="relay 러너 모드 hard timeout(AGY_RELAY_RUNNER_HARD_TIMEOUT_SEC 기본 3600s)보다 길게")
    args = parser.parse_args(argv)
    try:
        instruction = Path(args.instruction_file).read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"cannot read instruction file: {exc}", 2)
    if not instruction.strip():
        return _fail("empty instruction", 2)
    return run(args.model, args.workspace, instruction, args.relay_url, args.session_id, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
