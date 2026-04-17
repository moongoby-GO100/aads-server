#!/usr/bin/env python3
"""
Claude Code CLI Relay Server — 호스트에서 실행 (port 8199).

AADS Docker -> (httpx) -> 이 릴레이 -> claude CLI subprocess
-> NDJSON 스트리밍 응답 반환.

세션 유지: AADS session_id -> CLI session_id 매핑.
인증 모드 (env: AADS_CLAUDE_DIRECT_OAUTH):
  0 (default) = 기존 LiteLLM 프록시 경유
  1 = .env.oauth에서 토큰 읽어 Anthropic 직접 OAuth (HOME 격리)

호환: Python 3.6+ (호스트 CentOS 7)
"""
import asyncio
import hashlib
import json
import logging
import os
import re
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
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
MCP_TEMPLATE = Path(os.getenv(
    "MCP_CONFIG_TEMPLATE",
    "/root/aads/aads-server/scripts/mcp_config_template.json",
))

_MAX_CONCURRENT = int(os.getenv("CLAUDE_RELAY_MAX_CONCURRENT", "3"))
_semaphore = None  # 앱 시작 시 실제 이벤트 루프에서 생성 (Python 3.6 different loop 방지)

# --- Direct OAuth ---
_DIRECT_OAUTH_ENABLED = os.getenv("AADS_CLAUDE_DIRECT_OAUTH", "0") == "1"
_ENV_OAUTH_FILE = Path(os.getenv("ENV_OAUTH_FILE", "/root/.genspark/.env.oauth"))
_RELAY_HOME = Path("/tmp/.claude-relay")
_CODEX_HOME_ROOT = Path(os.getenv("CODEX_HOME_ROOT", "/root/.codex-relay"))
_last_429_slot = 0

_session_map = {}  # type: dict
_SESSION_MAP_FILE = Path("/tmp/claude_relay_sessions.json")

_MODEL_MAP = {
    "claude-opus": "claude-opus-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}


def _read_oauth_tokens():
    token1 = token2 = ""
    current = "1"
    try:
        text = _ENV_OAUTH_FILE.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "OAUTH_TOKEN_1":
                token1 = v
            elif k == "OAUTH_TOKEN_2":
                token2 = v
            elif k == "CURRENT_OAUTH":
                current = v
    except Exception as e:
        logger.error("Failed to read %s: %s", _ENV_OAUTH_FILE, e)
    return token1, token2, current


def _pick_token(preferred_slot=None):
    global _last_429_slot
    token1, token2, current = _read_oauth_tokens()
    if preferred_slot:
        slot = preferred_slot
    elif _last_429_slot:
        slot = "2" if _last_429_slot == 1 else "1"
    else:
        slot = current
    if slot == "1" and token1:
        return token1, "1", "gmail"
    elif slot == "2" and token2:
        return token2, "2", "naver"
    elif token1:
        return token1, "1", "gmail"
    elif token2:
        return token2, "2", "naver"
    return "", "0", "none"


def _ensure_relay_home():
    claude_dir = _RELAY_HOME / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_file = claude_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text("{}")
    return str(_RELAY_HOME)


def _build_claude_env(token):
    if not _DIRECT_OAUTH_ENABLED:
        return dict(os.environ, CLAUDE_CODE_MAX_OUTPUT_TOKENS="16384")
    relay_home = _ensure_relay_home()
    env = {}
    for k in ("PATH", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME",
              "SHELL", "TMPDIR", "XDG_RUNTIME_DIR", "NODE_PATH",
              "NVM_DIR", "NVM_BIN", "NVM_INC"):
        if k in os.environ:
            env[k] = os.environ[k]
    env["HOME"] = relay_home
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "16384"
    return env


def _load_session_map():
    global _session_map
    try:
        if _SESSION_MAP_FILE.exists():
            _session_map = json.loads(_SESSION_MAP_FILE.read_text())
            logger.info("Loaded %d session mappings", len(_session_map))
    except Exception as e:
        logger.warning("Failed to load session map: %s", e)
        _session_map = {}


def _save_session_map():
    try:
        _SESSION_MAP_FILE.write_text(json.dumps(_session_map))
    except Exception as e:
        logger.warning("Failed to save session map: %s", e)


def _build_mcp_config(session_id):
    template = _load_mcp_template(session_id)
    fd, path = tempfile.mkstemp(prefix="mcp_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(template, f)
    return path


def _load_mcp_template(session_id):
    # session_id 빈값 방지: MCP bridge가 세션 컨텍스트를 잃지 않도록 'default' 기본값 주입
    safe_sid = session_id or "default"
    if MCP_TEMPLATE.exists():
        template = json.loads(MCP_TEMPLATE.read_text())
    else:
        template = {"mcpServers": {"aads-tools": {"command": "docker", "args": [
            "exec", "-i", "-e", "AADS_SESSION_ID=" + safe_sid,
            "aads-server", "python", "-m", "mcp_servers.aads_tools_bridge"]}}}
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
                new_args.extend(["-e", "AADS_SESSION_ID=" + safe_sid])
                skip_next = True
                found_session = True
            else:
                new_args.append(arg)
        if not found_session:
            new_args.insert(2, "-e")
            new_args.insert(3, "AADS_SESSION_ID=" + safe_sid)
        cfg["args"] = new_args
    return template


def _toml_basic_string(value):
    return json.dumps(value)


def _extract_text_from_content(content):
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
            parts.append(block.get("text", ""))
    return "".join(parts)


def _parse_codex_tool_event(event):
    """Codex --json 이벤트 → 표준 tool_use/tool_result 변환.

    Codex 실제 스키마 (2026-04-17 실측):
      - item.started  + item.type=='mcp_tool_call'         → tool_use
      - item.completed + item.type=='mcp_tool_call'        → tool_result
      - item.completed + item.type=='command_execution'    → tool_result (bash)
      - item.completed + item.type=='function_call'        → tool_use (구버전 호환)
      - item.completed + item.type=='function_call_output' → tool_result (구버전 호환)
    """
    evt_type = event.get("type", "")
    item = event.get("item", {}) or {}
    item_type = item.get("type", "")

    # --- MCP 도구 호출 시작 ---
    if evt_type == "item.started" and item_type == "mcp_tool_call":
        args = item.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        return {
            "type": "tool_use",
            "tool_name": item.get("tool", "") or item.get("name", ""),
            "tool_use_id": item.get("id", "") or item.get("call_id", ""),
            "tool_input": args,
            "server": item.get("server", ""),
        }

    # --- MCP 도구 호출 완료 ---
    if evt_type == "item.completed" and item_type == "mcp_tool_call":
        result = item.get("result") or {}
        error = item.get("error")
        if error:
            content = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            is_error = True
        else:
            content_blocks = result.get("content") if isinstance(result, dict) else None
            if isinstance(content_blocks, list):
                content = _extract_text_from_content(content_blocks)
            elif isinstance(result, str):
                content = result
            else:
                content = json.dumps(result, ensure_ascii=False) if result else ""
            is_error = False
        return {
            "type": "tool_result",
            "tool_name": item.get("tool", "") or item.get("name", ""),
            "tool_use_id": item.get("id", "") or item.get("call_id", ""),
            "content": content,
            "is_error": is_error,
        }

    # --- bash/shell 실행 (command_execution) ---
    if evt_type == "item.completed" and item_type == "command_execution":
        return {
            "type": "tool_result",
            "tool_name": "bash",
            "tool_use_id": item.get("id", ""),
            "content": item.get("aggregated_output", "") or "",
            "is_error": bool(item.get("exit_code", 0)),
        }

    # --- 구버전 호환 (function_call / function_call_output) ---
    if evt_type == "item.completed" and item_type == "function_call":
        return {
            "type": "tool_use",
            "tool_name": item.get("name", ""),
            "tool_use_id": item.get("call_id", "") or item.get("id", ""),
            "tool_input": item.get("arguments", {}),
        }
    if evt_type == "item.completed" and item_type == "function_call_output":
        output = item.get("output")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False) if output is not None else ""
        return {
            "type": "tool_result",
            "tool_name": item.get("name", ""),
            "tool_use_id": item.get("call_id", "") or item.get("id", ""),
            "content": output,
        }

    if evt_type == "function_call_output":
        output = event.get("output")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False) if output is not None else ""
        return {
            "type": "tool_result",
            "tool_name": event.get("name", ""),
            "tool_use_id": event.get("call_id", "") or event.get("id", ""),
            "content": output,
        }

    return None


def _build_codex_home(session_id):
    _CODEX_HOME_ROOT.mkdir(parents=True, exist_ok=True)
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "default")
    home = _CODEX_HOME_ROOT / safe_session
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    # auth.json을 기본 Codex HOME(/root/.codex)에서 세션 HOME으로 심볼릭 링크
    # → HOME 분리로 인한 401 Unauthorized 방지 (ChatGPT Plus OAuth 공유)
    default_auth = Path("/root/.codex/auth.json")
    session_auth = codex_dir / "auth.json"
    if default_auth.exists():
        try:
            if session_auth.is_symlink() or session_auth.exists():
                session_auth.unlink()
            session_auth.symlink_to(default_auth)
        except Exception as exc:
            logger.warning("Codex auth.json symlink 실패 session=%s err=%s", session_id, exc)

    mcp_cfg = _load_mcp_template(session_id)
    server_cfg = (mcp_cfg.get("mcpServers", {}) or {}).get("aads-tools", {})
    command = server_cfg.get("command", "docker")
    args = server_cfg.get("args", [])

    # TOML 구조: 최상위(globals) → [projects."..."] → [mcp_servers.aads-tools]
    # 참고: https://developers.openai.com/codex/mcp
    cfg_lines = [
        # --- globals (공식 스펙: approval_policy, sandbox_mode, model_reasoning_effort) ---
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        'model_reasoning_effort = "high"',
        "",
        # --- [projects."/root/aads/aads-server"] ---
        '[projects."/root/aads/aads-server"]',
        'trust_level = "trusted"',
        "",
        # --- [mcp_servers.aads-tools] ---
        "[mcp_servers.aads-tools]",
        "command = " + _toml_basic_string(command),
        "args = [" + ", ".join(_toml_basic_string(arg) for arg in args) + "]",
        # --- 공식 스펙: startup_timeout_sec, tool_timeout_sec ---
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 120",
    ]

    cwd = server_cfg.get("cwd")
    if cwd:
        cfg_lines.append("cwd = " + _toml_basic_string(cwd))

    env_map = server_cfg.get("env", {}) or {}
    if env_map:
        cfg_lines.extend([
            "",
            "[mcp_servers.aads-tools.env]",
        ])
        for key, value in env_map.items():
            cfg_lines.append("{0} = {1}".format(key, _toml_basic_string(value)))

    config_path = codex_dir / "config.toml"
    next_config_text = "\n".join(cfg_lines) + "\n"

    current_config_text = ""
    if config_path.exists():
        try:
            current_config_text = config_path.read_text()
        except Exception as exc:
            logger.warning("Codex config.toml read 실패 session=%s err=%s", session_id, exc)

    current_hash = hashlib.sha256(current_config_text.encode("utf-8")).hexdigest()
    next_hash = hashlib.sha256(next_config_text.encode("utf-8")).hexdigest()
    if current_hash != next_hash:
        config_path.write_text(next_config_text)
        logger.info(f"[codex] config.toml refreshed for session={session_id or 'default'}")
    else:
        logger.debug(f"[codex] config.toml unchanged for session={session_id or 'default'}")

    return str(home)


async def handle_stream(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    system_prompt = body.get("system_prompt", "")
    messages_text = body.get("messages_text", "")
    content_blocks = body.get("content_blocks")
    model = body.get("model", "claude-opus")
    aads_session_id = body.get("session_id", "")

    if not messages_text and not content_blocks:
        return web.json_response({"error": "messages_text or content_blocks required"}, status=400)

    use_stream_json_input = bool(content_blocks)
    cli_model = _MODEL_MAP.get(model, "claude-opus-4-6")
    mcp_config_path = _build_mcp_config(aads_session_id)
    cli_session_id = _session_map.get(aads_session_id) if aads_session_id else None
    is_resume = cli_session_id is not None

    if _DIRECT_OAUTH_ENABLED:
        requested_slot = body.get("oauth_slot")
        token, slot, label = _pick_token(preferred_slot=requested_slot)
        if not token:
            return web.json_response({"error": "no OAuth token available"}, status=500)
        logger.info("Direct OAuth: slot=%s label=%s (requested=%s)", slot, label, requested_slot or "auto")
    else:
        token, slot, label = "", "0", "proxy"

    try:
        async with _semaphore:
            response = web.StreamResponse(status=200, reason="OK", headers={
                "Content-Type": "application/x-ndjson",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            })
            await response.prepare(request)

            if use_stream_json_input:
                if is_resume:
                    stdin_payload = json.dumps(content_blocks)
                else:
                    blocks = list(content_blocks)
                    if system_prompt:
                        blocks.insert(0, {"type": "text", "text": "[SYSTEM PROMPT]\n" + system_prompt + "\n\n[CONVERSATION]\n"})
                    stdin_payload = json.dumps(blocks)
                prompt = None
            else:
                stdin_payload = None
                if is_resume:
                    prompt = messages_text
                else:
                    prompt = messages_text
                    if system_prompt:
                        prompt = "[SYSTEM PROMPT]\n" + system_prompt + "\n\n[CONVERSATION]\n" + messages_text

            agents_json = json.dumps({
                "researcher": {"description": "코드 탐색, DB 조회, 로그 분석, 서버 상태 확인 등 조사가 필요할 때 사용.", "prompt": "당신은 시스템 조사 전문가입니다. MCP 도구를 사용하여 요청된 정보를 정확하게 수집하고 구조화된 보고서로 반환하세요.", "model": "sonnet"},
                "developer": {"description": "코드 수정, 파일 작성, 패치 적용, git 커밋/푸시 등 개발 작업이 필요할 때 사용.", "prompt": "당신은 풀스택 개발자입니다. MCP 도구를 사용하여 요청된 코드 변경을 정확하게 수행하세요.", "model": "sonnet"},
                "qa": {"description": "테스트 실행, 변경사항 검증, 서비스 헬스체크 등 품질 검증이 필요할 때 사용.", "prompt": "당신은 QA 엔지니어입니다. MCP 도구를 사용하여 시스템 상태를 검증하세요.", "model": "sonnet"},
            })

            cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose",
                   "--model", cli_model, "--mcp-config", mcp_config_path, "--strict-mcp-config",
                   "--allowedTools", "Agent,mcp__aads-tools__*",
                   "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,NotebookEdit",
                   "--max-turns", "200", "--agents", agents_json]
            if use_stream_json_input:
                cmd.extend(["--input-format", "stream-json"])
            if is_resume:
                cmd.extend(["--resume", cli_session_id])

            _stdin_data = stdin_payload if use_stream_json_input else prompt
            logger.info("CLI: model=%s aads=%s cli=%s resume=%s prompt_len=%d",
                        cli_model, aads_session_id[:8] if aads_session_id else "none",
                        cli_session_id[:8] if cli_session_id else "new", is_resume,
                        len(_stdin_data) if _stdin_data else 0)

            cli_env = _build_claude_env(token)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=cli_env)

            proc.stdin.write(_stdin_data.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            captured_cli_session_id = None

            try:
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
                    if not line:
                        break
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        event = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    if evt_type == "system" and event.get("subtype") == "init":
                        captured_cli_session_id = event.get("session_id")
                        if _DIRECT_OAUTH_ENABLED:
                            event["claude_auth_mode"] = "direct"
                            event["oauth_slot"] = slot
                            line = json.dumps(event).encode("utf-8")
                    elif evt_type == "result":
                        if not captured_cli_session_id:
                            captured_cli_session_id = event.get("session_id")
                        if _DIRECT_OAUTH_ENABLED and event.get("is_error"):
                            result_text = str(event.get("result", ""))
                            if re.search(r"429|rate.limit|overloaded|credit", result_text, re.IGNORECASE):
                                _last_429_slot = int(slot) if slot.isdigit() else 0
                                logger.warning("429/rate_limit on slot=%s", slot)
                    await response.write(line.strip() + b"\n")
            except asyncio.TimeoutError:
                logger.error("CLI timeout (600s): aads=%s", aads_session_id[:8])
                await response.write(json.dumps({"type": "error", "content": "CLI timeout (600s)"}).encode() + b"\n")
                proc.kill()
            except Exception as e:
                logger.error("Stream error: %s", e)
                await response.write(json.dumps({"type": "error", "content": str(e)}).encode() + b"\n")
            finally:
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        proc.kill()
                if proc.stderr:
                    stderr_bytes = await proc.stderr.read()
                    if stderr_bytes:
                        logger.debug("CLI stderr: %s", stderr_bytes.decode("utf-8", errors="replace")[:500])

            if proc.returncode != 0:
                logger.warning("CLI exited %s (slot=%s, resume=%s)", proc.returncode, slot, is_resume)
                if is_resume and aads_session_id and aads_session_id in _session_map:
                    del _session_map[aads_session_id]
                    _save_session_map()
                    logger.info("Cleared stale session: aads=%s", aads_session_id[:8])

            # 실패(exit!=0) 시 세션 저장 금지 — OAuth 슬롯 폴백 시 잘못된 --resume 방지
            if proc.returncode == 0 and aads_session_id and captured_cli_session_id:
                old_cli = _session_map.get(aads_session_id)
                if old_cli != captured_cli_session_id:
                    _session_map[aads_session_id] = captured_cli_session_id
                    _save_session_map()
                    logger.info("Session mapped: aads=%s -> cli=%s", aads_session_id[:8], captured_cli_session_id[:8])
            await response.write_eof()
            return response
    finally:
        try:
            os.unlink(mcp_config_path)
        except OSError:
            pass


_CODEX_MODEL_MAP = {
    "gpt-5": "gpt-5.4", "gpt-5-mini": "gpt-5.4-mini",
    "gpt-5.4": "gpt-5.4", "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.3-codex": "gpt-5.3-codex",
}


async def handle_codex_stream(request):
    """Codex CLI subprocess -> NDJSON pseudo-streaming. ChatGPT Plus OAuth."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    system_prompt = body.get("system_prompt", "")
    messages_text = body.get("messages_text", "")
    tool_names = body.get("tool_names", [])
    model = body.get("model", "gpt-5.4")
    session_id = body.get("session_id", "")
    if not messages_text:
        return web.json_response({"error": "messages_text required"}, status=400)
    codex_model = _CODEX_MODEL_MAP.get(model, "gpt-5.4")
    prompt = messages_text
    if system_prompt:
        prompt = "[SYSTEM]\n" + system_prompt + "\n\n[USER]\n" + messages_text
    if tool_names:
        prompt = "[AVAILABLE_AADS_MCP_TOOLS]\n" + ", ".join(tool_names) + "\n\n" + prompt
    try:
        async with _semaphore:
            response = web.StreamResponse(status=200, reason="OK", headers={
                "Content-Type": "application/x-ndjson",
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            await response.prepare(request)
            codex_home = _build_codex_home(session_id)
            cmd = [CODEX_BIN, "exec", "--json", "--full-auto",
                   "--skip-git-repo-check", "-C", "/root/aads/aads-server"]
            if codex_model:
                cmd.extend(["-m", codex_model])
            cmd.append(prompt)
            logger.info("Codex: model=%s prompt_len=%d tools=%d", codex_model, len(prompt), len(tool_names))
            proc_env = dict(os.environ)
            # Genspark 프록시 리다이렉트 차단 — ChatGPT Plus OAuth(auth.json) 직접 사용
            proc_env.pop("OPENAI_BASE_URL", None)
            proc_env.pop("OPENAI_API_KEY", None)
            proc_env["AADS_SESSION_ID"] = session_id or "default"
            proc_env["HOME"] = codex_home
            proc_env.setdefault("TMPDIR", "/tmp")
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=proc_env)
            proc.stdin.close()
            full_text = ""
            input_tokens = output_tokens = 0
            try:
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=300)
                    if not line:
                        break
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        event = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    tool_evt = _parse_codex_tool_event(event)
                    if tool_evt:
                        await response.write(json.dumps(tool_evt).encode() + b"\n")
                        continue
                    text = ""
                    if evt_type == "item.completed":
                        item = event.get("item", {}) or {}
                        # agent_message 타입만 최종 텍스트로 추출
                        # (mcp_tool_call / command_execution 등은 _parse_codex_tool_event에서 이미 tool_evt로 처리됨)
                        if item.get("type") == "agent_message":
                            text = item.get("text", "")
                            if not text:
                                text = _extract_text_from_content(item.get("content"))
                    elif evt_type == "item.streaming":
                        # Codex가 실시간 델타를 보내는 경우(긴 응답) 즉시 전달
                        item = event.get("item", {}) or {}
                        if item.get("type") == "agent_message":
                            text = item.get("delta", "") or item.get("text", "")
                    elif evt_type in {"message.delta", "message.completed"}:
                        delta = event.get("delta", {})
                        if isinstance(delta, dict):
                            text = delta.get("text", "")
                            if not text:
                                text = _extract_text_from_content(delta.get("content"))
                        elif isinstance(delta, str):
                            text = delta
                        if not text:
                            message = event.get("message", {})
                            if isinstance(message, dict):
                                text = _extract_text_from_content(message.get("content"))
                    elif evt_type in {"tool.completed", "item.created", "thread.started", "turn.started"}:
                        text = ""
                    if text:
                        if full_text and text and full_text.endswith(text):
                            text = ""
                    if text:
                        for i in range(0, len(text), 40):
                            chunk = text[i:i + 40]
                            await response.write(
                                json.dumps({"type": "assistant", "subtype": "text", "text": chunk}).encode() + b"\n")
                            await asyncio.sleep(0.015)
                        full_text += text
                    elif evt_type == "turn.completed":
                        usage = event.get("usage", {})
                        input_tokens += usage.get("input_tokens", 0)
                        output_tokens += usage.get("output_tokens", 0)
            except asyncio.TimeoutError:
                logger.error("Codex timeout (300s): model=%s", codex_model)
                await response.write(json.dumps({"type": "error", "content": "Codex CLI timeout"}).encode() + b"\n")
                proc.kill()
            except Exception as e:
                logger.error("Codex stream error: %s", e)
                await response.write(json.dumps({"type": "error", "content": str(e)}).encode() + b"\n")
            finally:
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        proc.kill()
                if proc.stderr:
                    stderr_bytes = await proc.stderr.read()
                    if stderr_bytes:
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                        if "ERROR" in stderr_text or "Forbidden" in stderr_text or "Unauthorized" in stderr_text:
                            logger.warning("Codex stderr: %s", stderr_text[:800])
                        else:
                            logger.info("Codex stderr: %s", stderr_text[:500])
            await response.write(json.dumps({
                "type": "result", "result": full_text,
                "input_tokens": input_tokens, "output_tokens": output_tokens, "model": model,
            }).encode() + b"\n")
            await response.write_eof()
            return response
    except Exception as e:
        logger.error("Codex handler error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request):
    health = {"status": "ok", "port": PORT, "sessions": len(_session_map),
              "auth_mode": "direct_oauth" if _DIRECT_OAUTH_ENABLED else "litellm_proxy"}
    if _DIRECT_OAUTH_ENABLED:
        token, slot, label = _pick_token()
        health.update({"oauth_slot": slot, "oauth_label": label, "token_available": bool(token)})
    return web.json_response(health)


async def handle_oauth_switch(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slot = body.get("slot")
    if not slot:
        slot = {"naver": "2", "gmail": "1"}.get(body.get("primary", "").lower())
    if slot not in ("1", "2"):
        return web.json_response({"error": "slot must be '1' or '2'"}, status=400)
    try:
        text = _ENV_OAUTH_FILE.read_text()
        new_text = re.sub(r"CURRENT_OAUTH=\d+", "CURRENT_OAUTH=" + slot, text)
        if new_text != text:
            _ENV_OAUTH_FILE.write_text(new_text)
            logger.info("OAuth switched: CURRENT_OAUTH=%s", slot)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    global _last_429_slot
    _last_429_slot = 0
    token, cur_slot, label = _pick_token()
    return web.json_response({"ok": True, "slot": cur_slot, "label": label, "token_available": bool(token)})


async def handle_sessions(request):
    return web.json_response(_session_map)


async def handle_reset_session(request):
    aads_sid = request.match_info.get("aads_session_id", "")
    if aads_sid in _session_map:
        del _session_map[aads_sid]
        _save_session_map()
        return web.json_response({"deleted": aads_sid})
    return web.json_response({"error": "not found"}, status=404)


async def _on_startup(app):
    """앱 시작 시 실제 이벤트 루프에서 Semaphore 생성 (Python 3.6 호환)."""
    global _semaphore
    _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    logger.info("Semaphore created: max_concurrent=%d", _MAX_CONCURRENT)


def create_app():
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.router.add_post("/stream", handle_stream)
    app.router.add_post("/codex-stream", handle_codex_stream)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/oauth/switch", handle_oauth_switch)
    app.router.add_get("/sessions", handle_sessions)
    app.router.add_delete("/sessions/{aads_session_id}", handle_reset_session)
    return app


def main():
    _load_session_map()
    auth_mode = "DIRECT_OAUTH" if _DIRECT_OAUTH_ENABLED else "LITELLM_PROXY"
    logger.info("Starting Claude Relay on port %d (%d sessions) [auth=%s]", PORT, len(_session_map), auth_mode)
    if _DIRECT_OAUTH_ENABLED:
        _ensure_relay_home()
        token, slot, label = _pick_token()
        logger.info("OAuth ready: slot=%s label=%s ok=%s", slot, label, bool(token))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=logger)


if __name__ == "__main__":
    main()
