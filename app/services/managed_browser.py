"""Managed browser profile helpers for PC Agent work sessions."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROFILE_ROOT = Path("/root/aads/runtime/managed-browser-profiles")


def normalize_work_key(work_key: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (work_key or "").strip()).strip("-")
    if not cleaned:
        raise ValueError("work_key_required")
    return cleaned[:120]


def normalize_origin(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def profile_key(work_key: str, target_url: str = "") -> str:
    normalized = normalize_work_key(work_key)
    origin = normalize_origin(target_url) or (target_url or "").strip()
    return f"{normalized}:{origin}"


def profile_info(work_key: str, target_url: str = "") -> dict[str, Any]:
    normalized = normalize_work_key(work_key)
    origin = normalize_origin(target_url)
    digest = hashlib.sha256(profile_key(normalized, target_url).encode("utf-8")).hexdigest()[:12]
    profile_dir = PROFILE_ROOT / f"{normalized}-{digest}"
    return {
        "work_key": normalized,
        "target_url": target_url,
        "origin": origin,
        "profile_key": profile_key(normalized, target_url),
        "profile_dir": str(profile_dir),
        "isolated_profile": True,
    }
