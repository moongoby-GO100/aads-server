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
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

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
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_WRAPPER = Path(os.getenv(
    "CLAUDE_NONINTERACTIVE_WRAPPER",
    str(_REPO_ROOT / "scripts" / "claude-docker-wrapper.sh"),
))
MCP_TEMPLATE = Path(os.getenv(
    "MCP_CONFIG_TEMPLATE",
    "/root/aads/aads-server/scripts/mcp_config_template.json",
))
_MCP_BRIDGE_MODE = (os.getenv("AADS_MCP_BRIDGE_MODE", "auto") or "auto").strip().lower()
_MCP_BRIDGE_PYTHON = (os.getenv("AADS_MCP_BRIDGE_PYTHON", "python3.11") or "python3.11").strip()
_MCP_PREFLIGHT_TIMEOUT_SEC = float(os.getenv("AADS_MCP_PREFLIGHT_TIMEOUT_SEC", "1.5"))
_MCP_PREFLIGHT_CACHE_TTL = float(os.getenv("AADS_MCP_PREFLIGHT_CACHE_TTL", "15"))
_MCP_PREFLIGHT_CACHE = {}

# 2GB급 호스트에서 Claude/Codex 동시 실행 3개는 메모리 압박으로 137(OOM성 종료)을 유발할 수 있다.
# 운영자가 명시적으로 override하지 않으면 보수적으로 1개만 허용한다.
_MAX_CONCURRENT = int(os.getenv("CLAUDE_RELAY_MAX_CONCURRENT", "1"))
_semaphore = None  # 앱 시작 시 실제 이벤트 루프에서 생성 (Python 3.6 different loop 방지)

# --- Direct OAuth ---
_DIRECT_OAUTH_ENABLED = os.getenv("AADS_CLAUDE_DIRECT_OAUTH", "0") == "1"
_ENV_OAUTH_FILE = Path(os.getenv("ENV_OAUTH_FILE", "/root/.genspark/.env.oauth"))
_RELAY_HOME = Path("/tmp/.claude-relay")
_CODEX_HOME_ROOT = Path(os.getenv("CODEX_HOME_ROOT", "/root/.codex-relay"))
_AADS_API_OAUTH_STATE_URL = os.getenv(
    "CLAUDE_RELAY_OAUTH_STATE_URL",
    "http://127.0.0.1:8100/api/v1/health/claude-relay/oauth-state",
)
_RELAY_SECRET_FILE = Path(os.getenv(
    "CLAUDE_RELAY_SHARED_SECRET_FILE",
    "/root/aads/aads-server/scripts/claude_relay_secret.txt",
))
_DB_OAUTH_CACHE_TTL = int(os.getenv("CLAUDE_RELAY_DB_OAUTH_CACHE_TTL", "30"))
_DB_OAUTH_CACHE = {"rows": None, "ts": 0}
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


def _load_relay_secret():
    secret = (os.getenv("CLAUDE_RELAY_SHARED_SECRET") or "").strip()
    if secret:
        return secret
    try:
        return _RELAY_SECRET_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_db_oauth_rows():
    now = time.time()
    cached_rows = _DB_OAUTH_CACHE.get("rows")
    cached_ts = _DB_OAUTH_CACHE.get("ts", 0)
    if cached_rows is not None and (now - cached_ts) < _DB_OAUTH_CACHE_TTL:
        return cached_rows

    headers = {}
    secret = _load_relay_secret()
    if secret:
        headers["X-Claude-Relay-Secret"] = secret
    req = urllib_request.Request(_AADS_API_OAUTH_STATE_URL, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        rows = payload.get("keys", []) if isinstance(payload, dict) else []
        if isinstance(rows, list):
            _DB_OAUTH_CACHE["rows"] = rows
            _DB_OAUTH_CACHE["ts"] = now
            return rows
    except (urllib_error.URLError, urllib_error.HTTPError, ValueError, json.JSONDecodeError) as e:
        logger.warning("DB OAuth read failed, using env fallback: %s", e)
    return None


def _is_rate_limited_row(row):
    until = (row or {}).get("rate_limited_until")
    if not until:
        return False
    try:
        target = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return target > datetime.now(timezone.utc)
    except Exception:
        return False


def _read_oauth_tokens():
    token1 = token2 = ""
    current = "1"
    label1 = "slot1"
    label2 = "slot2"
    db_rows = _read_db_oauth_rows()
    if db_rows:
        for row in db_rows:
            slot = str(row.get("slot", "") or "")
            if slot == "1":
                token1 = row.get("value", "") or token1
                label1 = row.get("label", "") or label1
            elif slot == "2":
                token2 = row.get("value", "") or token2
                label2 = row.get("label", "") or label2
        ordered = [row for row in db_rows if row.get("value")]
        ordered.sort(key=lambda row: int(row.get("priority", 9999) or 9999))
        preferred = [row for row in ordered if not _is_rate_limited_row(row)]
        if preferred:
            current = str(preferred[0].get("slot", "") or current)
        elif ordered:
            current = str(ordered[0].get("slot", "") or current)
        if token1 or token2:
            return token1, token2, current, label1, label2
    try:
        text = _ENV_OAUTH_FILE.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                lower = line.lower()
                if "token1:" in lower:
                    label1 = line.split(":", 1)[1].strip().split("(")[0].strip() or label1
                elif "token2:" in lower:
                    label2 = line.split(":", 1)[1].strip().split("(")[0].strip() or label2
                continue
            if "=" not in line:
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
    return token1, token2, current, label1, label2


def _pick_token(preferred_slot=None):
    global _last_429_slot
    token1, token2, current, label1, label2 = _read_oauth_tokens()
    if preferred_slot:
        slot = preferred_slot
    elif _last_429_slot:
        slot = "2" if _last_429_slot == 1 else "1"
    else:
        slot = current
    if slot == "1" and token1:
        return token1, "1", label1
    elif slot == "2" and token2:
        return token2, "2", label2
    elif token1:
        return token1, "1", label1
    elif token2:
        return token2, "2", label2
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


def _short_session(session_id):
    return (session_id or "default")[:8]


def _resolve_cli_command(kind):
    configured = (CLAUDE_BIN if kind == "claude" else CODEX_BIN).strip()
    if kind == "claude" and configured in ("", "claude") and _CLAUDE_WRAPPER.exists():
        return {
            "kind": kind,
            "mode": "docker_wrapper",
            "argv": [str(_CLAUDE_WRAPPER)],
            "resolved": str(_CLAUDE_WRAPPER),
        }

    target = configured or ("claude" if kind == "claude" else "codex")
    if os.path.sep in target or target.startswith("."):
        expanded = str(Path(target).expanduser())
        return {
            "kind": kind,
            "mode": "explicit_path",
            "argv": [expanded],
            "resolved": expanded,
        }

    resolved = shutil.which(target)
    return {
        "kind": kind,
        "mode": "path_lookup",
        "argv": [resolved or target],
        "resolved": resolved or target,
    }


def _preflight_cli_command(meta):
    cmd = (meta or {}).get("resolved") or ""
    if not cmd:
        return {"ok": False, "error_type": "missing_binary", "detail": "empty command"}

    cmd_path = Path(cmd)
    if os.path.isabs(cmd) or cmd.startswith("."):
        if not cmd_path.exists():
            return {"ok": False, "error_type": "missing_binary", "detail": cmd}
        if not os.access(cmd, os.X_OK):
            return {"ok": False, "error_type": "not_executable", "detail": cmd}
        return {"ok": True, "detail": cmd}

    resolved = shutil.which(cmd)
    if not resolved:
        return {"ok": False, "error_type": "missing_binary", "detail": cmd}
    return {"ok": True, "detail": resolved}


def _copy_server_cfg(cfg):
    return {
        "command": (cfg or {}).get("command", ""),
        "args": list((cfg or {}).get("args", []) or []),
        "cwd": (cfg or {}).get("cwd"),
        "env": dict((cfg or {}).get("env", {}) or {}),
    }


def _inject_session_into_cfg(cfg, session_id):
    safe_sid = session_id or "default"
    updated = _copy_server_cfg(cfg)
    env_map = dict(updated.get("env", {}) or {})
    env_map["AADS_SESSION_ID"] = safe_sid
    updated["env"] = env_map

    if Path(str(updated.get("command", ""))).name == "docker":
        args = list(updated.get("args", []) or [])
        new_args = []
        skip_next = False
        found_session = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg == "-e" and i + 1 < len(args) and str(args[i + 1]).startswith("AADS_SESSION_ID="):
                new_args.extend(["-e", "AADS_SESSION_ID=" + safe_sid])
                skip_next = True
                found_session = True
            else:
                new_args.append(arg)
        if not found_session:
            insert_at = 1
            if new_args[:2] == ["exec", "-i"]:
                insert_at = 2
            elif new_args[:1] == ["exec"]:
                insert_at = 1
            new_args[insert_at:insert_at] = ["-e", "AADS_SESSION_ID=" + safe_sid]
        updated["args"] = new_args
    return updated


def _build_docker_bridge_cfg(session_id, base_cfg=None):
    safe_sid = session_id or "default"
    cfg = {
        "command": shutil.which("docker") or "docker",
        "args": [
            "exec", "-i", "-e", "AADS_SESSION_ID=" + safe_sid,
            "aads-server", "python3", "-m", "mcp_servers.aads_tools_bridge",
        ],
        "cwd": None,
        "env": dict((base_cfg or {}).get("env", {}) or {}),
        "_path_mode": "docker_exec",
    }
    cfg["env"]["AADS_SESSION_ID"] = safe_sid
    return cfg


def _build_python_bridge_cfg(session_id, base_cfg=None):
    safe_sid = session_id or "default"
    env_map = dict((base_cfg or {}).get("env", {}) or {})
    existing_pythonpath = env_map.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
    path_parts = [str(_REPO_ROOT)]
    if existing_pythonpath:
        path_parts.append(existing_pythonpath)
    env_map["PYTHONPATH"] = os.pathsep.join(
        [part for idx, part in enumerate(path_parts) if part and part not in path_parts[:idx]]
    )
    env_map["AADS_SESSION_ID"] = safe_sid
    return {
        "command": shutil.which(_MCP_BRIDGE_PYTHON) or _MCP_BRIDGE_PYTHON,
        "args": ["-m", "mcp_servers.aads_tools_bridge"],
        "cwd": str(_REPO_ROOT),
        "env": env_map,
        "_path_mode": "python3.11_direct",
    }


def _candidate_signature(cfg):
    args = []
    for arg in list((cfg or {}).get("args", []) or []):
        text = str(arg)
        if text.startswith("AADS_SESSION_ID="):
            text = "AADS_SESSION_ID=*"
        args.append(text)
    env_items = []
    for key, value in sorted(dict((cfg or {}).get("env", {}) or {}).items()):
        text = str(value)
        if key == "AADS_SESSION_ID":
            text = "*"
        env_items.append((str(key), text))
    return (
        (cfg or {}).get("command", ""),
        tuple(args),
        (cfg or {}).get("cwd") or "",
        tuple(env_items),
    )


def _candidate_mcp_cfgs(session_id, base_cfg=None):
    candidates = []
    if base_cfg and base_cfg.get("command"):
        template_cfg = _inject_session_into_cfg(base_cfg, session_id)
        template_cfg["_path_mode"] = "template"
        candidates.append(template_cfg)

    if _MCP_BRIDGE_MODE in ("auto", "docker", "docker_exec"):
        candidates.append(_build_docker_bridge_cfg(session_id, base_cfg))
    if _MCP_BRIDGE_MODE in ("auto", "direct", "python", "python3.11_direct", "python311_direct"):
        if (_REPO_ROOT / "mcp_servers" / "aads_tools_bridge.py").exists():
            candidates.append(_build_python_bridge_cfg(session_id, base_cfg))

    deduped = []
    seen = set()
    for candidate in candidates:
        signature = _candidate_signature(candidate)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(candidate)
    return deduped


def _classify_preflight_detail(detail):
    lowered = (detail or "").lower()
    if "no such container" in lowered:
        return "docker_container_missing"
    if "cannot connect to the docker daemon" in lowered:
        return "docker_daemon_unavailable"
    if "no module named" in lowered or "modulenotfounderror" in lowered:
        return "python_module_missing"
    if "permission denied" in lowered:
        return "permission_denied"
    if "no such file or directory" in lowered:
        return "missing_binary"
    return "preflight_failed"


async def _preflight_mcp_server(name, cfg):
    signature = _candidate_signature(cfg)
    cached = _MCP_PREFLIGHT_CACHE.get(signature)
    now = time.time()
    if cached and (now - cached.get("ts", 0)) < _MCP_PREFLIGHT_CACHE_TTL:
        diag = dict(cached.get("diag", {}))
        diag["cached"] = True
        return diag

    diag = {
        "server": name,
        "path_mode": (cfg or {}).get("_path_mode", "unknown"),
        "command": (cfg or {}).get("command", ""),
        "cwd": (cfg or {}).get("cwd"),
        "ok": False,
        "cached": False,
        "error_type": "",
        "detail": "",
    }
    proc = None
    try:
        env = dict(os.environ)
        env.update(dict((cfg or {}).get("env", {}) or {}))
        proc = await asyncio.create_subprocess_exec(
            cfg.get("command", ""),
            *list(cfg.get("args", []) or []),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cfg.get("cwd") or None,
            env=env,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_MCP_PREFLIGHT_TIMEOUT_SEC)
            stdout = (await proc.stdout.read()).decode("utf-8", errors="replace")[:500]
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")[:500]
            detail = (stderr or stdout or ("exit=%s" % proc.returncode)).strip()
            diag.update({
                "ok": False,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "detail": detail,
                "error_type": _classify_preflight_detail(detail),
            })
        except asyncio.TimeoutError:
            diag["ok"] = True
    except Exception as exc:
        detail = str(exc)
        diag.update({
            "ok": False,
            "detail": detail,
            "error_type": _classify_preflight_detail(detail),
        })
    finally:
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1)
            except (asyncio.TimeoutError, ProcessLookupError):
                proc.kill()

    _MCP_PREFLIGHT_CACHE[signature] = {"ts": time.time(), "diag": dict(diag)}
    return diag


async def _resolve_aads_tools_cfg(session_id, relay_name, base_cfg=None):
    failures = []
    for candidate in _candidate_mcp_cfgs(session_id, base_cfg):
        diag = await _preflight_mcp_server("aads-tools", candidate)
        if diag.get("ok"):
            logger.info(
                "%s_relay_preflight_ok: session=%s mcp_mode=%s command=%s",
                relay_name,
                _short_session(session_id),
                diag.get("path_mode", "unknown"),
                candidate.get("command", ""),
            )
            cleaned = {k: v for k, v in candidate.items() if not str(k).startswith("_")}
            return cleaned, diag, failures
        failures.append(diag)
        logger.warning(
            "%s_relay_preflight_fail: session=%s mcp_mode=%s error_type=%s detail=%s",
            relay_name,
            _short_session(session_id),
            diag.get("path_mode", "unknown"),
            diag.get("error_type", "preflight_failed"),
            (diag.get("detail", "") or "")[:240],
        )
    return None, None, failures


def _format_preflight_failures(failures):
    return ", ".join(
        "{0}[{1}]".format(item.get("path_mode", "unknown"), item.get("error_type", "preflight_failed"))
        for item in (failures or [])
    ) or "no candidate"


def _classify_tool_error(raw_content, session_id="", relay_name="", tool_name="", server_name=""):
    text = str(raw_content or "").strip()
    lowered = text.lower()
    if (
        "user cancelled mcp tool call" in lowered
        or "user canceled mcp tool call" in lowered
        or ('"error"' in lowered and '"cancelled"' in lowered)
    ):
        return {
            "is_error": True,
            "error_type": "session_cancelled_mcp_tool_call",
            "cancel_scope": "session",
            "raw_error": text,
            "content": "session cancelled MCP tool call "
            "(relay={0}, session={1}, server={2}, tool={3})".format(
                relay_name or "unknown",
                _short_session(session_id),
                server_name or "aads-tools",
                tool_name or "unknown",
            ),
        }
    return None


def _extract_tool_result_text(content):
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "output_text")
        ).strip()
    return str(content or "").strip()


def _replace_tool_result_content(content, next_text):
    if isinstance(content, list):
        replaced = False
        blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "output_text") and not replaced:
                next_block = dict(block)
                next_block["text"] = next_text
                blocks.append(next_block)
                replaced = True
            else:
                blocks.append(block)
        if replaced:
            return blocks
        return [{"type": "text", "text": next_text}]
    return next_text


def _annotate_claude_event(event, session_id):
    evt_type = event.get("type", "")
    if evt_type != "user":
        return event
    msg = event.get("message", {}) or {}
    blocks = msg.get("content", [])
    changed = False
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        classification = _classify_tool_error(
            _extract_tool_result_text(block.get("content")),
            session_id=session_id,
            relay_name="claude",
            server_name=block.get("server", ""),
        )
        if not classification:
            continue
        block["is_error"] = True
        block["aads_error_type"] = classification["error_type"]
        block["aads_cancel_scope"] = classification["cancel_scope"]
        block["aads_raw_error"] = classification["raw_error"][:500]
        block["content"] = _replace_tool_result_content(block.get("content"), classification["content"])
        changed = True
        logger.warning(
            "claude_relay_tool_cancel: session=%s error_type=%s raw=%s",
            _short_session(session_id),
            classification["error_type"],
            classification["raw_error"][:240],
        )
    return event if not changed else event


def _build_mcp_config(session_id, template=None):
    template = template if template is not None else _load_mcp_template(session_id)
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
            "aads-server", "python3", "-m", "mcp_servers.aads_tools_bridge"]}}}
    servers = template.get("mcpServers", {})
    for name, cfg in servers.items():
        injected = _inject_session_into_cfg(cfg, safe_sid)
        cfg["args"] = injected.get("args", [])
        cfg["env"] = injected.get("env", {})
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


async def _iter_ndjson_lines(stream, timeout_sec, chunk_size=16384, max_line_size=4 * 1024 * 1024):
    """StreamReader에서 NDJSON 라인을 안전하게 복원한다.

    subprocess stdout에 매우 긴 단일 JSON line이 오면 readline()은 내부 limit(약 64KiB)에 걸려
    `Separator is not found, and chunk exceed the limit`를 발생시킬 수 있다.
    청크 기반으로 읽고 newline을 직접 복원해 Codex/Claude 모두 동일하게 처리한다.
    """
    buffer = bytearray()
    while True:
        chunk = await asyncio.wait_for(stream.read(chunk_size), timeout=timeout_sec)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_line_size:
            raise RuntimeError("NDJSON line exceeds max size (%d bytes)" % max_line_size)

        while True:
            newline_idx = buffer.find(b"\n")
            if newline_idx < 0:
                break
            raw = bytes(buffer[:newline_idx]).strip()
            del buffer[:newline_idx + 1]
            if raw:
                yield raw

    tail = bytes(buffer).strip()
    if tail:
        yield tail


def _parse_codex_tool_event(event, session_id=""):
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
            classification = _classify_tool_error(
                content,
                session_id=session_id,
                relay_name="codex",
                tool_name=item.get("tool", "") or item.get("name", ""),
                server_name=item.get("server", ""),
            )
            if classification:
                content = classification["content"]
        else:
            content_blocks = result.get("content") if isinstance(result, dict) else None
            if isinstance(content_blocks, list):
                content = _extract_text_from_content(content_blocks)
            elif isinstance(result, str):
                content = result
            else:
                content = json.dumps(result, ensure_ascii=False) if result else ""
            is_error = False
            classification = None
        payload = {
            "type": "tool_result",
            "tool_name": item.get("tool", "") or item.get("name", ""),
            "tool_use_id": item.get("id", "") or item.get("call_id", ""),
            "content": content,
            "is_error": is_error,
        }
        if classification:
            payload["error_type"] = classification["error_type"]
            payload["cancel_scope"] = classification["cancel_scope"]
            payload["raw_error"] = classification["raw_error"][:500]
            logger.warning(
                "codex_relay_tool_cancel: session=%s tool=%s raw=%s",
                _short_session(session_id),
                payload.get("tool_name", ""),
                classification["raw_error"][:240],
            )
        return payload

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


def _build_codex_home(session_id, mcp_cfg=None):
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

    mcp_cfg = mcp_cfg if mcp_cfg is not None else _load_mcp_template(session_id)
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

    mcp_config_path = None
    try:
        async with _semaphore:
            claude_meta = _resolve_cli_command("claude")
            claude_preflight = _preflight_cli_command(claude_meta)
            if not claude_preflight.get("ok"):
                logger.error(
                    "claude_relay_preflight_failed: session=%s error_type=%s detail=%s",
                    _short_session(aads_session_id),
                    claude_preflight.get("error_type", "preflight_failed"),
                    claude_preflight.get("detail", ""),
                )
                return web.json_response({
                    "error": "claude_relay_preflight_failed",
                    "error_type": claude_preflight.get("error_type", "preflight_failed"),
                    "detail": claude_preflight.get("detail", ""),
                }, status=503)

            mcp_template = _load_mcp_template(aads_session_id)
            servers = mcp_template.setdefault("mcpServers", {})
            selected_cfg, mcp_diag, mcp_failures = await _resolve_aads_tools_cfg(
                aads_session_id,
                "claude",
                (servers.get("aads-tools", {}) or {}),
            )
            if not selected_cfg:
                failure_text = _format_preflight_failures(mcp_failures)
                logger.error(
                    "claude_relay_mcp_preflight_failed: session=%s failures=%s",
                    _short_session(aads_session_id),
                    failure_text,
                )
                return web.json_response({
                    "error": "relay_mcp_preflight_failed",
                    "error_type": "relay_mcp_preflight_failed",
                    "detail": failure_text,
                }, status=503)
            servers["aads-tools"] = selected_cfg
            mcp_config_path = _build_mcp_config(aads_session_id, template=mcp_template)

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

            cmd = list(claude_meta.get("argv", []) or []) + ["-p", "--output-format", "stream-json", "--verbose",
                   "--model", cli_model, "--mcp-config", mcp_config_path, "--strict-mcp-config",
                   "--allowedTools", "Agent,mcp__aads-tools__*",
                   "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,NotebookEdit",
                   "--max-turns", "200", "--agents", agents_json]
            if use_stream_json_input:
                cmd.extend(["--input-format", "stream-json"])
            if is_resume:
                cmd.extend(["--resume", cli_session_id])

            _stdin_data = stdin_payload if use_stream_json_input else prompt
            logger.info("CLI: model=%s aads=%s cli=%s resume=%s prompt_len=%d cmd_mode=%s mcp_mode=%s",
                        cli_model, aads_session_id[:8] if aads_session_id else "none",
                        cli_session_id[:8] if cli_session_id else "new", is_resume,
                        len(_stdin_data) if _stdin_data else 0,
                        claude_meta.get("mode", "unknown"),
                        (mcp_diag or {}).get("path_mode", "unknown"))

            cli_env = _build_claude_env(token)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=cli_env)

            proc.stdin.write(_stdin_data.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            captured_cli_session_id = None

            try:
                async for raw_line in _iter_ndjson_lines(proc.stdout, timeout_sec=600):
                    line_to_write = raw_line
                    try:
                        event = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    if evt_type == "system" and event.get("subtype") == "init":
                        captured_cli_session_id = event.get("session_id")
                        if _DIRECT_OAUTH_ENABLED:
                            event["claude_auth_mode"] = "direct"
                            event["oauth_slot"] = slot
                        event = _annotate_claude_event(event, aads_session_id)
                        line_to_write = json.dumps(event).encode("utf-8")
                    elif evt_type == "result":
                        if not captured_cli_session_id:
                            captured_cli_session_id = event.get("session_id")
                        if _DIRECT_OAUTH_ENABLED and event.get("is_error"):
                            result_text = str(event.get("result", ""))
                            if re.search(r"429|rate.limit|overloaded|credit", result_text, re.IGNORECASE):
                                _last_429_slot = int(slot) if slot.isdigit() else 0
                                logger.warning("429/rate_limit on slot=%s", slot)
                        event = _annotate_claude_event(event, aads_session_id)
                        line_to_write = json.dumps(event).encode("utf-8")
                    elif evt_type == "user":
                        event = _annotate_claude_event(event, aads_session_id)
                        line_to_write = json.dumps(event).encode("utf-8")
                    await response.write(line_to_write + b"\n")
            except ConnectionResetError:
                logger.info("CLI relay client disconnected: aads=%s resume=%s", aads_session_id[:8], is_resume)
            except asyncio.TimeoutError:
                logger.error("CLI timeout (600s): aads=%s", aads_session_id[:8])
                try:
                    await response.write(json.dumps({"type": "error", "content": "CLI timeout (600s)"}).encode() + b"\n")
                except ConnectionResetError:
                    logger.info("CLI timeout write skipped: client already closed aads=%s", aads_session_id[:8])
                proc.kill()
            except Exception as e:
                logger.error("Stream error: %s", e)
                try:
                    await response.write(json.dumps({"type": "error", "content": str(e)}).encode() + b"\n")
                except ConnectionResetError:
                    logger.info("CLI stream error write skipped: client already closed aads=%s", aads_session_id[:8])
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
                        if proc.returncode not in (None, 0):
                            logger.warning("CLI stderr(exit=%s): %s", proc.returncode, stderr_text[:1200])
                        else:
                            logger.info("CLI stderr: %s", stderr_text[:500])

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
            try:
                await response.write_eof()
            except ConnectionResetError:
                logger.info("CLI write_eof skipped: client already closed aads=%s", aads_session_id[:8])
            return response
    finally:
        if mcp_config_path:
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
    tool_schemas = body.get("tool_schemas", [])
    model = body.get("model", "gpt-5.4")
    session_id = body.get("session_id", "")
    if not messages_text:
        return web.json_response({"error": "messages_text required"}, status=400)
    codex_model = _CODEX_MODEL_MAP.get(model, "gpt-5.4")
    prompt = messages_text
    if system_prompt:
        prompt = "[SYSTEM]\n" + system_prompt + "\n\n[USER]\n" + messages_text
    if tool_schemas:
        tool_lines = []
        for tool in tool_schemas:
            name = str(tool.get("name", "")).strip()
            if not name:
                continue
            description = str(tool.get("description", "")).strip() or "(no description)"
            params = tool.get("params", [])
            if isinstance(params, dict):
                params = list(params.keys())
            elif not isinstance(params, list):
                params = [str(params)] if params else []
            params_text = ", ".join(str(param).strip() for param in params if str(param).strip()) or "(none)"
            tool_lines.append(f"- {name}: {description}\n  params: {params_text}")
        if tool_lines:
            prompt = "[AVAILABLE_AADS_MCP_TOOLS]\n" + "\n".join(tool_lines) + "\n\n" + prompt
    elif tool_names:
        prompt = "[AVAILABLE_AADS_MCP_TOOLS]\n" + ", ".join(tool_names) + "\n\n" + prompt
    try:
        async with _semaphore:
            codex_meta = _resolve_cli_command("codex")
            codex_preflight = _preflight_cli_command(codex_meta)
            if not codex_preflight.get("ok"):
                logger.error(
                    "codex_relay_preflight_failed: session=%s error_type=%s detail=%s",
                    _short_session(session_id),
                    codex_preflight.get("error_type", "preflight_failed"),
                    codex_preflight.get("detail", ""),
                )
                return web.json_response({
                    "error": "codex_relay_preflight_failed",
                    "error_type": codex_preflight.get("error_type", "preflight_failed"),
                    "detail": codex_preflight.get("detail", ""),
                }, status=503)

            mcp_template = _load_mcp_template(session_id)
            servers = mcp_template.setdefault("mcpServers", {})
            selected_cfg, mcp_diag, mcp_failures = await _resolve_aads_tools_cfg(
                session_id,
                "codex",
                (servers.get("aads-tools", {}) or {}),
            )
            if not selected_cfg:
                failure_text = _format_preflight_failures(mcp_failures)
                logger.error(
                    "codex_relay_mcp_preflight_failed: session=%s failures=%s",
                    _short_session(session_id),
                    failure_text,
                )
                return web.json_response({
                    "error": "relay_mcp_preflight_failed",
                    "error_type": "relay_mcp_preflight_failed",
                    "detail": failure_text,
                }, status=503)
            servers["aads-tools"] = selected_cfg

            response = web.StreamResponse(status=200, reason="OK", headers={
                "Content-Type": "application/x-ndjson",
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            await response.prepare(request)
            codex_home = _build_codex_home(session_id, mcp_cfg=mcp_template)
            cmd = list(codex_meta.get("argv", []) or []) + ["exec", "--json", "--full-auto",
                   "--skip-git-repo-check", "-C", "/root/aads/aads-server"]
            if codex_model:
                cmd.extend(["-m", codex_model])
            cmd.append(prompt)
            logger.info(
                "Codex: model=%s prompt_len=%d tools=%d cmd_mode=%s mcp_mode=%s",
                codex_model,
                len(prompt),
                len(tool_names),
                codex_meta.get("mode", "unknown"),
                (mcp_diag or {}).get("path_mode", "unknown"),
            )
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
                async for raw_line in _iter_ndjson_lines(proc.stdout, timeout_sec=300):
                    try:
                        event = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    tool_evt = _parse_codex_tool_event(event, session_id=session_id)
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
            except ConnectionResetError:
                logger.info("Codex relay client disconnected: session=%s", (session_id or "default")[:8])
            except asyncio.TimeoutError:
                logger.error("Codex timeout (300s): model=%s", codex_model)
                try:
                    await response.write(json.dumps({"type": "error", "content": "Codex CLI timeout"}).encode() + b"\n")
                except ConnectionResetError:
                    logger.info("Codex timeout write skipped: client already closed session=%s", (session_id or "default")[:8])
                proc.kill()
            except Exception as e:
                logger.error("Codex stream error: %s", e)
                try:
                    await response.write(json.dumps({"type": "error", "content": str(e)}).encode() + b"\n")
                except ConnectionResetError:
                    logger.info("Codex stream error write skipped: client already closed session=%s", (session_id or "default")[:8])
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
            try:
                await response.write(json.dumps({
                    "type": "result", "result": full_text,
                    "input_tokens": input_tokens, "output_tokens": output_tokens, "model": model,
                }).encode() + b"\n")
            except ConnectionResetError:
                logger.info("Codex result write skipped: client already closed session=%s", (session_id or "default")[:8])
            try:
                await response.write_eof()
            except ConnectionResetError:
                logger.info("Codex write_eof skipped: client already closed session=%s", (session_id or "default")[:8])
            return response
    except Exception as e:
        logger.error("Codex handler error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request):
    health = {"status": "ok", "port": PORT, "sessions": len(_session_map),
              "auth_mode": "direct_oauth" if _DIRECT_OAUTH_ENABLED else "litellm_proxy",
              "claude_cmd_mode": _resolve_cli_command("claude").get("mode", "unknown"),
              "codex_cmd_mode": _resolve_cli_command("codex").get("mode", "unknown"),
              "mcp_bridge_mode": _MCP_BRIDGE_MODE}
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
    _DB_OAUTH_CACHE["rows"] = None
    _DB_OAUTH_CACHE["ts"] = 0
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
