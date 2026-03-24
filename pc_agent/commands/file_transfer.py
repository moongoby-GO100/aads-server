"""AADS: 파일 전송 — 서버↔PC 파일 업로드/다운로드/상태확인."""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 기본 최대 파일 크기 (MB)
_DEFAULT_MAX_SIZE_MB = 10

# 보안: 차단 경로
_BLOCKED_PATHS = [
    "C:\\Windows\\System32",
    "C:\\Windows\\SysWOW64",
]


def _validate_path(path: str) -> str | None:
    """경로 검증. 문제 있으면 에러 메시지 반환, 정상이면 None."""
    if not path:
        return "경로가 비어있습니다."
    # path traversal 방지
    if ".." in path:
        return "경로에 '..'은 허용되지 않습니다."
    # 절대경로 또는 홈디렉토리 기준 상대경로 허용
    if not os.path.isabs(path):
        path = os.path.join(os.path.expanduser("~"), path)
    norm = os.path.normpath(path).lower()
    for blocked in _BLOCKED_PATHS:
        if norm.startswith(blocked.lower()):
            return f"접근 차단 경로: {path}"
    return None


def _resolve_path(path: str) -> str:
    """상대경로를 홈디렉토리 기준 절대경로로 변환."""
    if not os.path.isabs(path):
        return os.path.join(os.path.expanduser("~"), path)
    return path


async def file_upload(params: Dict[str, Any]) -> Dict[str, Any]:
    """PC 파일을 base64로 읽어서 반환 (PC→서버 전송용)."""
    path = params.get("path", "")
    max_size_mb = int(params.get("max_size_mb", _DEFAULT_MAX_SIZE_MB))

    err = _validate_path(path)
    if err:
        return {"status": "error", "data": {"error": err}}

    path = _resolve_path(path)
    try:
        if not os.path.isfile(path):
            return {"status": "error", "data": {"error": f"파일을 찾을 수 없습니다: {path}"}}

        size = os.path.getsize(path)
        max_bytes = max_size_mb * 1024 * 1024
        if size > max_bytes:
            return {"status": "error", "data": {
                "error": f"파일 크기 초과: {size:,} bytes (최대 {max_size_mb}MB)",
                "size": size,
                "max_bytes": max_bytes,
            }}

        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")

        return {"status": "success", "data": {
            "path": path,
            "size": size,
            "data": data,
            "encoding": "base64",
        }}
    except PermissionError:
        return {"status": "error", "data": {"error": f"접근 권한 없음: {path}"}}
    except Exception as e:
        logger.error("file_upload error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def file_download(params: Dict[str, Any]) -> Dict[str, Any]:
    """base64 데이터를 PC에 파일로 저장 (서버→PC 전송용)."""
    path = params.get("path", "")
    data = params.get("data", "")
    overwrite = params.get("overwrite", False)

    if not data:
        return {"status": "error", "data": {"error": "data(base64) 파라미터 필수"}}

    err = _validate_path(path)
    if err:
        return {"status": "error", "data": {"error": err}}

    path = _resolve_path(path)
    try:
        if not overwrite and os.path.exists(path):
            return {"status": "error", "data": {
                "error": f"파일이 이미 존재합니다: {path} (overwrite=true로 덮어쓰기 가능)",
            }}

        # 디렉토리 자동 생성
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        raw = base64.b64decode(data)
        with open(path, "wb") as f:
            f.write(raw)

        return {"status": "success", "data": {
            "path": path,
            "size": len(raw),
        }}
    except base64.binascii.Error:
        return {"status": "error", "data": {"error": "유효하지 않은 base64 데이터입니다."}}
    except PermissionError:
        return {"status": "error", "data": {"error": f"접근 권한 없음: {path}"}}
    except Exception as e:
        logger.error("file_download error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def file_sync_status(params: Dict[str, Any]) -> Dict[str, Any]:
    """파일 존재 여부 + 크기 + 수정시간 확인."""
    path = params.get("path", "")

    err = _validate_path(path)
    if err:
        return {"status": "error", "data": {"error": err}}

    path = _resolve_path(path)
    try:
        if not os.path.exists(path):
            return {"status": "success", "data": {
                "path": path,
                "exists": False,
            }}

        stat = os.stat(path)
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return {"status": "success", "data": {
            "path": path,
            "exists": True,
            "is_file": os.path.isfile(path),
            "is_dir": os.path.isdir(path),
            "size": stat.st_size,
            "modified": mtime,
        }}
    except PermissionError:
        return {"status": "error", "data": {"error": f"접근 권한 없음: {path}"}}
    except Exception as e:
        logger.error("file_sync_status error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
