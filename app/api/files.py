"""
AADS-FILES: 채팅 산출물 파일 열람·다운로드 API (2026-08-18).

문제
  모델이 생성한 파일을 `/root/aads/aads-server/보고서.xlsx` 같은 파일시스템 절대경로로
  안내하면 브라우저가 이를 사이트 경로로 해석해 `https://aads.newtalk.kr/root/...` 404가 난다.
  결과적으로 CEO 화면에서 열리지도, 내려받아지지도 않았다.

해결
  경로 표기가 호스트/컨테이너/상대 어떤 형태이든 실제 파일을 찾아 스트리밍한다.
  - 호스트(/root/aads/aads-server/...) ↔ 컨테이너(/app/...) 별칭 변환
  - 직접 경로로 못 찾으면 표준 산출물 디렉터리에서 파일명으로 재탐색 (레거시 링크 구제)
  - 허용 루트 밖 경로와 민감 파일(.env/secrets/id_rsa/*.key/*.pem)은 차단

엔드포인트
  GET /api/v1/files/download?path=...&inline=0  → 첨부 다운로드(기본) / inline=1 이면 브라우저 표시
  GET /api/v1/files/meta?path=...               → 존재 여부·크기·MIME 사전 확인
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter()
logger = structlog.get_logger()

# 컨테이너에서 읽을 수 있고 외부 노출이 허용된 루트만 화이트리스트로 유지한다.
ALLOWED_ROOTS: tuple[str, ...] = (
    "/app/docs",
    "/app/reports",
    "/app/app/static",
    "/app/generated-media-static",
    "/root/aads/aads-docs",
    "/root/aads/aads-dashboard/docs",
    "/root/aads/aads-dashboard/reports",
    "/root/aads/aads-dashboard/public",
    "/root/aads/aads-core",
    "/root/project-docs",
    "/var/www/certbot/exports",
    "/tmp/aads_exports",
)

# 채팅에서 만든 파일의 표준 저장 위치 (호스트 /root/aads/aads-server/app/static/exports 와 동일 실체)
EXPORT_DIR = Path(os.getenv("AADS_EXPORT_DIR", "/app/app/static/exports"))

# 경로가 어긋난 링크를 파일명으로 되찾기 위한 탐색 경로
FALLBACK_DIRS: tuple[Path, ...] = (
    EXPORT_DIR,
    Path("/app/app/static/reports"),
    Path("/app/app/static/docs"),
    Path("/app/reports"),
    Path("/app/docs"),
    Path("/root/aads/aads-dashboard/public/exports"),
    Path("/root/aads/aads-dashboard/public/reports"),
    Path("/var/www/certbot/exports"),
    Path("/tmp/aads_exports"),
)

HOST_ALIASES: tuple[tuple[str, str], ...] = (
    ("/root/aads/aads-server/app/", "/app/app/"),
    ("/root/aads/aads-server/", "/app/"),
)

CONTAINER_ALIASES: tuple[tuple[str, str], ...] = (
    ("/app/app/", "/root/aads/aads-server/app/"),
    ("/app/", "/root/aads/aads-server/"),
)

# 저장소 루트 바로 아래 산출물 허용 (소스 유출 방지를 위해 확장자 제한)
REPO_ROOT_DIRS = ("/app", "/root/aads/aads-server")
REPO_ROOT_SUFFIXES = {
    ".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".hwp", ".hwpx", ".zip", ".md", ".txt", ".png", ".jpg", ".jpeg",
}

SENSITIVE_PARTS = (".env", ".ssh", ".git", "secrets", "credentials", "id_rsa", "vault")
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}

INLINE_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
    ".txt", ".md", ".csv", ".json", ".log", ".html", ".htm",
}

MAX_BYTES = 200 * 1024 * 1024  # 200MB

try:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:  # pragma: no cover - 권한 문제 시에도 API는 동작해야 한다
    logger.warning("files_export_dir_init_failed", path=str(EXPORT_DIR), error=str(e))


def _clean_path(raw: str) -> str:
    """URL/따옴표/라인번호/앵커가 섞인 입력을 순수 파일 경로로 정리한다."""
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        return ""
    value = unquote(value)
    if value.startswith("http://") or value.startswith("https://"):
        value = urlparse(value).path
        value = unquote(value)
    for sep in ("#", "?"):
        if sep in value:
            value = value.split(sep, 1)[0]
    # trailing ":123" (코드 링크 라인번호) 제거
    head, sep, tail = value.rpartition(":")
    if sep and tail.isdigit() and head:
        value = head
    return value.replace("\\", "/").strip()


def _is_sensitive(path: Path) -> bool:
    lowered = str(path).lower()
    if any(marker in lowered for marker in SENSITIVE_PARTS):
        return True
    return path.suffix.lower() in SENSITIVE_SUFFIXES


def _is_allowed(path: Path) -> bool:
    resolved = str(path)
    if any(resolved == root or resolved.startswith(root.rstrip("/") + "/") for root in ALLOWED_ROOTS):
        return True
    # 채팅 세션이 저장소 루트에 바로 만든 산출물(xlsx/pdf 등)도 열람 가능해야 한다.
    # 소스코드 유출을 막기 위해 "루트 바로 아래 + 문서/오피스 확장자"로만 한정한다.
    if str(path.parent) in REPO_ROOT_DIRS and path.suffix.lower() in REPO_ROOT_SUFFIXES:
        return True
    return False


def _candidates(cleaned: str) -> list[Path]:
    """호스트/컨테이너/상대 경로 표기를 모두 후보로 만든다."""
    out: list[str] = []

    def _add(value: str) -> None:
        if value and value not in out:
            out.append(value)

    _add(cleaned)
    for host_prefix, container_prefix in HOST_ALIASES:
        if cleaned.startswith(host_prefix):
            _add(container_prefix + cleaned[len(host_prefix):])
    for container_prefix, host_prefix in CONTAINER_ALIASES:
        if cleaned.startswith(container_prefix):
            _add(host_prefix + cleaned[len(container_prefix):])

    if not cleaned.startswith("/"):
        rel = cleaned.lstrip("./")
        _add(f"/app/{rel}")
        _add(f"/root/aads/aads-server/{rel}")
        for base in FALLBACK_DIRS:
            _add(f"{base}/{rel}")

    return [Path(p) for p in out]


def _resolve(raw: str) -> Path:
    cleaned = _clean_path(raw)
    if not cleaned:
        raise HTTPException(400, "path 파라미터가 비어 있습니다")
    if ".." in cleaned.split("/"):
        raise HTTPException(400, "잘못된 경로입니다 (상위 경로 탐색 차단)")

    checked: list[str] = []
    for candidate in _candidates(cleaned):
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        checked.append(str(resolved))
        if not resolved.is_file():
            continue
        if _is_sensitive(resolved):
            raise HTTPException(403, "민감 파일은 제공하지 않습니다")
        if _is_allowed(resolved):
            return resolved

    # 마지막 구제책: 표준 산출물 디렉터리에서 같은 파일명 재탐색 (레거시 경로 링크 대응)
    name = Path(cleaned).name
    if name:
        for base in FALLBACK_DIRS:
            candidate = (base / name)
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved.is_file() and _is_allowed(resolved) and not _is_sensitive(resolved):
                logger.info("files_resolved_by_name", requested=cleaned, resolved=str(resolved))
                return resolved

    logger.warning("files_not_found", requested=cleaned, checked=checked[:6])
    raise HTTPException(404, f"파일을 찾을 수 없습니다: {cleaned}")


def _media_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


@router.get("/files/meta", summary="파일 존재/크기/MIME 확인", tags=["files"])
async def file_meta(path: str = Query(..., description="파일 경로 (호스트/컨테이너/상대 모두 허용)")):
    resolved = _resolve(path)
    stat = resolved.stat()
    suffix = resolved.suffix.lower()
    return {
        "status": "ok",
        "name": resolved.name,
        "resolved_path": str(resolved),
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
        "mime_type": _media_type(resolved),
        "inline_capable": suffix in INLINE_SUFFIXES,
        "download_url": f"/api/v1/files/download?path={resolved}",
    }


@router.get("/files/download", summary="파일 다운로드/열람", tags=["files"])
async def file_download(
    path: str = Query(..., description="파일 경로 (호스트/컨테이너/상대 모두 허용)"),
    inline: int = Query(0, ge=0, le=1, description="1이면 브라우저에서 바로 표시"),
    filename: str | None = Query(None, description="다운로드 파일명 지정 (선택)"),
):
    resolved = _resolve(path)
    size = resolved.stat().st_size
    if size > MAX_BYTES:
        raise HTTPException(413, f"파일이 너무 큽니다 ({size // (1024 * 1024)}MB > 200MB)")

    suffix = resolved.suffix.lower()
    disposition = "inline" if (inline == 1 and suffix in INLINE_SUFFIXES) else "attachment"
    download_name = Path(filename).name if filename else resolved.name

    logger.info("files_download", path=str(resolved), size=size, disposition=disposition)
    return FileResponse(
        str(resolved),
        media_type=_media_type(resolved),
        filename=download_name,
        content_disposition_type=disposition,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
