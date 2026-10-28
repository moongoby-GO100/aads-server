"""파일 스킬(SKILL.md) 본문 읽기 전용 로더 (AADS-SKILL-INSTRUCTION-LOADER-20261028).

파일 읽기만 한다. 스크립트·하위 파일은 실행하지도 읽지도 않는다.
스캔 루트는 ohvis_harness.scan_repository_skills() 와 같다(.claude/skills, .codex/skills).
"""
from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.services.skill_file_import import (
    infer_risk_tier,
    parse_allowed_tools,
    parse_frontmatter,
    repo_root,
)

SKILL_ROOTS = (".claude/skills", ".codex/skills")
MAX_INSTRUCTION_CHARS = 60_000
MAX_CANDIDATES = 5
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_FRONTMATTER_STRIP_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": code, "message": message, **extra}


def _skill_roots(roots: Iterable[Path] | None) -> list[Path]:
    if roots is not None:
        return [Path(r) for r in roots]
    base = repo_root()
    return [base / rel for rel in SKILL_ROOTS]


def _available_slugs(roots: list[Path]) -> list[str]:
    slugs: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob("*/SKILL.md"):
            if _SLUG_RE.fullmatch(path.parent.name):
                slugs.add(path.parent.name)
    return sorted(slugs)


def _similar_slugs(slug: str, available: list[str]) -> list[str]:
    close = difflib.get_close_matches(slug, available, n=MAX_CANDIDATES, cutoff=0.5)
    partial = [s for s in available if slug and (slug in s or s in slug)]
    return list(dict.fromkeys(close + partial))[:MAX_CANDIDATES]


def _first_h1(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def load_skill_instructions(slug: str, *, roots: Iterable[Path] | None = None) -> dict[str, Any]:
    """slug 의 SKILL.md 본문(frontmatter 제외)을 돌려준다. 실패는 {"error": 코드, ...} 로 돌려준다.

    ``roots`` 는 테스트용 재정의다. 운영 호출은 지정하지 않는다.
    """
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        return _error("invalid_slug", "slug 는 소문자·숫자·하이픈(최대 64자)만 허용합니다")

    root_list = _skill_roots(roots)
    for root in root_list:
        try:
            root_real = root.resolve()
            if not root_real.is_dir():
                continue
            path = root_real / slug / "SKILL.md"
            real = path.resolve()
        except OSError:
            continue
        if not path.is_file():
            continue
        if not real.is_relative_to(root_real):
            return _error("skill_path_escape", "스킬 경로가 스캔 루트 밖을 가리켜 거부했습니다")
        try:
            raw = real.read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        front = parse_frontmatter(text)
        body = _FRONTMATTER_STRIP_RE.sub("", text, count=1)
        risk_tier, risk_reasons = infer_risk_tier(front, text)
        truncated = len(body) > MAX_INSTRUCTION_CHARS
        source = f"{root.parent.name}/{root.name}/{slug}/SKILL.md"
        return {
            "slug": slug,
            "title": _first_h1(body, slug),
            "status": str(front.get("status") or "candidate"),
            "risk_tier": risk_tier,
            "risk_reasons": risk_reasons,
            "allowed_tools": parse_allowed_tools(front),
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "source": source,
            "instructions": body[:MAX_INSTRUCTION_CHARS],
            "truncated": truncated,
        }

    candidates = _similar_slugs(slug, _available_slugs(root_list))
    return _error(
        "skill_not_found", f"파일 스킬 {slug} 을 찾을 수 없습니다", candidates=candidates,
    )
