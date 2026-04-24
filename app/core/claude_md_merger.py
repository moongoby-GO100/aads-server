from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300
_MAX_MERGED_BYTES = 128 * 1024
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _CacheEntry:
    content: str
    sha256: str
    expires_at: float


@dataclass(frozen=True)
class _Section:
    path: str
    content: str


_CACHE: dict[str, _CacheEntry] = {}


def invalidate_claude_md_cache(project: str | None = None) -> None:
    if project is None:
        _CACHE.clear()
        return
    _CACHE.pop(_normalize_project(project), None)


def get_merged_claude_md_sha256(project: str = "AADS") -> str | None:
    entry = _get_valid_cache_entry(_normalize_project(project))
    return entry.sha256 if entry else None


async def build_merged_claude_md(project: str = "AADS") -> str:
    project_key = _normalize_project(project)
    cached = _get_valid_cache_entry(project_key)
    if cached:
        return cached.content

    entry = await asyncio.to_thread(_build_cache_entry, project_key)
    _CACHE[project_key] = entry
    return entry.content


def _normalize_project(project: str) -> str:
    normalized = (project or "AADS").strip().upper()
    return normalized or "AADS"


def _get_valid_cache_entry(project: str) -> _CacheEntry | None:
    cached = _CACHE.get(project)
    if not cached:
        return None
    if cached.expires_at <= time.monotonic():
        _CACHE.pop(project, None)
        return None
    return cached


def _build_cache_entry(project: str) -> _CacheEntry:
    sections = _load_sections(project)
    content = _render_sections(sections)
    content = _truncate_if_needed(content, sections)
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return _CacheEntry(
        content=content,
        sha256=sha256,
        expires_at=time.monotonic() + _CACHE_TTL_SECONDS,
    )


def _load_sections(project: str) -> list[_Section]:
    sections: list[_Section] = []
    for rel_path in _source_paths(project):
        abs_path = _REPO_ROOT / rel_path
        if not abs_path.exists():
            logger.warning("claude_md_source_missing: %s", rel_path.as_posix())
            continue
        if not abs_path.is_file():
            logger.warning("claude_md_source_not_file: %s", rel_path.as_posix())
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("claude_md_source_read_failed: %s (%s)", rel_path.as_posix(), exc)
            continue
        sections.append(_Section(path=rel_path.as_posix(), content=content))
    return sections


def _source_paths(project: str) -> list[Path]:
    paths = [Path("CLAUDE.md")]

    rules_dir = _REPO_ROOT / ".claude" / "rules"
    if rules_dir.exists():
        paths.extend(
            sorted(
                (Path(".claude") / "rules" / path.name)
                for path in rules_dir.glob("*.md")
                if path.is_file()
            )
        )
    else:
        logger.warning("claude_md_rules_dir_missing: %s", Path(".claude/rules").as_posix())

    paths.append(Path("docs") / "knowledge" / f"{project}-KNOWLEDGE.md")
    paths.append(Path("docs") / "shared-lessons" / "INDEX.md")
    return paths


def _render_sections(sections: list[_Section]) -> str:
    rendered: list[str] = []
    for section in sections:
        header = f"# === {section.path} ==="
        body = section.content.rstrip()
        if body:
            rendered.append(f"{header}\n\n{body}")
        else:
            rendered.append(header)
    return "\n\n".join(rendered).rstrip() + ("\n" if rendered else "")


def _truncate_if_needed(content: str, sections: list[_Section]) -> str:
    original_size = _byte_len(content)
    if original_size <= _MAX_MERGED_BYTES:
        return content

    kept_sections = list(sections)
    while len(kept_sections) > 1:
        kept_sections.pop()
        candidate = _with_truncation_marker(original_size, _render_sections(kept_sections))
        if _byte_len(candidate) <= _MAX_MERGED_BYTES:
            return candidate

    remaining = _render_sections(kept_sections)
    return _truncate_text_with_marker(original_size, remaining)


def _with_truncation_marker(original_size: int, body: str) -> str:
    separator = "\n\n" if body else ""
    marker = f"[truncated: {_to_kb(original_size)}KB \u2192 {_to_kb(_byte_len(body))}KB]"
    candidate = f"{marker}{separator}{body}" if body else marker
    marker = f"[truncated: {_to_kb(original_size)}KB \u2192 {_to_kb(_byte_len(candidate))}KB]"
    return f"{marker}{separator}{body}" if body else marker


def _truncate_text_with_marker(original_size: int, body: str) -> str:
    marker = _with_truncation_marker(original_size, "")
    separator = "\n\n" if body else ""
    available_bytes = max(_MAX_MERGED_BYTES - _byte_len(marker) - _byte_len(separator), 0)
    truncated_body = _truncate_to_bytes(body, available_bytes)

    candidate = _with_truncation_marker(original_size, truncated_body)
    while _byte_len(candidate) > _MAX_MERGED_BYTES and truncated_body:
        overflow = _byte_len(candidate) - _MAX_MERGED_BYTES
        truncated_body = _truncate_to_bytes(truncated_body, max(_byte_len(truncated_body) - overflow - 8, 0))
        candidate = _with_truncation_marker(original_size, truncated_body)
    return candidate


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _to_kb(size_bytes: int) -> int:
    return max(1, (size_bytes + 1023) // 1024)
