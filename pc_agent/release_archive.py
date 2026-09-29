"""Reproducible PC archive with a publish-time release check."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from functools import lru_cache
from pathlib import Path

# Moved unchanged from kakao_bot so both the route and release gate share them.
ZIP_EXCLUDE_DIRS = {"__pycache__", ".git", "build_tmp", "dist", ".mypy_cache", ".pytest_cache"}
ZIP_EXCLUDE_EXTS = {".pyc", ".pyo", ".exe", ".spec"}
ZIP_EXCLUDE_SUFFIXES = {".bak_aads"}
MANIFEST_NAME = "RELEASE_ZIP_SHA256.json"


def _source_files(agent_dir: Path) -> list[str]:
    # Path ordering is case-insensitive on Windows. Sort canonical archive names,
    # not native Path objects, so the manifest and ZIP match on every OS.
    return sorted(path.relative_to(agent_dir).as_posix() for path in agent_dir.rglob("*")
            if path.is_file() and not any(part in ZIP_EXCLUDE_DIRS for part in path.relative_to(agent_dir).parts)
            and path.suffix not in ZIP_EXCLUDE_EXTS
            and not any(path.name.endswith(s) for s in ZIP_EXCLUDE_SUFFIXES)
            and not path.name.startswith("RESULT_") and path.name != MANIFEST_NAME)


def _release_files(agent_dir: Path) -> list[str]:
    manifest = agent_dir / MANIFEST_NAME
    if not manifest.exists():
        return _source_files(agent_dir)  # Existing temporary test fixtures.
    files = json.loads(manifest.read_text(encoding="utf-8")).get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) and f for f in files):
        raise ValueError("PC ZIP manifest file list is invalid")
    return files


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    # ZipInfo defaults to DOS on Windows and Unix elsewhere; this byte is part
    # of the central directory and therefore part of the published SHA256.
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def build_agent_zip(agent_dir: Path, install_ticket: str | None = None) -> bytes:
    buf = io.BytesIO()
    # STORED entries do not vary with zlib or CPython compression versions.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name in _release_files(agent_dir):
            path = agent_dir / name
            if not path.resolve().is_relative_to(agent_dir.resolve()):
                raise ValueError("Invalid PC ZIP manifest path")
            info = _zip_info(name)
            zf.writestr(info, path.read_bytes())
        if install_ticket:
            info = _zip_info("install_ticket.txt")
            zf.writestr(info, install_ticket)
    return buf.getvalue()


@lru_cache(maxsize=8)
def cached_agent_zip(agent_dir: Path) -> tuple[bytes, str]:
    """One ZIP and digest per immutable release directory, per worker."""
    data = build_agent_zip(agent_dir)
    return data, hashlib.sha256(data).hexdigest()


def ticketed_agent_zip(agent_dir: Path, ticket: str | None = None) -> bytes:
    data, _ = cached_agent_zip(agent_dir)
    if not ticket:
        return data
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_STORED) as zf:
        info = _zip_info("install_ticket.txt")
        zf.writestr(info, ticket)
    return buf.getvalue()


def release_sha256(agent_dir: Path) -> str:
    """Check tracked source at publication, never on an HTTP request."""
    manifest = json.loads((agent_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    version = (agent_dir / "VERSION").read_text(encoding="utf-8").strip()
    digest = manifest.get("sha256")
    if (manifest.get("version") != version or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or manifest.get("files") != _source_files(agent_dir)):
        raise ValueError("PC ZIP release manifest is invalid or source files differ")
    if hashlib.sha256(build_agent_zip(agent_dir)).hexdigest() != digest:
        raise ValueError("PC ZIP differs from the published release manifest")
    return digest


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent
    if sys.argv[1:] == ["write"]:
        value = {"version": (root / "VERSION").read_text(encoding="utf-8").strip(),
                 "files": _source_files(root), "sha256": ""}
        (root / MANIFEST_NAME).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        value["sha256"] = hashlib.sha256(build_agent_zip(root)).hexdigest()
        (root / MANIFEST_NAME).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    elif sys.argv[1:] == ["check"]:
        print(release_sha256(root))
    else:
        raise SystemExit("usage: python3 -m pc_agent.release_archive write|check")
