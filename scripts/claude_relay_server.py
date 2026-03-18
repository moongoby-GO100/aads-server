#!/usr/bin/env python3
"""
Claude Code CLI Relay Server — 호스트에서 실행 (port 8199).

AADS Docker -> (httpx) -> 이 릴레이 -> claude CLI subprocess
-> NDJSON 스트리밍 응답 반환.

세션 유지: AADS session_id -> CLI session_id 매핑.
- 첫 메시지: claude -p (새 세션) -> CLI session_id 캡처
- 이후: claude -p --resume CLI_SESSION_ID (대화 이어가기)

실행: python3 /root/aads/aads-server/scripts/claude_relay_server.py
systemd: /etc/systemd/system/claude-relay.service
호환: Python 3.6+ (호스트 CentOS 7)
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("claude_relay")

PORT = int(os.getenv("CLAUDE_RELAY_PORT", "8199"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
MCP_TEMPLATE = Path(os.getenv(
    "MCP_CONFIG_TEMPLATE",
    "/root/aads/aads-server/scripts/mcp_config_template.json",
))

_MAX_CONCURRENT = int(os.getenv("CLAUDE_RELAY_MAX_CONCURRENT", "3"))
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

# AADS session_id -> CLI session_id 매핑
_session_map = {}  # type: dict
_SESSION_MAP_FILE = Path("/tmp/claude_relay_sessions.json")

# 모델 매핑
_MODEL_MAP = {
    "claude-opus": "claude-opus-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}


def _load_session_map():
    """세션 매핑 파일 로드."""
    global _session_map
    try:
        if _SESSION_MAP_FILE.exists():
            _session_map = json.loads(_SESSION_MAP_FILE.read_text())
            logger.info("Loaded %d session mappings", len(_session_map))
    except Exception as e:
        logger.warning("Failed to load session map: %s", e)
        _session_map = {}


def _save_session_map():
    """세션 매핑 파일 저장."""
    try:
        _SESSION_MAP_FILE.write_text(json.dumps(_session_map))
    except Exception as e:
        logger.warning("Failed to save session map: %s", e)


def _build_mcp_config(session_id):
    """세션별 MCP config JSON 파일 생성, 경로 반환."""
    if MCP_TEMPLATE.exists():
        template = json.loads(MCP_TEMPLATE.read_text())
    else:
        template = {
            "mcpServers": {
                "aads-tools": {
                    "command": "docker",
                    "args": [
                        "exec", "-i",
                        "-e", "AADS_SESSION_ID=" + (session_id or ""),
                        "aads-server",
                        "python", "-m", "mcp_servers.aads_tools_bridge"
                    ]
                }
            }
        }

    # 세션 ID를 env로 주입
    servers = template.get("mcpServers", {})
    for name, cfg in servers.items():
        args = cfg.get("args", [])
        new_args = []
        skip_next = False
        found_session = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg == "-e" and i + 1 < len(args) and args[i + 1].startswith("AADS_SESSION_ID="):
                new_args.extend(["-e", "AADS_SESSION_ID=" + (session_id or "")])
                skip_next = True
                found_session = True
            else:
                new_args.append(arg)
        if not found_session:
            insert_idx = 2
            new_args.insert(insert_idx, "-e")
            new_args.insert(insert_idx + 1, "AADS_SESSION_ID=" + (session_id or ""))
        cfg["args"] = new_args

    fd, path = tempfile.mkstemp(prefix="mcp_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(template, f)
    return path


async def handle_stream(request):
    """POST /stream — Claude CLI 실행 + NDJSON 스트리밍 + 세션 유지."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    system_prompt = body.get("system_prompt", "")
    messages_text = body.get("messages_text", "")
    model = body.get("model", "claude-opus")
    aads_session_id = body.get("session_id", "")

    if not messages_text:
        return web.json_response({"error": "messages_text required"}, status=400)

    cli_model = _MODEL_MAP.get(model, "claude-opus-4-6")
    mcp_config_path = _build_mcp_config(aads_session_id)

    # CLI 세션 매핑 조회
    cli_session_id = _session_map.get(aads_session_id) if aads_session_id else None
    is_resume = cli_session_id is not None

    try:
        async with _semaphore:
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "application/x-ndjson",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
            await response.prepare(request)

            # 프롬프트 구성
            if is_resume:
                # 이어가기: 사용자 메시지만 전달 (시스템 프롬프트는 세션에 보존됨)
                prompt = messages_text
            else:
                # 새 세션: 시스템 프롬프트 포함
                prompt = messages_text
                if system_prompt:
                    prompt = "[SYSTEM PROMPT]\n" + system_prompt + "\n\n[CONVERSATION]\n" + messages_text

            # Agent 팀 정의 (Agent SDK와 동일)
            agents_json = json.dumps({
                "researcher": {
                    "description": "코드 탐색, DB 조회, 로그 분석, 서버 상태 확인 등 조사가 필요할 때 사용. 여러 파일/DB를 병렬로 조사할 때 효율적.",
                    "prompt": (
                        "당신은 시스템 조사 전문가입니다. "
                        "MCP 도구(read_remote_file, query_db, query_project_database, search_logs, list_remote_dir, git_remote_status)를 사용하여 "
                        "요청된 정보를 정확하게 수집하고 구조화된 보고서로 반환하세요. "
                        "추측하지 말고 반드시 도구로 확인한 데이터만 보고하세요."
                    ),
                    "model": "sonnet",
                },
                "developer": {
                    "description": "코드 수정, 파일 작성, 패치 적용, git 커밋/푸시 등 개발 작업이 필요할 때 사용.",
                    "prompt": (
                        "당신은 풀스택 개발자입니다. "
                        "MCP 도구(write_remote_file, patch_remote_file, run_remote_command, git_remote_add, git_remote_commit, git_remote_push)를 사용하여 "
                        "요청된 코드 변경을 정확하게 수행하세요. "
                        "변경 전 반드시 현재 코드를 read_remote_file로 확인하고, 변경 후 검증하세요."
                    ),
                    "model": "sonnet",
                },
                "qa": {
                    "description": "테스트 실행, 변경사항 검증, 서비스 헬스체크, 에러 확인 등 품질 검증이 필요할 때 사용.",
                    "prompt": (
                        "당신은 QA 엔지니어입니다. "
                        "MCP 도구를 사용하여 시스템 상태를 검증하고, 에러를 탐지하고, 변경사항이 정상 반영되었는지 확인하세요. "
                        "문제 발견 시 구체적인 에러 내용과 재현 경로를 보고하세요."
                    ),
                    "model": "sonnet",
                },
            })

            # CLI 명령어 구성
            cmd = [
                CLAUDE_BIN,
                "-p",
                "--output-format", "stream-json",
                "--verbose",
                "--model", cli_model,
                "--mcp-config", mcp_config_path,
                "--strict-mcp-config",
                "--allowedTools", "Agent,mcp__aads-tools__*",
                "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,NotebookEdit",
                "--max-turns", "200",
                "--agents", agents_json,
            ]

            # 세션 이어가기
            if is_resume:
                cmd.extend(["--resume", cli_session_id])

            logger.info("CLI: model=%s aads=%s cli=%s resume=%s prompt_len=%d",
                        cli_model,
                        aads_session_id[:8] if aads_session_id else "none",
                        cli_session_id[:8] if cli_session_id else "new",
                        is_resume,
                        len(prompt))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ, CLAUDE_CODE_MAX_OUTPUT_TOKENS="16384"),
            )

            # stdin으로 프롬프트 전달
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            captured_cli_session_id = None

            try:
                while True:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=600,
                    )
                    if not line:
                        break

                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue

                    try:
                        event = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue

                    # CLI session_id 캡처 (init 또는 result 이벤트에서)
                    evt_type = event.get("type", "")
                    if evt_type == "system" and event.get("subtype") == "init":
                        captured_cli_session_id = event.get("session_id")
                    elif evt_type == "result":
                        if not captured_cli_session_id:
                            captured_cli_session_id = event.get("session_id")

                    await response.write(line.strip() + b"\n")

            except asyncio.TimeoutError:
                logger.error("CLI timeout (600s): aads=%s", aads_session_id[:8])
                error_event = json.dumps({"type": "error", "content": "CLI timeout (600s)"})
                await response.write(error_event.encode() + b"\n")
                proc.kill()
            except Exception as e:
                logger.error("Stream error: %s", e)
                error_event = json.dumps({"type": "error", "content": str(e)})
                await response.write(error_event.encode() + b"\n")
            finally:
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        proc.kill()

                if proc.stderr:
                    stderr = await proc.stderr.read()
                    if stderr:
                        logger.debug("CLI stderr: %s", stderr.decode("utf-8", errors="replace")[:500])

            # 세션 매핑 저장
            if aads_session_id and captured_cli_session_id:
                old_cli = _session_map.get(aads_session_id)
                if old_cli != captured_cli_session_id:
                    _session_map[aads_session_id] = captured_cli_session_id
                    _save_session_map()
                    logger.info("Session mapped: aads=%s -> cli=%s (resume=%s)",
                                aads_session_id[:8], captured_cli_session_id[:8], is_resume)

            await response.write_eof()
            return response

    finally:
        try:
            os.unlink(mcp_config_path)
        except OSError:
            pass


async def handle_health(request):
    """GET /health"""
    return web.json_response({
        "status": "ok",
        "port": PORT,
        "sessions": len(_session_map),
    })


async def handle_sessions(request):
    """GET /sessions — 세션 매핑 조회."""
    return web.json_response(_session_map)


async def handle_reset_session(request):
    """DELETE /sessions/{aads_session_id} — 세션 매핑 삭제 (새 세션 강제)."""
    aads_sid = request.match_info.get("aads_session_id", "")
    if aads_sid in _session_map:
        del _session_map[aads_sid]
        _save_session_map()
        return web.json_response({"deleted": aads_sid})
    return web.json_response({"error": "not found"}, status=404)


def create_app():
    app = web.Application()
    app.router.add_post("/stream", handle_stream)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/sessions", handle_sessions)
    app.router.add_delete("/sessions/{aads_session_id}", handle_reset_session)
    return app


def main():
    _load_session_map()
    logger.info("Starting Claude Relay Server on port %d (%d sessions loaded)", PORT, len(_session_map))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=logger)


if __name__ == "__main__":
    main()
