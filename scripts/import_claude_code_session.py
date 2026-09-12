#!/usr/bin/env python3
"""Claude Code 터미널 대화를 AADS 채팅 자원으로 적재한다.

SessionEnd 훅이 stdin 으로 넘기는 JSON({session_id, transcript_path, cwd})을 읽어
트랜스크립트 JSONL 을 chat_sessions / chat_messages / external_chat_sessions 에 넣는다.

자격증명 마스킹이 전제조건이다. 터미널 대화에는 psql 명령의 PGPASSWORD, 릴레이
공유 시크릿, OAuth 토큰이 그대로 남는다(2026-09-12 실측: 한 세션에서 DB 비밀번호
76행, 릴레이 시크릿 63행, Anthropic 토큰 2행). 마스킹 없이 넣으면 채팅 검색으로
비밀번호가 조회되어 R-KEY 규칙을 정면으로 위반한다.

수동 실행: import_claude_code_session.py <transcript.jsonl> [session_id]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

WORKSPACE_ID = os.getenv("AADS_IMPORT_WORKSPACE_ID", "48cb8821-76b6-4493-9874-7fcb5a751b1a")
TENANT_ID = os.getenv("AADS_IMPORT_TENANT_ID", "2d701a8c-9596-4757-8588-faa4f7837112")
PG_CONTAINER = os.getenv("AADS_PG_CONTAINER", "aads-postgres")
PG_USER = os.getenv("AADS_PG_USER", "aads")
PG_DB = os.getenv("AADS_PG_DB", "aads")
PG_PASSWORD = os.getenv("AADS_PG_PASSWORD", "aads2026secure")

# 본문에 실려서는 안 되는 값들. 값 자체를 지우고 무엇이었는지만 남긴다.
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-o[ar]t01-[A-Za-z0-9_\-]{20,}"), "[REDACTED:anthropic-oauth]"),
    (re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}"), "[REDACTED:anthropic-api]"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "[REDACTED:api-key]"),
    (re.compile(r"(PGPASSWORD=)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(X-Claude-Relay-Secret:\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password[\"'\s:=]+)([^\s\"',;)]{6,})"), r"\1[REDACTED]"),
    (re.compile(r"\b(ghp_|gho_|github_pat_)[A-Za-z0-9_]{20,}"), "[REDACTED:github]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED:private-key]"),
]
# 알려진 평문 비밀번호는 정규식으로 못 잡는 자리에도 나오므로 문자열로 한 번 더 훑는다.
LITERAL_SECRETS = [s for s in (PG_PASSWORD, os.getenv("AADS_EXTRA_SECRET", "")) if len(s) >= 6]


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    for literal in LITERAL_SECRETS:
        text = text.replace(literal, "[REDACTED]")
    return text


def _blocks_to_text(content) -> tuple[str, list[dict]]:
    """message.content → (본문 텍스트, 도구 이벤트)."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    parts: list[str] = []
    tools: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            continue  # 사고 과정은 검색 가치가 낮고 양이 크다
        elif btype == "tool_use":
            tools.append({
                "type": "tool_use",
                "tool_name": str(block.get("name") or ""),
                "tool_use_id": str(block.get("id") or ""),
            })
        elif btype == "tool_result":
            tools.append({
                "type": "tool_result",
                "tool_use_id": str(block.get("tool_use_id") or ""),
                "is_error": bool(block.get("is_error")),
            })
    return "\n".join(p for p in parts if p), tools


def parse_codex_transcript(path: Path) -> list[dict]:
    """Codex rollout-*.jsonl. Claude Code 와 형식이 다르다.

    엔트리는 {type, payload} 이고 대화는 type="response_item",
    payload.type="message" 에 담긴다. role="developer" 는 스킬 안내와
    "Approved command prefix saved" 같은 시스템 주입이라 대화가 아니다.
    """
    rows: list[dict] = []
    for line in path.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        blocks = payload.get("content")
        parts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
                    parts.append(str(block.get("text") or ""))
        text = redact("\n".join(p for p in parts if p).strip())
        # 환경 컨텍스트 주입은 사용자가 쓴 말이 아니다
        if not text or text.startswith("<environment_context>"):
            continue
        rows.append({
            "role": role,
            "content": text,
            "model": "codex" if role == "assistant" else "",
            "tools": [],
            "ts": entry.get("timestamp") or "",
        })
    return rows


def detect_format(path: Path) -> str:
    """첫 유효 엔트리로 형식을 판별한다."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if entry.get("type") in ("session_meta", "response_item", "event_msg"):
                return "codex"
            return "claude-code"
    return "claude-code"


def parse_transcript(path: Path) -> list[dict]:
    """대화만 추린다. mode/bridge-session 같은 런타임 메타는 버린다."""
    rows: list[dict] = []
    for line in path.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        message = entry.get("message") or {}
        text, tools = _blocks_to_text(message.get("content"))
        text = redact(text.strip())
        if not text:
            continue
        rows.append({
            "role": role,
            "content": text,
            "model": str(message.get("model") or "") if role == "assistant" else "",
            "tools": tools,
            "ts": entry.get("timestamp") or "",
        })
    return rows


def psql(sql: str, *, capture: bool = True) -> str:
    cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={PG_PASSWORD}",
           PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-At", "-f", "-"]
    proc = subprocess.run(cmd, input=sql, text=True, capture_output=capture, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[:400])
    return (proc.stdout or "").strip()


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def already_imported(session_id: str) -> bool:
    out = psql(
        "SELECT 1 FROM external_chat_sessions "
        f"WHERE provider IN ('claude-code','codex') AND external_user_id={sql_literal(session_id)} LIMIT 1;"
    )
    return out.strip() == "1"


def main() -> int:
    payload: dict = {}
    if not sys.stdin.isatty():
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (ValueError, TypeError):
            payload = {}

    transcript = sys.argv[1] if len(sys.argv) > 1 else payload.get("transcript_path") or ""
    session_id = sys.argv[2] if len(sys.argv) > 2 else str(payload.get("session_id") or "")

    if not transcript and session_id:
        hits = sorted(Path("/root/.claude/projects").glob(f"*/{session_id}.jsonl"))
        if hits:
            transcript = str(hits[0])
    if not transcript:
        print("no transcript path", file=sys.stderr)
        return 0  # 훅은 세션 종료를 막으면 안 된다

    path = Path(transcript)
    if not path.is_file():
        print(f"transcript missing: {path}", file=sys.stderr)
        return 0
    session_id = session_id or path.stem

    try:
        if already_imported(session_id):
            print(f"already imported: {session_id}")
            return 0
        fmt = detect_format(path)
        rows = parse_codex_transcript(path) if fmt == "codex" else parse_transcript(path)
        if not rows:
            print("no conversational rows")
            return 0

        aads_session = str(uuid.uuid4())
        first_user = next((r["content"] for r in rows if r["role"] == "user"), "terminal session")
        title = (first_user.splitlines()[0] or "terminal session")[:120]

        stmts = [
            "BEGIN;",
            "INSERT INTO chat_sessions (id, workspace_id, tenant_id, title, message_count) VALUES ("
            f"{sql_literal(aads_session)}::uuid, {sql_literal(WORKSPACE_ID)}::uuid, "
            f"{sql_literal(TENANT_ID)}::uuid, {sql_literal(title)}, {len(rows)});",
        ]
        for row in rows:
            stmts.append(
                "INSERT INTO chat_messages (session_id, role, content, model_used, intent, tools_called) VALUES ("
                f"{sql_literal(aads_session)}::uuid, {sql_literal(row['role'])}, "
                f"{sql_literal(row['content'])}, "
                f"{sql_literal(row['model']) if row['model'] else 'NULL'}, "
                "'claude_code_terminal', "
                f"{sql_literal(json.dumps(row['tools'], ensure_ascii=False))}::jsonb);"
            )
        meta = json.dumps({
            "source_file": str(path),
            "format": fmt,
            "cwd": payload.get("cwd") or "",
            "entries": len(rows),
            "redacted": True,
        }, ensure_ascii=False)
        stmts.append(
            "INSERT INTO external_chat_sessions "
            "(provider, service, external_user_id, display_name, aads_session_id, tenant_id, metadata) VALUES ("
            f"{sql_literal(fmt)}, 'terminal', {sql_literal(session_id)}, "
            f"{sql_literal('Codex CLI' if fmt == 'codex' else 'Claude Code')}, "
            f"{sql_literal(aads_session)}::uuid, {sql_literal(TENANT_ID)}::uuid, {sql_literal(meta)}::jsonb);"
        )
        stmts.append("COMMIT;")
        psql("\n".join(stmts))
        print(f"imported {len(rows)} messages -> {aads_session}")
    except Exception as exc:  # 훅은 어떤 경우에도 세션 종료를 방해하지 않는다
        print(f"import failed: {str(exc)[:300]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
