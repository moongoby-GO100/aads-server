"""
AADS: Tool Output Auto-Compression & Observation Masking

Code-only context compression (no LLM calls).
- compress_tool_output: tool별 규칙 기반 출력 압축
- mask_old_observations: 슬라이딩 윈도우 밖 도구 결과 마스킹
- estimate_tokens: 빠른 토큰 추정 (len // 4)
- needs_structured_summary: 임계치 초과 여부 확인
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# ─── Tool Output Compression ────────────────────────────────────────────────

_DEFAULT_TRUNCATE = 2000
_ERROR_PATTERNS = re.compile(
    r"(error|exception|traceback|failed|fatal|critical)",
    re.IGNORECASE,
)


def _looks_like_error(text: str) -> bool:
    """Return True if the output appears to be an error message."""
    first_lines = text[:500]
    return bool(_ERROR_PATTERNS.search(first_lines))


def _compress_health_check(raw: str) -> str:
    """Extract a single status summary line from health_check output."""
    lines = raw.strip().splitlines()
    # Look for a line containing 'status' or build one from key fields
    for line in lines:
        if "status" in line.lower():
            return line.strip()
    # Fallback: grab key=value pairs from the first few lines
    summary_parts: list[str] = []
    for line in lines[:10]:
        stripped = line.strip()
        if ":" in stripped or "=" in stripped:
            summary_parts.append(stripped)
    return " | ".join(summary_parts) if summary_parts else lines[0] if lines else raw


def _compress_read_remote_file(raw: str) -> str:
    """Keep first 80 + last 20 lines, omit the middle."""
    lines = raw.splitlines()
    if len(lines) <= 100:
        return raw
    head = lines[:80]
    tail = lines[-20:]
    omitted = len(lines) - 100
    return "\n".join(head + [f"[...{omitted} lines omitted...]"] + tail)


def _compress_query_database(raw: str) -> str:
    """Keep first 30 rows of tabular output, omit the rest."""
    lines = raw.splitlines()
    if len(lines) <= 32:  # header + separator + 30 rows
        return raw
    # Try to detect header row (first 1-2 lines)
    header_end = min(2, len(lines))
    data_lines = lines[header_end:]
    if len(data_lines) <= 30:
        return raw
    kept = lines[:header_end] + data_lines[:30]
    omitted = len(data_lines) - 30
    return "\n".join(kept + [f"[...{omitted} rows omitted...]"])


def _compress_list_remote_dir(raw: str) -> str:
    """Keep first 50 directory entries."""
    lines = raw.splitlines()
    if len(lines) <= 50:
        return raw
    omitted = len(lines) - 50
    return "\n".join(lines[:50] + [f"[...{omitted} entries omitted...]"])


def _compress_code_results(raw: str) -> str:
    """Keep first 5 results with file:line references only."""
    lines = raw.splitlines()
    kept: list[str] = []
    count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        kept.append(stripped)
        count += 1
        if count >= 5:
            remaining = len([l for l in lines[lines.index(line) + 1:] if l.strip()])
            if remaining:
                kept.append(f"[...{remaining} more results omitted...]")
            break
    return "\n".join(kept) if kept else raw


def _compress_web(raw: str) -> str:
    """Keep first 500 characters of web search / jina_read output."""
    if len(raw) <= 500:
        return raw
    return raw[:500] + f"\n[...truncated, {len(raw) - 500} chars omitted...]"


def _truncate_default(raw: str) -> str:
    """Default truncation at 2000 characters."""
    if len(raw) <= _DEFAULT_TRUNCATE:
        return raw
    return raw[:_DEFAULT_TRUNCATE] + f"\n[...truncated, {len(raw) - _DEFAULT_TRUNCATE} chars omitted...]"


# Tool name -> compressor mapping
_COMPRESSORS: Dict[str, Any] = {
    "health_check": _compress_health_check,
    "read_remote_file": _compress_read_remote_file,
    "query_database": _compress_query_database,
    "list_remote_dir": _compress_list_remote_dir,
    "code_explorer": _compress_code_results,
    "semantic_code_search": _compress_code_results,
    "web_search": _compress_web,
    "web_search_brave": _compress_web,
    "web_search_naver": _compress_web,
    "web_search_kakao": _compress_web,
    "jina_read": _compress_web,
}


def compress_tool_output(tool_name: str, raw_output: str) -> str:
    """Compress a tool's raw output using rule-based, code-only logic.

    Error messages are always preserved in full. Each tool type has its own
    compression strategy; unknown tools are truncated at 2000 chars.

    Args:
        tool_name: Name of the tool that produced the output.
        raw_output: The raw string output from the tool.

    Returns:
        Compressed output string.
    """
    if not raw_output:
        return raw_output

    # Always preserve error output in full
    if _looks_like_error(raw_output):
        return raw_output

    # Match tool name (support prefixed names like "mcp__server__tool")
    base_name = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name

    # Try exact match first, then prefix match for web_search variants
    compressor = _COMPRESSORS.get(base_name)
    if compressor is None:
        for prefix, fn in _COMPRESSORS.items():
            if base_name.startswith(prefix):
                compressor = fn
                break

    if compressor is None:
        compressor = _truncate_default

    try:
        return compressor(raw_output)
    except Exception:
        logger.warning("compress_tool_output failed for %s, using truncation", tool_name)
        return _truncate_default(raw_output)


# ─── Observation Masking ─────────────────────────────────────────────────────

def _count_turns(messages: List[Dict[str, Any]]) -> int:
    """Count the number of user turns in the message list."""
    return sum(1 for m in messages if m.get("role") == "user")


def _extract_tool_name_from_id(tool_use_id: str, messages: List[Dict[str, Any]]) -> str:
    """Find the tool name that corresponds to a given tool_use_id."""
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id") == tool_use_id
                ):
                    return block.get("name", "unknown_tool")
    return "unknown_tool"


def _mask_tool_result_content(
    content: Union[str, List[Dict[str, Any]]],
    tool_name: str,
) -> Union[str, List[Dict[str, Any]]]:
    """Replace tool_result content with a short placeholder."""
    placeholder = f"[도구 결과: {tool_name} — 상세 내용 생략]"
    if isinstance(content, str):
        return placeholder
    if isinstance(content, list):
        return [{"type": "text", "text": placeholder}]
    return placeholder


def mask_old_observations(
    messages: List[Dict[str, Any]],
    window: int = 10,
) -> List[Dict[str, Any]]:
    """Mask tool_result content outside the most recent sliding window.

    Keeps the last ``window`` user turns' tool results intact. For older
    turns, tool_result content is replaced with a short Korean placeholder.
    All assistant and user text content is preserved.

    Args:
        messages: Full conversation message list (Anthropic format).
        window: Number of recent user turns whose tool results to keep.

    Returns:
        New message list with older tool results masked.
    """
    if not messages:
        return messages

    # Identify user-turn boundaries (index of each user message)
    user_indices: list[int] = [
        i for i, m in enumerate(messages) if m.get("role") == "user"
    ]
    total_user_turns = len(user_indices)

    if total_user_turns <= window:
        return messages  # Nothing to mask

    # Cutoff: messages before this index are "old"
    cutoff_idx = user_indices[total_user_turns - window]

    result: list[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i >= cutoff_idx or msg.get("role") != "user":
            # Within window or not a user message -> keep as-is
            result.append(msg)
            continue

        # Old user message: check for tool_result blocks
        content = msg.get("content")

        # String content from user -> always preserve
        if isinstance(content, str):
            result.append(msg)
            continue

        # List content (Anthropic format) -> mask tool_result blocks
        if isinstance(content, list):
            new_content: list[Dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    tool_name = _extract_tool_name_from_id(tool_use_id, messages)
                    masked_block = {**block}
                    masked_block["content"] = _mask_tool_result_content(
                        block.get("content", ""), tool_name,
                    )
                    new_content.append(masked_block)
                else:
                    new_content.append(block)
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)

    return result


# ─── Token Estimation ────────────────────────────────────────────────────────

def _text_from_content(content: Any) -> str:
    """Recursively extract all text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                # tool_use, tool_result, text blocks
                if "text" in item:
                    parts.append(item["text"])
                if "content" in item:
                    parts.append(_text_from_content(item["content"]))
                if "input" in item and isinstance(item["input"], dict):
                    parts.append(json.dumps(item["input"], ensure_ascii=False))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content) if content else ""


def estimate_tokens(
    messages: List[Dict[str, Any]],
    system_prompt: str = "",
) -> int:
    """Quick token estimate using len(text) // 4 heuristic.

    Args:
        messages: Conversation message list.
        system_prompt: System prompt text.

    Returns:
        Estimated total token count.
    """
    total_chars = len(system_prompt)
    for msg in messages:
        total_chars += len(msg.get("role", ""))
        total_chars += len(_text_from_content(msg.get("content", "")))
    return total_chars // 4


def needs_structured_summary(
    messages: List[Dict[str, Any]],
    system_prompt: str = "",
    threshold: int = 80_000,
) -> bool:
    """Check whether estimated tokens exceed the threshold.

    Args:
        messages: Conversation message list.
        system_prompt: System prompt text.
        threshold: Token threshold (default 80,000).

    Returns:
        True if estimated tokens exceed the threshold.
    """
    return estimate_tokens(messages, system_prompt) > threshold
