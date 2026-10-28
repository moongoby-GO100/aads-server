"""SKILL.md 파일 스킬의 파싱·해시·드리프트 판정 (CAT-1/2/3 공용, DB 비의존)."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RISK_TIER = "read"
CANONICAL_MARK = "file_skill"
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_skill_dirs() -> list[Path]:
    dirs = [repo_root() / ".claude" / "skills", Path.home() / ".claude" / "skills",
            Path("/root/.claude/skills")]
    return list(dict.fromkeys(d.resolve() for d in dirs))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def scan_skill_files(dirs: Iterable[Path] | None = None) -> list[dict[str, Any]]:
    """`<dir>/<name>/SKILL.md` 를 훑어 slug 별 첫 항목만 돌려준다 (앞선 디렉터리 우선)."""
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for base in dirs if dirs is not None else default_skill_dirs():
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*/SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                digest = sha256_file(path)
            except OSError:
                continue
            front = parse_frontmatter(text)
            slug = _clean(front.get("name")) or path.parent.name
            if slug in seen:
                continue
            seen.add(slug)
            entries.append({
                "slug": slug,
                "title": slug,
                "description": _clean(front.get("description")),
                "source_path": str(path),
                "content_sha256": digest,
                "risk_tier": DEFAULT_RISK_TIER,
                "enabled": True,
            })
    return entries


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def plan_import(entries: Iterable[Mapping[str, Any]],
                existing: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """slug 기준 멱등 계획. 기존 행의 내용은 절대 덮어쓰지 않는다.

    action: insert(없음) | unchanged(해시 동일) | drift(해시 불일치 → file_drift 만 true)
            | resync_flag(해시가 다시 일치 → file_drift false 로 복구) | skip_db_origin(파일 출처 아님)
    """
    plan: list[dict[str, Any]] = []
    for entry in entries:
        row = existing.get(entry["slug"])
        if row is None:
            plan.append({**entry, "action": "insert"})
            continue
        meta = _metadata_dict(row.get("metadata"))
        stored = meta.get("content_sha256")
        if not meta.get("source_path") or not stored:
            plan.append({**entry, "action": "skip_db_origin"})
        elif stored != entry["content_sha256"]:
            plan.append({**entry, "action": "drift", "stored_sha256": stored})
        elif meta.get("file_drift"):
            plan.append({**entry, "action": "resync_flag", "stored_sha256": stored})
        else:
            plan.append({**entry, "action": "unchanged"})
    return plan


def compute_file_drift(metadata: Any, *, path_reader=sha256_file) -> bool:
    """저장된 해시와 현재 파일 해시를 비교한다. 파일을 읽을 수 없으면 저장된 플래그를 쓴다."""
    meta = _metadata_dict(metadata)
    source_path = meta.get("source_path")
    stored = meta.get("content_sha256")
    if not source_path:
        return False
    if stored:
        try:
            return path_reader(Path(str(source_path))) != stored
        except OSError:
            pass
    return bool(meta.get("file_drift"))
