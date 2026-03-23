"""AADS-195: 파일 작업 (목록, 읽기, 쓰기)."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 보안: 접근 차단 경로
_BLOCKED_PATHS = [
    "C:\\Windows\\System32",
    "C:\\Windows\\SysWOW64",
]


def _is_blocked(path: str) -> bool:
    """차단 경로 확인."""
    norm = os.path.normpath(path).lower()
    return any(norm.startswith(b.lower()) for b in _BLOCKED_PATHS)


async def file_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """디렉토리 파일 목록."""
    path = params.get("path", "C:\\")
    if _is_blocked(path):
        return {"status": "error", "data": {"error": f"접근 차단 경로: {path}"}}
    try:
        entries = []
        for entry in os.scandir(path):
            entries.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else 0,
            })
        # 디렉토리 우선, 이름순 정렬
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"status": "success", "data": {"files": entries[:100], "path": path}}
    except PermissionError:
        return {"status": "error", "data": {"error": f"접근 권한 없음: {path}"}}
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def file_read(params: Dict[str, Any]) -> Dict[str, Any]:
    """파일 내용 읽기 (텍스트, 최대 50KB)."""
    path = params.get("path", "")
    if not path:
        return {"status": "error", "data": {"error": "파일 경로가 비어있습니다."}}
    if _is_blocked(path):
        return {"status": "error", "data": {"error": f"접근 차단 경로: {path}"}}
    try:
        size = os.path.getsize(path)
        if size > 50 * 1024:
            return {"status": "error", "data": {"error": f"파일이 너무 큽니다: {size} bytes (최대 50KB)"}}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"status": "success", "data": {"content": content, "path": path, "size": size}}
    except FileNotFoundError:
        return {"status": "error", "data": {"error": f"파일을 찾을 수 없습니다: {path}"}}
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}


async def file_write(params: Dict[str, Any]) -> Dict[str, Any]:
    """파일 쓰기."""
    path = params.get("path", "")
    content = params.get("content", "")
    if not path:
        return {"status": "error", "data": {"error": "파일 경로가 비어있습니다."}}
    if _is_blocked(path):
        return {"status": "error", "data": {"error": f"접근 차단 경로: {path}"}}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "data": {"path": path, "size": len(content)}}
    except Exception as e:
        return {"status": "error", "data": {"error": str(e)}}
