"""
프로젝트별 문서 통합 조회 API.
프로젝트 서버의 docs, reports 디렉토리를 스캔하여
프로젝트별로 분류된 문서 목록과 내용을 제공한다.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import html
import io
import json
import mimetypes
import os
import re
import shlex
import stat
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query

# 승인 결정은 **사람만** 부른다 — 테넌트 멤버 인증을 요구한다.
from typing import Any as _Any
from app.auth import TenantRole, require_tenant_role

TenantContext = dict[str, _Any]
require_tenant_member = require_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])

router = APIRouter()
logger = structlog.get_logger()

# ── 캐시 ──
_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5분
PERSISTENT_CACHE_FILE = Path(os.getenv("PROJECT_DOCS_CACHE_FILE", "/tmp/aads_project_docs_cache.json"))

# Public education-report discovery is intentionally isolated from the broad
# project document scanner below. Keep both the directory and filename policy
# server-controlled so this endpoint cannot become an arbitrary file browser.
PUBLIC_EDUCATION_REPORTS_DIR = Path("/app/app/static/reports")
PUBLIC_EDUCATION_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*_education\.html$"
)

# ── 서버/프로젝트 경로 매핑 ──
SERVER_CONFIG = {
    "AADS": {
        "host": None,  # 로컬
        "paths": [
            {"base": "/app/docs", "label": "서버 문서"},
            {"base": "/app/reports", "label": "서버 리포트"},
            {"base": "/root/aads/aads-server/docs", "label": "서버 문서"},
            {"base": "/root/aads/aads-server/reports", "label": "서버 리포트"},
            {"base": "/root/aads/aads-docs/docs", "label": "공용 문서"},
            {"base": "/root/aads/aads-docs/reports", "label": "공용 리포트", "exclude": ["ceo-documents/_index.json"]},
            {"base": "/root/aads/aads-dashboard/docs", "label": "대시보드 문서"},
            {"base": "/root/aads/aads-dashboard/reports", "label": "대시보드 리포트"},
            {"base": "/root/aads/aads-dashboard/src", "label": "대시보드 소스"},
            {"base": "/root/aads/aads-core/docs", "label": "코어 문서"},
            {"base": "/root/aads/aads-core/reports", "label": "코어 리포트"},
            {"base": "/app/app", "label": "서버 앱 소스"},
            {"base": "/app/app/static/docs", "label": "정적 문서"},
            {"base": "/app/app/static/reports", "label": "정적 리포트"},
            {"base": "/app/app/static/preview", "label": "프리뷰"},
            {"base": "/app/app/static/gallery", "label": "갤러리"},
            {"base": "/root/aads/aads-dashboard/public/reports", "label": "대시보드 공개 리포트"},
            {"base": "/root/aads/aads-dashboard/public/exports", "label": "대시보드 내보내기"},
        ],
    },
    "KIS": {
        "host": "contabo14",
        "paths": [
            {"base": "/root/kis-autotrade-v4/docs", "label": "문서",
             "exclude": ["kis-api-portal", "GO100", "go100"]},
        ],
    },
    "GO100": {
        "host": "contabo14",
        "paths": [
            {"base": "/root/kis-autotrade-v4/report", "label": "리포트"},
            {"base": "/root/kis-autotrade-v4/reports", "label": "리포트"},
            {"base": "/root/kis-autotrade-v4/artifacts/go100", "label": "GO100 산출물",
             "include": ["latest.md", "report", "summary", "audit", "plan", ".html"]},
            {"base": "/root/kis-autotrade-v4/docs/go100", "label": "문서"},
            {"base": "/root/kis-autotrade-v4/docs/technical", "label": "기술문서"},
            {"base": "/root/kis-autotrade-v4/docs/reports", "label": "문서 리포트"},
            {"base": "/root/kis-autotrade-v4/docs/plans", "label": "기획문서"},
            {"base": "/root/kis-autotrade-v4/docs/plan", "label": "기획문서"},
            {"base": "/root/kis-autotrade-v4/docs/api", "label": "API 문서"},
            {"base": "/root/kis-autotrade-v4/docs/handover", "label": "인수인계"},
            {"base": "/root/kis-autotrade-v4/docs/operations", "label": "운영문서"},
            {"base": "/root/kis-autotrade-v4/docs/architecture", "label": "아키텍처"},
            {"base": "/root/kis-autotrade-v4/docs/design", "label": "설계문서"},
            {"base": "/root/kis-autotrade-v4/docs/agenda", "label": "아젠다"},
            {"base": "/root/kis-autotrade-v4/docs/features", "label": "기능명세"},
            {"base": "/root/kis-autotrade-v4/docs/analysis", "label": "분석문서"},
            {"base": "/root/kis-autotrade-v4/docs/whitepapers", "label": "백서"},
            {"base": "/root/kis-autotrade-v4/docs", "label": "문서",
             "include": ["GO100", "go100"],
             "exclude": [
                 "go100/", "technical/", "reports/", "plans/", "plan/", "api/",
                 "handover/", "operations/", "architecture/", "design/",
                 "agenda/", "features/", "analysis/", "whitepapers/",
                 "kis-api-portal/",
             ]},
        ],
    },
    "SF": {
        "host": "cafe24_114",
        "paths": [
            {"base": "/data/shortflow/docs", "label": "서비스 문서"},
        ],
    },
    "NTV2": {
        "host": "cafe24_114",
        "paths": [
            {"base": "/srv/newtalk-v2/docs", "label": "서비스 문서"},
        ],
    },
}

EXTENSIONS = {
    # 문서/리포트
    ".md", ".txt", ".html", ".htm", ".rst", ".pdf",
    # 데이터
    ".json", ".yaml", ".yml", ".toml", ".xml", ".csv",
    # 코드
    ".py", ".sh", ".sql", ".js", ".ts", ".tsx", ".jsx", ".css",
    # 설정/로그
    ".ini", ".cfg", ".conf", ".log",
    # 이미지 (브라우저 표시 가능)
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    # 오피스 문서 (다운로드 안내)
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp",
    # 일반 첨부/아카이브/미디어 (직접 링크 열람 및 다운로드 폴백)
    ".xls", ".xlsm", ".doc", ".ppt",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm",
}

# 바이너리 처리 대상 (텍스트로 읽지 않고 base64/raw)
BINARY_EXTENSIONS = {
    ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".docx", ".xlsx", ".xls", ".xlsm", ".pptx", ".odt", ".ods", ".odp",
    ".doc", ".ppt",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm",
}

BASE64_PREVIEW_EXTENSIONS = {
    ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
}

EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
DOCX_EXTENSIONS = {".docx"}
POWERPOINT_EXTENSIONS = {".pptx", ".odp"}
XML_OFFICE_EXTENSIONS = {".docx", ".pptx", ".odt", ".ods", ".odp"}
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls", ".ppt"}
SENSITIVE_PATH_MARKERS = (".env", "secrets", "credentials", "id_rsa")
SENSITIVE_EXTENSIONS = {".key", ".pem"}
AADS_APP_ROOT = "/app"
AADS_APP_REL_PREFIXES = ("docs/", "reports/", "app/", "migrations/", "scripts/", "tests/")

LOCAL_BASE_ALIASES = {
    "/app": ["/app", "/root/aads/aads-server"],
    "/app/app": ["/app/app", "/root/aads/aads-server/app"],
    "/app/docs": ["/app/docs", "/root/aads/aads-server/docs"],
    "/app/reports": ["/app/reports", "/root/aads/aads-server/reports"],
    "/app/app/static/docs": ["/app/app/static/docs", "/root/aads/aads-server/app/static/docs"],
    "/app/app/static/reports": ["/app/app/static/reports", "/root/aads/aads-server/app/static/reports"],
    "/app/app/static/preview": ["/app/app/static/preview", "/root/aads/aads-server/app/static/preview"],
    "/app/app/static/gallery": ["/app/app/static/gallery", "/root/aads/aads-server/app/static/gallery"],
}

PROJECT_FILE_HINTS = [
    ("GO100", re.compile(r"^(GO100[-_]|GO100\b|#?\d+.*GO100|.*상한가|.*백억)", re.IGNORECASE)),
    ("KIS", re.compile(r"^(KIS[-_]|KIS\b|.*자동매매)", re.IGNORECASE)),
    ("SF", re.compile(r"^(SF[-_]|ShortFlow\b|.*shortflow|.*숏폼)", re.IGNORECASE)),
    ("NTV2", re.compile(r"^(NTV2[-_]|NT[-_]|NewTalk\b|.*newtalk)", re.IGNORECASE)),
]

LEGACY_AADS_PROJECT_BASES = {
    "GO100": {
        "/app/docs": "/root/kis-autotrade-v4/docs",
        "/app/reports": "/root/kis-autotrade-v4/reports",
    },
    "KIS": {
        "/app/docs": "/root/kis-autotrade-v4/docs",
        "/app/reports": "/root/kis-autotrade-v4/docs",
    },
    "SF": {
        "/app/docs": "/data/shortflow/docs",
        "/app/reports": "/data/shortflow/docs",
    },
    "NTV2": {
        "/app/docs": "/srv/newtalk-v2/docs",
        "/app/reports": "/srv/newtalk-v2/docs",
    },
}


def _configured_base_paths(project: str) -> set[str]:
    config = SERVER_CONFIG.get(project) or {}
    paths = {str(Path(path_cfg["base"])) for path_cfg in config.get("paths", [])}
    if project == "AADS":
        paths.add(AADS_APP_ROOT)
    return paths


def _is_safe_relative_path(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return False
    parts = [part.lower() for part in normalized.split("/") if part]
    if any(part in SENSITIVE_PATH_MARKERS or part.startswith(".env") or part.startswith("id_rsa") for part in parts):
        return False
    if any(marker in normalized.lower() for marker in ("secrets", "credentials")):
        return False
    return Path(normalized).suffix.lower() not in SENSITIVE_EXTENSIONS


def _candidate_local_bases(base_path: str) -> list[Path]:
    normalized = str(Path(base_path))
    return [Path(p) for p in LOCAL_BASE_ALIASES.get(normalized, [normalized])]


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _project_hint_from_file_path(file_path: str) -> Optional[str]:
    name = _basename(file_path)
    for project, pattern in PROJECT_FILE_HINTS:
        if pattern.search(name):
            return project
    return None


def _candidate_file_paths_for_base(base_path: str, file_path: str) -> list[str]:
    normalized = file_path.replace("\\", "/").lstrip("/")
    candidates = [normalized]
    if normalized.startswith("docs/reports/"):
        candidates.append(normalized.removeprefix("docs/"))
        candidates.append(normalized.removeprefix("docs/reports/"))
    if normalized.startswith("reports/"):
        candidates.append(normalized.removeprefix("reports/"))
    elif base_path.endswith("/docs") or base_path.endswith("/app/docs"):
        candidates.append(f"reports/{normalized}")
    return list(dict.fromkeys([item for item in candidates if item]))


def _content_location_candidates(project: str, base_path: str, file_path: str) -> list[tuple[str, str, str]]:
    normalized_project = project.strip()
    normalized_base = str(Path(base_path))
    normalized_file = file_path.replace("\\", "/").lstrip("/")
    candidates: list[tuple[str, str, str]] = [(normalized_project, normalized_base, normalized_file)]

    hinted_project = _project_hint_from_file_path(normalized_file)
    if normalized_project == "AADS" and hinted_project and normalized_base in {"/app/docs", "/app/reports"}:
        hinted_base = LEGACY_AADS_PROJECT_BASES.get(hinted_project, {}).get(normalized_base)
        if hinted_base:
            candidates.append((hinted_project, hinted_base, normalized_file))

    # Older chat replies normalized every relative ``docs/...`` or ``reports/...``
    # path as an AADS link, even when the active workspace was GO100/KIS/SF/NTV2.
    # A filename hint repairs branded names (for example GO100-*.md), but generic
    # names such as PRD-WAVE-ENGINE-MODULARIZATION.md have no project prefix. Keep
    # the originally requested AADS location first, then try only the equivalent
    # allowlisted document root for each external project. This makes legacy links
    # recoverable without enabling arbitrary cross-project path traversal.
    if normalized_project == "AADS" and normalized_base in {"/app/docs", "/app/reports"}:
        fallback_projects = list(LEGACY_AADS_PROJECT_BASES)
        if hinted_project in fallback_projects:
            fallback_projects.remove(hinted_project)
            fallback_projects.insert(0, hinted_project)
        for fallback_project in fallback_projects:
            fallback_base = LEGACY_AADS_PROJECT_BASES[fallback_project].get(normalized_base)
            if fallback_base:
                candidates.append((fallback_project, fallback_base, normalized_file))

    projects = [normalized_project]
    if hinted_project and hinted_project not in projects:
        projects.append(hinted_project)

    for candidate_project in projects:
        for candidate_base in sorted(_configured_base_paths(candidate_project), key=len, reverse=True):
            for candidate_file in _candidate_file_paths_for_base(candidate_base, normalized_file):
                candidates.append((candidate_project, candidate_base, candidate_file))

    deduped: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _matches_path_filters(rel_path: str, *, include: list[str] | None, exclude: list[str] | None) -> bool:
    """Return whether a scanned relative path should be shown in 문서현황."""
    normalized = rel_path.replace("\\", "/")
    if exclude and any(ex in normalized for ex in exclude):
        return False
    if include and not any(inc in normalized for inc in include):
        return False
    return True


def _resolve_local_file(project: str, base_path: str, file_path: str) -> Path:
    normalized_base = str(Path(base_path))
    allowed = _configured_base_paths(project)
    if normalized_base not in allowed:
        raise HTTPException(400, "Unsupported base_path")
    if not _is_safe_relative_path(file_path):
        raise HTTPException(400, "Invalid file path")
    normalized_file = file_path.replace("\\", "/")
    if project == "AADS" and normalized_base == AADS_APP_ROOT and not normalized_file.startswith(AADS_APP_REL_PREFIXES):
        raise HTTPException(403, "File path is not allowed under /app")

    rel = Path(normalized_file)
    first_candidate: Path | None = None
    for base in _candidate_local_bases(normalized_base):
        base_resolved = base.resolve()
        candidate = (base_resolved / rel).resolve()
        try:
            candidate.relative_to(base_resolved)
        except ValueError:
            raise HTTPException(400, "Invalid file path")
        if first_candidate is None:
            first_candidate = candidate
        if candidate.exists() and candidate.is_file():
            return candidate

    return first_candidate or (Path(normalized_base) / rel)


async def _remote_file_exists(host: str, full_path: str) -> bool:
    quoted_path = shlex.quote(full_path)
    output = await _run_cmd(
        ["ssh", "-o", "ConnectTimeout=5", host, f"test -f {quoted_path} && printf exists"],
        timeout=8,
    )
    return output.strip() == "exists"


async def _resolve_content_location(project: str, base_path: str, file_path: str) -> tuple[str, str, str, str]:
    for candidate_project, candidate_base, candidate_file in _content_location_candidates(project, base_path, file_path):
        if not _is_safe_relative_path(candidate_file):
            continue
        candidate_config = SERVER_CONFIG.get(candidate_project)
        if not candidate_config:
            continue
        normalized_base = str(Path(candidate_base))
        if normalized_base not in _configured_base_paths(candidate_project):
            continue

        host = candidate_config["host"]
        full_path = f"{normalized_base.rstrip('/')}/{candidate_file}"
        if host is None:
            try:
                local_path = _resolve_local_file(candidate_project, normalized_base, candidate_file)
            except HTTPException:
                continue
            if local_path.exists() and local_path.is_file():
                return candidate_project, normalized_base, candidate_file, str(local_path)
        elif await _remote_file_exists(host, full_path):
            return candidate_project, normalized_base, candidate_file, full_path

    normalized_base = str(Path(base_path))
    normalized_file = file_path.replace("\\", "/").lstrip("/")
    return project, normalized_base, normalized_file, f"{normalized_base.rstrip('/')}/{normalized_file}"


def _excel_bytes_to_csv_text(raw: bytes, filename: str) -> str:
    try:
        from openpyxl import load_workbook
    except Exception as e:
        raise RuntimeError(f"openpyxl unavailable: {e}") from e

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            buf = io.StringIO()
            writer = csv.writer(buf)
            for row in ws.iter_rows(values_only=True):
                writer.writerow(["" if cell is None else str(cell) for cell in row])
            parts.append(f"## Sheet: {sheet_name}\n{buf.getvalue().rstrip()}")
    finally:
        wb.close()
    return f"# {filename} CSV preview\n\n" + "\n\n".join(parts)


def _docx_bytes_to_text(raw: bytes, filename: str) -> str:
    try:
        import docx
    except Exception as e:
        logger.warning("project_doc_python_docx_unavailable", filename=filename, error=str(e))
        return _zip_office_bytes_to_text(raw, filename, ".docx")

    doc = docx.Document(io.BytesIO(raw))
    paragraphs = [p.text for p in doc.paragraphs if p.text]
    return f"# {filename} text preview\n\n" + "\n".join(paragraphs)


def _xml_text_nodes(raw_xml: bytes) -> list[str]:
    text = raw_xml.decode("utf-8", errors="replace")
    nodes = re.findall(r"<[^>/!:]+:?t(?:\s[^>]*)?>(.*?)</[^>:]+:?t>", text, flags=re.DOTALL)
    if not nodes:
        nodes = re.findall(r"<text:[^>]+>(.*?)</text:[^>]+>", text, flags=re.DOTALL)
    if not nodes:
        stripped = re.sub(r"<[^>]+>", " ", text)
        nodes = [stripped]
    values: list[str] = []
    for node in nodes:
        value = re.sub(r"<[^>]+>", " ", node)
        value = html.unescape(value)
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            values.append(value)
    return values


def _zip_office_bytes_to_text(raw: bytes, filename: str, ext: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            parts: list[str] = []
            if ext == ".pptx":
                slide_names = sorted(
                    [name for name in names if re.match(r"ppt/slides/slide\d+\.xml$", name)],
                    key=lambda value: int(re.search(r"slide(\d+)\.xml$", value).group(1)),  # type: ignore[union-attr]
                )
                for index, name in enumerate(slide_names, start=1):
                    slide_text = _xml_text_nodes(zf.read(name))
                    if slide_text:
                        parts.append(f"## Slide {index}\n" + "\n".join(slide_text))
            elif ext == ".docx":
                doc_names = [
                    name for name in names
                    if name == "word/document.xml"
                    or re.match(r"word/(header|footer)\d+\.xml$", name)
                ]
                for name in doc_names:
                    parts.extend(_xml_text_nodes(zf.read(name)))
            elif ext in {".odt", ".ods", ".odp"} and "content.xml" in names:
                parts.extend(_xml_text_nodes(zf.read("content.xml")))
            if not parts:
                raise ValueError("No previewable Office XML text found")
            label = {
                ".docx": "Word",
                ".pptx": "PowerPoint",
                ".odt": "OpenDocument Text",
                ".ods": "OpenDocument Sheet",
                ".odp": "OpenDocument Presentation",
            }.get(ext, "Office")
            preview = "\n\n".join(parts)
            return f"# {filename} {label} preview\n\n{preview[:200_000]}"
    except Exception as e:
        raise RuntimeError(f"Office XML preview failed: {e}") from e


def _legacy_office_bytes_to_text(raw: bytes, filename: str) -> str:
    chunks: list[str] = []
    seen: set[str] = set()

    for encoding in ("utf-16le", "utf-8", "cp949", "latin-1"):
        decoded = raw.decode(encoding, errors="ignore")
        for match in re.findall(r"[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]{4,}", decoded):
            value = re.sub(r"\s+", " ", match).strip()
            if len(value) < 4 or value in seen:
                continue
            if sum(ch.isalnum() or "\uac00" <= ch <= "\ud7a3" for ch in value) < 3:
                continue
            seen.add(value)
            chunks.append(value[:500])
            if len(chunks) >= 300:
                break
        if len(chunks) >= 300:
            break

    if not chunks:
        raise RuntimeError("No readable legacy Office text found")
    return (
        f"# {filename} legacy Office text preview\n\n"
        "구형 Office 바이너리에서 추출한 텍스트 미리보기입니다. 원본 서식은 다운로드 파일에서 확인하십시오.\n\n"
        + "\n".join(f"- {chunk}" for chunk in chunks)
    )


def _office_preview_format(ext: str) -> str:
    if ext in {".xlsx", ".xlsm", ".ods", ".xls"}:
        return "excel-csv" if ext in {".xlsx", ".xlsm"} else "office-text"
    if ext in {".docx", ".odt", ".doc"}:
        return "word-text"
    if ext in {".pptx", ".odp", ".ppt"}:
        return "powerpoint-text"
    return "office-text"


def _office_preview_response(
    *,
    project: str,
    file_path: str,
    full_path: str,
    content: str,
    source_mime_type: str,
    ext: str,
) -> dict:
    return {
        "project": project,
        "file_path": file_path,
        "full_path": full_path,
        "content": content,
        "size": len(content),
        "encoding": "text",
        "mime_type": "text/plain",
        "is_binary": False,
        "source_mime_type": source_mime_type,
        "converted_from": ext.lstrip("."),
        "format": _office_preview_format(ext),
    }


def _load_persistent_cache() -> Optional[dict]:
    """프로세스 재시작 후에도 이전 문서 목록을 즉시 재사용한다."""
    if _cache["data"]:
        return _cache["data"]
    try:
        if not PERSISTENT_CACHE_FILE.exists():
            return None
        data = json.loads(PERSISTENT_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("status") != "ok":
            return None
        _cache["data"] = data
        _cache["ts"] = int(data.get("scanned_at") or 0)
        return data
    except Exception as e:
        logger.warning("project_docs_cache_load_failed", path=str(PERSISTENT_CACHE_FILE), error=str(e))
        return None


def _save_persistent_cache(data: dict) -> None:
    """스캔 결과를 파일 캐시에 저장한다. 실패해도 API 응답은 유지한다."""
    try:
        PERSISTENT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PERSISTENT_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("project_docs_cache_save_failed", path=str(PERSISTENT_CACHE_FILE), error=str(e))


def _file_key(doc: dict) -> tuple[str, str]:
    return (doc.get("base_path", ""), doc.get("path", ""))


def _file_signature(doc: dict) -> tuple[int, int]:
    return (int(doc.get("size") or 0), int(doc.get("modified") or 0))


def _previous_project(previous: Optional[dict], project: str) -> Optional[dict]:
    if not previous:
        return None
    for item in previous.get("projects") or []:
        if item.get("project") == project:
            return item
    return None


def _attach_delta(current: dict, previous: Optional[dict]) -> dict:
    """기존 목록과 비교해 이번 스캔에서 실제 변경된 파일 수를 표시한다."""
    prev_files = previous.get("files", []) if previous else []
    prev_map = {_file_key(doc): doc for doc in prev_files}
    curr_map = {_file_key(doc): doc for doc in current.get("files", [])}

    new_count = 0
    updated_count = 0
    unchanged_count = 0
    for key, doc in curr_map.items():
        prev_doc = prev_map.get(key)
        if not prev_doc:
            new_count += 1
        elif _file_signature(prev_doc) != _file_signature(doc):
            updated_count += 1
        else:
            unchanged_count += 1

    current["delta"] = {
        "new": new_count,
        "updated": updated_count,
        "removed": max(0, len(prev_map) - len(curr_map.keys() & prev_map.keys())),
        "unchanged": unchanged_count,
    }
    return current


async def _run_cmd(cmd: list[str], timeout: float = 10) -> str:
    """subprocess 실행 헬퍼."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace") if stdout else ""
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("cmd_failed", cmd=cmd[:3], error=str(e))
        return ""


async def _scan_local(base: str, exclude: list[str] | None = None, include: list[str] | None = None) -> list[dict]:
    """로컬 파일시스템 스캔."""
    results = []
    base_path = Path(base)
    if not base_path.exists():
        return results
    for p in sorted(base_path.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in EXTENSIONS:
            continue
        rel = str(p.relative_to(base_path))
        if not _matches_path_filters(rel, include=include, exclude=exclude):
            continue
        stat = p.stat()
        results.append({
            "name": p.name,
            "path": rel,
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "type": _classify(p.name, rel),
            "format": _detect_format(p.name),
        })
    return results


async def _scan_remote(host: str, base: str, exclude: list[str] | None = None, include: list[str] | None = None) -> list[dict]:
    """SSH로 원격 서버 스캔."""
    ext_pattern = " -o ".join(f'-name "*.{ext.lstrip(".")}"' for ext in EXTENSIONS)
    find_cmd = f'find {base} -type f \\( {ext_pattern} \\) -printf "%P\\t%s\\t%T@\\n" 2>/dev/null'
    output = await _run_cmd(["ssh", "-o", "ConnectTimeout=5", host, find_cmd], timeout=15)
    if not output.strip():
        return []
    results = []
    for line in output.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel_path, size_str, mtime_str = parts[0], parts[1], parts[2]
        name = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path
        if not _matches_path_filters(rel_path, include=include, exclude=exclude):
            continue
        results.append({
            "name": name,
            "path": rel_path,
            "size": int(size_str) if size_str.isdigit() else 0,
            "modified": int(float(mtime_str)) if mtime_str else 0,
            "type": _classify(name, rel_path),
            "format": _detect_format(name),
        })
    return sorted(results, key=lambda x: x["path"])


def _classify(name: str, path: str) -> str:
    """문서 유형 분류."""
    nl = name.lower()
    pl = path.lower()

    if any(key in pl for key in ("contract", "contracts", "agreement", "agreements", "계약")) or \
            any(key in nl for key in ("contract", "agreement", "계약", "근로계약", "입점계약", "프리랜서")):
        return "contract"
    if any(key in pl for key in ("ceo-documents", "directive", "directives", "policy", "rule")) or \
            any(key in nl for key in ("directive", "directives", "policy", "rules")):
        return "directive"
    if "handover" in nl or "handover" in pl:
        return "handover"
    if any(key in pl for key in ("changelog", "release-note", "release_note", "history")) or \
            any(key in nl for key in ("changelog", "release-note", "release_note", "history")):
        return "changelog"
    if any(key in pl for key in ("report", "result", "retrospective", "postmortem")) or \
            any(key in nl for key in ("report", "result", "retrospective", "postmortem")):
        return "report"
    if any(key in pl for key in ("qa", "test", "verification", "benchmark")) or \
            any(key in nl for key in ("qa", "test", "verification", "benchmark")):
        return "qa"
    if any(key in pl for key in ("api", "openapi", "swagger")) or \
            any(key in nl for key in ("api", "openapi", "swagger")):
        return "api"
    if any(key in pl for key in ("architecture", "system-design", "design", "technical", "tech")) or \
            any(key in nl for key in ("architecture", "design", "technical", "tech")):
        return "architecture"
    if any(key in pl for key in ("runbook", "deploy", "deployment", "operation", "ops", "playbook", "troubleshoot")) or \
            any(key in nl for key in ("runbook", "deploy", "deployment", "operation", "ops", "playbook", "troubleshoot")):
        return "runbook"
    if any(key in pl for key in ("plan", "roadmap", "proposal", "spec", "prd", "layout")) or \
            any(key in nl for key in ("plan", "roadmap", "proposal", "spec", "prd")):
        return "plan"
    if any(key in pl for key in ("status", "incident", "issue", "summary")) or \
            any(key in nl for key in ("status", "incident", "issue", "summary")):
        return "status"
    if any(key in pl for key in ("lesson", "knowledge", "guide", "manual", "faq", "tutorial")) or \
            any(key in nl for key in ("lesson", "knowledge", "guide", "manual", "faq", "tutorial")):
        return "knowledge"
    if nl.endswith(".sql") or any(key in pl for key in ("schema", "migration", "erd", "ddl")) or \
            any(key in nl for key in ("schema", "migration", "erd", "ddl")):
        return "schema"
    if nl.endswith((".py", ".sh")):
        return "script"
    if nl.endswith((".json", ".yaml", ".yml")) or \
            any(key in pl for key in ("config", "settings", "compose", "env")) or \
            any(key in nl for key in ("config", "settings", "compose", "env")):
        return "config"
    return "doc"


def _detect_format(name: str) -> str:
    """파일 확장자 기반 포맷 분류."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    _FORMAT_MAP = {
        # 문서
        "md": "markdown", "txt": "text", "html": "html", "htm": "html", "rst": "rst", "pdf": "pdf",
        # 데이터
        "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml", "xml": "xml", "csv": "csv",
        # 코드
        "py": "python", "sh": "shell", "sql": "sql",
        "js": "javascript", "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
        "css": "css", "ini": "config", "cfg": "config", "conf": "config", "log": "log",
        # 이미지
        "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
        "svg": "image", "webp": "image", "bmp": "image", "ico": "image",
        # 오피스
        "docx": "word", "doc": "word",
        "xlsx": "excel", "xls": "excel", "xlsm": "excel",
        "pptx": "powerpoint", "ppt": "powerpoint",
        "odt": "word", "ods": "excel", "odp": "powerpoint",
    }
    return _FORMAT_MAP.get(ext, "other")


async def _scan_project(project: str, config: dict, previous: Optional[dict] = None) -> dict:
    """프로젝트 1개 스캔."""
    host = config["host"]
    all_docs = []
    for path_cfg in config["paths"]:
        base = path_cfg["base"]
        exclude = path_cfg.get("exclude")
        include = path_cfg.get("include")
        label = path_cfg["label"]
        if host is None:
            docs = await _scan_local(base, exclude, include)
        else:
            docs = await _scan_remote(host, base, exclude, include)
        for d in docs:
            d["base_path"] = base
            d["label"] = label
            d["full_path"] = f"{base.rstrip('/')}/{d['path']}"
        all_docs.extend(docs)

    # 같은 실파일이 여러 base_path에서 중복 노출되지 않도록 정규화 dedupe
    deduped = []
    seen = set()
    for d in all_docs:
        key = (d.get("base_path", ""), d.get("path", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)
    return _attach_delta({
        "project": project,
        "host": host or "localhost",
        "total": len(deduped),
        "files": deduped,
    }, previous)


def _education_report_date(basename: str, modified_at: float) -> str:
    """Return a stable display date, preferring a valid YYYYMMDD filename prefix."""
    prefix = basename[:8]
    if len(basename) > 8 and basename[8] in {"_", "-"} and prefix.isdigit():
        try:
            return datetime.strptime(prefix, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(modified_at, tz=timezone.utc).date().isoformat()


def _education_report_title(basename: str) -> str:
    """Derive a safe human-readable title from an allowlisted basename."""
    title = re.sub(r"^\d{8}[_-]", "", basename)
    title = title.removesuffix("_education.html")
    return re.sub(r"[_-]+", " ", title).strip()


def _list_public_education_reports(reports_dir: Path | None = None) -> list[dict]:
    """List safe metadata for direct, regular education-report files only."""
    directory = reports_dir or PUBLIC_EDUCATION_REPORTS_DIR
    reports: list[tuple[str, int, dict]] = []

    try:
        entries = directory.iterdir()
        for entry in entries:
            basename = entry.name
            if not PUBLIC_EDUCATION_FILENAME_RE.fullmatch(basename):
                continue
            try:
                file_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue

            date = _education_report_date(basename, file_stat.st_mtime)
            reports.append((date, file_stat.st_mtime_ns, {
                "basename": basename,
                "title": _education_report_title(basename),
                "date": date,
                "size": file_stat.st_size,
            }))
    except OSError as exc:
        logger.warning("public_education_index_unavailable", error=type(exc).__name__)
        return []

    reports.sort(key=lambda item: (-int(item[0].replace("-", "")), -item[1], item[2]["basename"]))
    return [metadata for _, _, metadata in reports]


@router.get("/project-docs/public-education-index")
async def public_education_index():
    """Return the public, read-only education-report index."""
    return {"documents": _list_public_education_reports()}


@router.get("/project-docs/scan")
async def scan_all_docs(force: bool = Query(False, description="캐시 무시하고 재스캔")):
    """전 서버 문서 스캔.

    - 일반 호출: 5분 메모리 캐시 우선, 프로세스 재시작 후에는 파일 캐시 우선.
    - 강제 호출: 기존 캐시와 비교해 new/updated/removed만 delta로 표시한다.
    """
    now = time.time()
    previous = _load_persistent_cache()
    if not force and previous and (now - _cache["ts"]) < CACHE_TTL:
        resp = dict(previous)
        resp["cache_hit"] = True
        resp["cache_age_sec"] = int(now - _cache["ts"])
        resp["cache_mode"] = "memory" if _cache["data"] is previous else "file"
        return resp

    tasks = [_scan_project(proj, cfg, _previous_project(previous, proj)) for proj, cfg in SERVER_CONFIG.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    projects = []
    total = 0
    for r in results:
        if isinstance(r, Exception):
            logger.error("scan_error", error=str(r))
            continue
        projects.append(r)
        total += r["total"]

    resp = {
        "status": "ok",
        "total": total,
        "projects": projects,
        "scanned_at": int(now),
        "cache_hit": False,
        "cache_mode": "incremental" if previous else "full",
        "delta": {
            "new": sum((p.get("delta") or {}).get("new", 0) for p in projects),
            "updated": sum((p.get("delta") or {}).get("updated", 0) for p in projects),
            "removed": sum((p.get("delta") or {}).get("removed", 0) for p in projects),
            "unchanged": sum((p.get("delta") or {}).get("unchanged", 0) for p in projects),
        },
    }
    _cache["data"] = resp
    _cache["ts"] = now
    _save_persistent_cache(resp)
    return resp


@router.get("/approvals/pending")
async def approvals_pending(
    limit: int = Query(50, ge=1, le=200),
    session_id: str = Query("", max_length=64, description="이 세션이 올린 요청만"),
):
    """CEO 승인 대기 목록.

    2026-09-14 CEO 지시 — "실매매조건은 나의 승인후 진행해야지".
    실매매 경로를 바꾸려는 도구 호출은 `live_trading_guard` 가 실행 전에
    막고 여기에 요청을 남긴다.
    """
    from app.core.db_pool import get_pool

    try:
        rows = await get_pool().fetch(
            """
            SELECT r.id::text, r.action_type, r.action_summary, r.risk_level,
                   r.gate_source, r.tier, r.requested_by, r.work_key,
                   -- 붙을 버블이 화면에 없으면 **없다고 답한다.** 그래야 화면이
                   -- 이 카드를 팝업·하단 카드로 되돌린다.
                   --
                   -- 2026-09-18. 붙을 자리가 사라진 카드(메시지가 지워졌거나
                   -- 숨김 메시지에 붙은 카드)는 인라인으로도 안 뜨고,
                   -- source_message_id 가 있다는 이유로 팝업에서도 빠져서
                   -- 어느 화면에도 없었다 — 48시간 11장.
                   CASE
                     WHEN m.id IS NULL THEN NULL
                     WHEN m.is_hidden IS TRUE THEN NULL
                     WHEN m.deleted_at IS NOT NULL THEN NULL
                     ELSE r.source_message_id::text
                   END AS source_message_id,
                   to_char(r.created_at AT TIME ZONE 'Asia/Seoul', 'MM-DD HH24:MI') AS at,
                   r.decision,
                   GREATEST(0, EXTRACT(EPOCH FROM (r.expires_at - now()))::int / 60)
                       AS expires_in_min
            FROM agent_permission_requests r
            LEFT JOIN chat_messages m ON m.id = r.source_message_id
            WHERE r.decision = 'pending' AND r.tier = 'approve'
              AND r.expires_at > now()
              AND ($2 = '' OR r.requested_by = $2)
            ORDER BY CASE r.risk_level WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                       ELSE 2 END,
                     r.created_at DESC
            LIMIT $1
            """,
            limit, session_id,
        )
    except Exception as exc:
        logger.warning("approvals_pending_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="승인 목록을 읽지 못했습니다") from exc

    return {
        "pending": [
            {"id": r["id"], "tool": r["action_type"], "summary": r["action_summary"],
             "risk": r["risk_level"], "gate_source": r["gate_source"],
             "tier": r["tier"], "requested_by": r["requested_by"],
             "work_key": r["work_key"], "at": r["at"],
             "expires_in_min": r["expires_in_min"],
             # 어느 응답 버블 아래에 붙일 카드인가. 없으면 화면은 기존대로
             # 팝업으로만 띄운다 — 붙일 자리를 모르는 카드를 아무 데나
             # 붙이면 회장님이 다른 답변의 제안을 승인하시게 된다.
             "source_message_id": r["source_message_id"],
             # 승인 UI 가 그대로 그릴 수 있게 선택지를 서버가 내려준다.
             # 화면마다 다른 규칙을 적어 두면 한쪽이 반드시 낡는다.
             #
             # 제안 카드(next_step)는 "이걸 할까요" 한 건이다. 도구 카드와
             # 같은 선택지를 주면 1회성 제안에 "최대 20회" 권한이 붙는다
             # (2026-09-15 실측 — 제안 승인 문구에 그대로 찍혀 나왔다).
             #
             # 담당 세션 생성(goal_owner)도 1회성이다. **한 번 만들면 끝**
             # 이므로 "이 대화 동안 최대 50회" 같은 반복 권한이 붙을 자리가
             # 없다. 붙으면 승인 한 번에 채팅창이 계속 늘어난다 — 대표님이
             # 모르게 늘어나지 않는다는 원칙이 그대로 무너진다(2026-09-17).
             "choices": (
                 [
                     {"key": "single", "label": "세션 생성 승인",
                      "params": {"decision": "approved", "scope": "single", "hours": 2}},
                     {"key": "reject", "label": "거절",
                      "params": {"decision": "rejected"}},
                 ] if r["gate_source"] == "goal_owner" else (
                 [
                     {"key": "single", "label": "지금 실행",
                      "params": {"decision": "approved", "scope": "single", "hours": 2}},
                     {"key": "reject", "label": "거절",
                      "params": {"decision": "rejected"}},
                 ] if r["gate_source"] == "next_step" else (
                     [
                         {"key": "single", "label": "이번 건만",
                          "params": {"decision": "approved", "scope": "single",
                                     "hours": 2}},
                         {"key": "mission", "label": "같은 대상",
                          "params": {"decision": "approved", "scope": "mission",
                                     "hours": 2, "max_executions": 20}},
                     ]
                     # 주문·자금(critical)에는 넓은 범위를 **보여주지도 않는다.**
                     # 서버가 눌린 뒤에 미션으로 낮추기는 하지만, 누를 수 있게
                     # 두면 언젠가 눌린다. 화면에서 먼저 내린다(2026-09-15).
                     + ([] if r["risk_level"] == "critical" else [
                         {"key": "session", "label": "이 대화 동안",
                          "params": {"decision": "approved", "scope": "session",
                                     "hours": 4, "max_executions": 50}},
                         {"key": "project", "label": "이 프로젝트 동안",
                          "params": {"decision": "approved", "scope": "project",
                                     "hours": 8, "max_executions": 100}},
                     ])
                     + [
                         {"key": "reject", "label": "거부",
                          "params": {"decision": "rejected"}},
                     ]
                 ))
             )}
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/approvals/notifications")
async def approvals_notifications(
    limit: int = Query(50, ge=1, le=200),
    include_acknowledged: bool = Query(False, description="확인한 것도 보기"),
):
    """막지 않고 알린 것들.

    2026-09-15 대표님 지시로 나뉘었다 — 되돌릴 수 있는 변경(목표·마일스톤·
    프롬프트, 실매매 주변 코드)은 막지 않고 여기에만 남는다. 아니다 싶으면
    대표님이 그때 되돌리시면 된다.
    """
    from app.core.db_pool import get_pool

    wanted = ["notified", "acknowledged"] if include_acknowledged else ["notified"]
    try:
        rows = await get_pool().fetch(
            """
            SELECT id::text, action_type, action_summary, risk_level,
                   gate_source, requested_by, decision,
                   to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM-DD HH24:MI') AS at
            FROM agent_permission_requests
            WHERE tier = 'notify' AND decision = ANY($2::text[])
            ORDER BY created_at DESC LIMIT $1
            """,
            limit, wanted,
        )
    except Exception as exc:
        logger.warning("approvals_notifications_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="알림 목록을 읽지 못했습니다") from exc

    return {
        "notifications": [
            {"id": r["id"], "tool": r["action_type"], "summary": r["action_summary"],
             "risk": r["risk_level"], "gate_source": r["gate_source"],
             "requested_by": r["requested_by"], "decision": r["decision"],
             "at": r["at"]}
            for r in rows
        ],
        "count": len(rows),
    }


@router.post("/approvals/{request_id}/acknowledge")
async def approvals_acknowledge(
    request_id: str,
    context: TenantContext = Depends(require_tenant_member),
):
    """알림 한 건을 확인 처리한다. 승인과 다르다 — 이미 실행된 일이다."""
    from app.core.db_pool import get_pool

    row = await get_pool().fetchrow(
        """
        UPDATE agent_permission_requests
           SET decision = 'acknowledged', decided_by = $2, decided_at = now(),
               updated_at = now()
         WHERE id = $1::uuid AND tier = 'notify' AND decision = 'notified'
        RETURNING id::text
        """,
        request_id, str(_tenant_id(context) or "CEO"),
    )
    if not row:
        raise HTTPException(status_code=404, detail="확인 대기 중인 알림이 아닙니다")
    return {"ok": True, "id": row["id"]}


@router.post("/approvals/acknowledge-all")
async def approvals_acknowledge_all(
    context: TenantContext = Depends(require_tenant_member),
):
    """알림을 한 번에 확인한다. 승인이 아니라 읽음 표시다."""
    from app.core.db_pool import get_pool

    count = await get_pool().fetchval(
        """
        WITH updated AS (
            UPDATE agent_permission_requests
               SET decision = 'acknowledged', decided_by = $1, decided_at = now(),
                   updated_at = now()
             WHERE tier = 'notify' AND decision = 'notified'
            RETURNING 1
        )
        SELECT count(*) FROM updated
        """,
        str(_tenant_id(context) or "CEO"),
    )
    return {"ok": True, "acknowledged": int(count or 0)}


@router.get("/approvals/gate-status")
async def approvals_gate_status():
    """게이트가 켜져 있나.

    조용히 꺼져 있으면 대표님은 "승인할 게 없다" 로 읽는다. 화면이 그것을
    구분해 그릴 수 있어야 한다.
    """
    import os

    from app.core.db_pool import get_pool

    live_on = os.getenv("LIVE_TRADING_GATE_ENABLED", "true").lower() == "true"
    direction_on = os.getenv("DIRECTION_GUARD_ENABLED", "true").lower() == "true"
    try:
        counts = await get_pool().fetchrow(
            """
            SELECT count(*) FILTER (WHERE decision = 'pending' AND tier = 'approve'
                                      AND expires_at > now()) AS waiting,
                   count(*) FILTER (WHERE decision = 'notified' AND tier = 'notify')
                       AS unread
            FROM agent_permission_requests
            """
        )
    except Exception:
        counts = None
    return {
        "live_trading_gate": live_on,
        "direction_guard": direction_on,
        "waiting": int(counts["waiting"]) if counts else 0,
        "unread_notifications": int(counts["unread"]) if counts else 0,
    }


@router.get("/approvals/active")
async def approvals_active(
    session_id: str = Query("", max_length=64, description="이 세션이 받은 승인만"),
    limit: int = Query(20, ge=1, le=100),
):
    """지금 살아 있는 승인 — 잔여 횟수와 남은 시간.

    미션 승인을 켜 두고 잊는 것이 가장 위험하다. 대표님이 상시로
    "무엇이 몇 회 남았나" 를 볼 수 있어야 회수 판단이 가능하다.
    """
    from app.core.db_pool import get_pool

    try:
        rows = await get_pool().fetch(
            """
            SELECT id::text, action_type, action_summary, requested_by,
                   approval_scope->>'scope' AS scope,
                   max_executions,
                   COALESCE((approval_scope->>'used')::int, 0) AS used,
                   GREATEST(0, EXTRACT(EPOCH FROM (expires_at - now()))::int / 60)
                       AS expires_in_min
              FROM agent_permission_requests
             WHERE decision = 'approved' AND expires_at > now()
               AND COALESCE((approval_scope->>'used')::int, 0) < max_executions
               AND ($2 = '' OR requested_by = $2)
             ORDER BY decided_at DESC LIMIT $1
            """,
            limit, session_id,
        )
    except Exception as exc:
        logger.warning("approvals_active_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="승인 현황을 읽지 못했습니다") from exc

    return {
        "active": [
            {"id": r["id"], "tool": r["action_type"], "summary": r["action_summary"],
             "scope": r["scope"] or "single", "used": r["used"],
             "max_executions": r["max_executions"],
             "remaining": max(0, r["max_executions"] - r["used"]),
             "expires_in_min": r["expires_in_min"]}
            for r in rows
        ],
        "count": len(rows),
    }


@router.post("/approvals/{request_id}/revoke")
async def approvals_revoke(
    request_id: str,
    context: TenantContext = Depends(require_tenant_member),
):
    """승인 즉시 회수.

    미션 승인을 주고 나서 "지금 당장 멈춰" 가 안 되면 아무도 미션 승인을
    주지 않는다. 회수는 만료를 앞당기는 것으로 끝낸다 — 기록은 남긴다.
    """
    from app.core.db_pool import get_pool

    revoked_by = str(_tenant_id(context) or "CEO")
    try:
        row = await get_pool().fetchrow(
            """
            UPDATE agent_permission_requests
               SET expires_at = now(), updated_at = now(),
                   reason = COALESCE(NULLIF(reason, ''), '') || ' [회수됨]'
             WHERE id = $1::uuid AND decision = 'approved' AND expires_at > now()
            RETURNING id::text, action_type
            """,
            request_id,
        )
    except Exception as exc:
        logger.warning("approvals_revoke_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="회수 처리 실패") from exc

    if not row:
        raise HTTPException(status_code=404, detail="유효한 승인이 아닙니다 (이미 만료·회수됨)")

    logger.warning(
        "live_trading_gate_revoked request=%s tool=%s by=%s",
        request_id[:8], row["action_type"], revoked_by[:8],
    )
    return {"id": row["id"], "revoked": True}


_SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# 담당 세션 생성 카드. 문자열을 두 벌로 적지 않는다 — 한쪽만 고치면
# 승인은 되는데 세션은 안 생기는, 제일 알아채기 어려운 실패가 된다.
_OWNER_SESSION_ACTION = "create_owner_session"


def _owner_session_note(
    *, head: str, approved: bool, summary: str, result: Optional[Dict[str, Any]]
) -> str:
    """담당 세션 생성 결정을 주도 대화에 적는다.

    **이 건은 이미 끝났다.** 다른 카드는 "승인했으니 이어서 하라" 지만
    여기서는 서버가 생성까지 마친 뒤에 적는 글이다. 그래서 무엇이 생겼는지
    (제목·마일스톤 수·프롬프트 유무)를 사실로 적고, 할 일을 시키지 않는다.
    """
    if not approved:
        return (
            f"**{head}** — 담당 세션 생성\n\n"
            f"- 요청: {(summary or '')[:300]}\n\n"
            "채팅창을 만들지 않습니다. 해당 마일스톤에는 '담당 세션 생성 거절됨' 을 "
            "남겼습니다 — 기존 담당에게 붙이거나 역할을 다시 정해야 합니다."
        )

    result = result or {}
    if result.get("error"):
        return (
            f"**{head}** — 담당 세션 생성\n\n"
            f"- 요청: {(summary or '')[:300]}\n"
            f"- ⚠️ 승인은 기록됐으나 **생성에 실패했습니다**: {str(result['error'])[:300]}\n\n"
            "마일스톤은 여전히 담당이 없습니다. 원인을 확인해야 합니다."
        )

    made = "새로 만들었습니다" if result.get("created") else "이미 있던 채팅창을 연결했습니다"
    lines = [
        f"**{head}** — 담당 세션 생성",
        "",
        f"- 채팅창: \"{result.get('session_title') or ''}\" — {made}",
        f"- 넘겨받은 마일스톤: {int(result.get('linked_milestones') or 0)}건",
    ]
    if result.get("has_prompt"):
        lines.append("- 역할 프롬프트: 있음")
    else:
        lines.append(
            "- ⚠️ 역할 프롬프트가 없습니다 — 이 담당은 자기가 무엇을 하는 사람인지 "
            "모르는 채로 시작합니다"
        )
    lines.append("")
    lines.append("맡은 마일스톤은 다음 발송 주기에 그 담당에게 전달됩니다.")
    return "\n".join(lines)


async def _notify_chat_of_approval_decision(
    *,
    session_id: str,
    request_id: str,
    tool: str,
    summary: str,
    decision: str,
    scope: str,
    grant_executions: int,
    hours: int,
    owner_scope: Optional[Dict[str, Any]] = None,
    owner_result: Optional[Dict[str, Any]] = None,
) -> None:
    """결정을 그 대화에 남기고, 남은 대기 건이 없으면 막힌 작업을 이어서 돌린다.

    2026-09-15 CEO 지적 — "승인 거절 누르면 해당 채팅창에 전달되나 액션이 없다".
    그전까지 `/approvals/{id}/decide` 는 DB 행만 바꿨다. 화면에서는 카드가
    사라질 뿐이고, 게이트에 막혀 멈춘 도구 호출은 **다음 지시가 올 때까지**
    그대로 서 있었다. 승인을 눌러도 아무 일이 일어나지 않는 것과 같다.

    그래서 둘을 한다. ① 결정을 대화에 기록으로 남긴다 — 승인 화면(/approvals)
    에서 눌러도 대화에 남아야 나중에 "누가 언제 무엇을 허락했나" 가 보인다.
    ② 그 세션에 남은 대기 건이 없을 때만 재개 턴을 띄운다. 대기 건마다
    턴을 띄우면 5건을 연달아 누를 때 턴이 5번 뜬다.
    """
    sid = (session_id or "").strip()
    if not _SESSION_UUID_RE.match(sid):
        return  # 채팅이 아닌 경로(러너·스크립트)에서 올라온 요청

    approved = decision == "approved"
    head = "✅ 승인" if approved else "⛔ 거절"
    is_owner_session = (tool or "") == _OWNER_SESSION_ACTION
    # 제안 카드는 도구 이름(`next_step`)을 보여 봐야 뜻이 없다.
    label = {
        "next_step": "다음 단계",
        _OWNER_SESSION_ACTION: "담당 세션 생성",
    }.get(tool or "", f"`{tool}`")

    if is_owner_session:
        # 거절도 결론이다. 남기지 않으면 그 마일스톤은 "승인 요청함" 에
        # 멈춘 채로 보이고, 다음 사람은 아직 대기 중인 줄 안다.
        if not approved:
            try:
                from app.services.owner_session_provision import (
                    note_owner_session_rejected,
                )

                await note_owner_session_rejected(owner_scope or {})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "owner_session_reject_note_failed request=%s error=%s",
                    request_id[:8], str(exc)[:160],
                )
        note = _owner_session_note(
            head=head, approved=approved, summary=summary, result=owner_result,
        )
    elif approved:
        scope_label = {
            "single": "이번 건만 (1회)",
            "mission": f"같은 대상 작업 (최대 {grant_executions}회)",
            "session": f"이 대화 동안 (최대 {grant_executions}회)",
            "project": f"이 프로젝트 전체 (최대 {grant_executions}회)",
        }.get(scope, f"같은 대상 작업 (최대 {grant_executions}회)")
        note = (
            f"**{head}** — {label}\n\n"
            f"- 범위: {scope_label} · 유효 {hours}시간\n"
            f"- 요청: {(summary or '')[:300]}\n\n"
            "이어서 진행합니다."
        )
    else:
        note = (
            f"**{head}** — {label}\n\n"
            f"- 요청: {(summary or '')[:300]}\n\n"
            "이 작업은 진행하지 않습니다."
        )

    from app.core.db_pool import get_pool

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO chat_messages
                       (session_id, role, content, intent, cost, tokens_in, tokens_out,
                        attachments, sources, tools_called)
                   VALUES ($1::uuid, 'assistant', $2, 'approval_decision', 0, 0, 0,
                           '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)""",
                sid, note,
            )
            await conn.execute(
                "UPDATE chat_sessions SET message_count = message_count + 1,"
                " updated_at = now() WHERE id = $1::uuid",
                sid,
            )
            remaining = await conn.fetchval(
                """SELECT count(*) FROM agent_permission_requests
                    WHERE requested_by = $1 AND decision = 'pending'
                      AND expires_at > now()""",
                sid,
            )
    except Exception as exc:
        logger.warning(
            "approval_decision_note_failed request=%s error=%s", request_id[:8], str(exc)
        )
        return

    if remaining:
        return  # 남은 결정을 다 누른 뒤에 한 번만 이어서 돈다

    try:
        from app.services.chat_service import trigger_ai_reaction

        # 제안 카드(next_step)는 "막혀서 멈춘 것" 이 아니라 "이걸 할까요" 다.
        # 같은 문구로 이어 붙이면 담당이 있지도 않은 중단 지점을 찾는다.
        is_proposal = (tool or "") == "next_step"
        if is_owner_session:
            # 여기서 "막혀서 중단된 작업을 이어서 수행하세요" 를 쓰면 안 된다.
            # 서버가 이미 실행을 끝냈다 — 주도는 있지도 않은 중단 지점을
            # 찾다가 엉뚱한 것을 다시 한다. 사실과 다음 흐름만 알린다.
            made = (owner_result or {})
            if not approved:
                prompt = (
                    "[시스템] 대표님이 담당 세션 생성을 거절했습니다.\n"
                    f"요청: {(summary or '')[:300]}\n\n"
                    "새 채팅창은 만들지 마세요. 이미 있는 담당에게 그 마일스톤을 "
                    "붙이거나, 담당 역할을 다시 정해서 올리세요."
                )
            elif made.get("error"):
                prompt = (
                    "[시스템] 담당 세션 생성이 승인됐으나 생성에 실패했습니다.\n"
                    f"실패 사유: {str(made['error'])[:300]}\n\n"
                    "마일스톤은 여전히 담당이 없습니다. 원인을 확인하고 보고하세요. "
                    "직접 세션을 만들지는 마세요 — 생성은 승인 경로로만 합니다."
                )
            else:
                prompt = (
                    "[시스템] 대표님이 승인해 담당 세션이 준비됐습니다.\n"
                    f"채팅창: \"{made.get('session_title') or ''}\" · "
                    f"넘겨받은 마일스톤 {int(made.get('linked_milestones') or 0)}건 · "
                    f"역할 프롬프트 {'있음' if made.get('has_prompt') else '없음'}\n\n"
                    "해당 담당에게는 다음 발송 주기에 마일스톤이 자동으로 전달됩니다. "
                    "지금 그 담당에게 따로 말을 걸 필요는 없습니다. "
                    "역할 프롬프트가 없다면 무엇을 하는 담당인지 정리해 올리세요."
                )
        elif approved and is_proposal:
            prompt = (
                f"[시스템] 대표님이 다음 단계를 승인했습니다.\n"
                f"승인된 제안: {(summary or '')[:500]}\n\n"
                "이 제안을 지금 수행하고 결과를 보고하세요. "
                "제안에 적힌 범위만 하고, 적히지 않은 변경은 하지 마세요. "
                "수행 중 새로 필요한 단계가 생기면 propose_next_steps 로 다시 올리세요."
            )
        elif approved:
            prompt = (
                f"[시스템] 대표님이 승인했습니다 — 도구 `{tool}`, 범위 {scope}, "
                f"유효 {hours}시간.\n요청 내용: {(summary or '')[:500]}\n\n"
                "보호 게이트에 막혀 중단됐던 그 작업을 지금 이어서 수행하고 결과를 보고하세요. "
                "승인 범위를 벗어나는 변경은 하지 마세요."
            )
        elif is_proposal:
            prompt = (
                f"[시스템] 대표님이 다음 단계 제안을 거절했습니다.\n"
                f"거절된 제안: {(summary or '')[:500]}\n\n"
                "이 제안은 진행하지 마세요. 다른 선택지가 있으면 짧게 제시하고, "
                "없으면 그대로 두고 다음 지시를 기다리세요."
            )
        else:
            prompt = (
                f"[시스템] 대표님이 거절했습니다 — 도구 `{tool}`.\n"
                f"요청 내용: {(summary or '')[:500]}\n\n"
                "이 작업은 진행하지 말고, 대신 가능한 대안과 남은 영향만 간단히 보고하세요."
            )
        await trigger_ai_reaction(sid, prompt)
    except Exception as exc:
        logger.warning(
            "approval_decision_reaction_failed request=%s error=%s",
            request_id[:8], str(exc),
        )


@router.post("/approvals/{request_id}/decide")
async def approvals_decide(
    request_id: str,
    decision: str = Query(..., pattern="^(approved|rejected)$"),
    reason: str = Query("", max_length=500),
    scope: str = Query("single", pattern="^(single|mission|session|project)$",
                       description="single=이번 건만, mission=같은 대상, "
                                   "session=이 대화 전체, project=이 프로젝트 전체"),
    hours: int = Query(2, ge=1, le=24, description="승인 유효 시간"),
    max_executions: int = Query(1, ge=1, le=500,
                                description="single 이 아닐 때 허용 횟수"),
    context: TenantContext = Depends(require_tenant_member),
):
    """승인 또는 거절.

    **에이전트가 아니라 사람이 부른다.** 브라우저에서 CEO 인증으로 호출되며,
    채팅 세션의 도구로는 노출하지 않는다 — 에이전트가 자기 요청을 스스로
    승인할 수 있으면 게이트가 없는 것과 같다.

    2026-09-15. 승인이 2시간·1회로 고정이라 "미션 끝까지" 가 불가능했다.
    이제 세 가지를 고른다 — 이번 건만 / 이 미션 동안 / 거부.
    미션 승인도 **무제한이 아니다**. 횟수 상한과 시간 상한을 둘 다 건다.
    """
    from app.core.db_pool import get_pool

    decided_by = str(_tenant_id(context) or "CEO")
    # 이번 건만이면 1회, 그 밖의 범위는 요청한 횟수 상한을 준다.
    grant_executions = max_executions if scope != "single" else 1
    try:
        row = await get_pool().fetchrow(
            """
            WITH cur AS (
                SELECT id, risk_level
                  FROM agent_permission_requests
                 WHERE id = $1::uuid AND decision = 'pending' AND expires_at > now()
            ),
            -- 주문·자금(critical)은 대화·프로젝트 범위로 열지 않는다.
            --
            -- 넓은 범위는 "같은 종류의 일을 계속 한다" 는 뜻이지 "돈이 나가는
            -- 일을 계속 해도 된다" 는 뜻이 아니다. 실수로 눌렀을 때 되돌릴 수
            -- 없는 쪽이므로, 거절하지 않고 **미션 범위로 낮춰서** 승인한다.
            -- 거절하면 대표님이 다시 눌러야 하고, 그대로 열면 게이트가 없는
            -- 것과 같다. 낮춘 사실은 대화 기록에 그대로 나간다.
            eff AS (
                SELECT id,
                       CASE WHEN $5 IN ('session', 'project')
                                 AND risk_level = 'critical'
                            THEN 'mission' ELSE $5 END AS scope
                  FROM cur
            )
            UPDATE agent_permission_requests a
               -- `reason` 은 NOT NULL 이다. NULLIF 로 빈 사유를 NULL 로
               -- 바꾸면 제약에 걸려 승인 자체가 503 으로 실패한다 — 대표님이
               -- 사유를 적지 않고 누르는 것이 보통이므로 사실상 항상 실패했다
               -- (2026-09-15 확인). 사유가 비면 기존 값을 그대로 둔다.
               SET decision = $2,
                   reason = CASE WHEN $3 <> '' THEN $3 ELSE reason END,
                   decided_by = $4,
                   decided_at = now(), updated_at = now(),
                   max_executions = CASE WHEN $2 = 'approved'
                                         THEN $6 ELSE max_executions END,
                   approval_scope = CASE WHEN $2 = 'approved'
                        THEN jsonb_build_object(
                                 'scope', eff.scope,
                                 'used', 0,
                                 -- 대상 지문은 요청 시점에 박혔다. 여기서
                                 -- 덮어 없애면 미션 승인이 다시 "세션의
                                 -- 모든 쓰기" 로 벌어진다(2026-09-15).
                                 'target', COALESCE(a.approval_scope->>'target', ''),
                                 -- 프로젝트 범위가 이 값으로 맞춘다. 대상
                                 -- 지문과 같은 이유로 요청 시점 값을 옮긴다.
                                 'project', COALESCE(a.approval_scope->>'project', ''),
                                 -- 파일 내용 지문도 같은 이유로 옮긴다. 여기서
                                 -- 빠뜨리면 승인 즉시 지문이 사라져 파일이
                                 -- 바뀌어도 재승인 없이 통과한다(2026-09-15).
                                 'file_fp', COALESCE(a.approval_scope->>'file_fp', ''),
                                 'mission_key', CASE WHEN eff.scope = 'mission'
                                     THEN split_part(a.work_key, ':', 1) || ':'
                                          || split_part(a.work_key, ':', 2)
                                     ELSE '' END)
                             -- 담당 세션 생성 카드는 승인된 뒤에 **서버가
                             -- 직접 실행**한다. 어느 목표의 어느 역할인지가
                             -- 여기서 지워지면 승인 즉시 되살릴 방법이 없다
                             -- (jsonb_build_object 는 나머지 키를 버린다).
                             || CASE WHEN a.action_type = 'create_owner_session'
                                     THEN jsonb_build_object(
                                         'goal_id',
                                         COALESCE(a.approval_scope->>'goal_id', ''),
                                         'role_key',
                                         COALESCE(a.approval_scope->>'role_key', ''),
                                         'workspace_id',
                                         COALESCE(a.approval_scope->>'workspace_id', ''),
                                         'requester_session_id',
                                         COALESCE(
                                             a.approval_scope->>'requester_session_id', ''),
                                         'has_prompt',
                                         COALESCE(a.approval_scope->'has_prompt',
                                                  'false'::jsonb))
                                     ELSE '{}'::jsonb END
                        ELSE a.approval_scope END,
                   expires_at = CASE WHEN $2 = 'approved'
                                     THEN now() + make_interval(hours => $7)
                                     ELSE now() END
              FROM eff
             WHERE a.id = eff.id
            RETURNING a.id::text, a.action_type, a.decision, a.max_executions,
                      a.approval_scope->>'scope' AS scope,
                      a.requested_by, a.action_summary,
                      a.approval_scope::text AS scope_json
            """,
            request_id, decision, reason, decided_by,
            scope, grant_executions, hours,
        )
    except Exception as exc:
        logger.warning("approvals_decide_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="승인 처리 실패") from exc

    if not row:
        raise HTTPException(status_code=404, detail="대기 중인 요청이 아닙니다 (이미 처리됐거나 없음)")

    # critical 은 위에서 미션 범위로 낮춰졌을 수 있다. 기록도 대화 알림도
    # **실제로 부여된 범위**를 써야 한다 — 요청한 범위를 적으면 대표님이
    # 누른 것과 실제 권한이 달라진다.
    effective_scope = row["scope"] or scope
    logger.warning(
        "live_trading_gate_decided request=%s tool=%s decision=%s scope=%s "
        "granted=%s hours=%s max_exec=%s by=%s",
        request_id[:8], row["action_type"], decision, scope, effective_scope,
        hours, grant_executions, decided_by[:8],
    )
    # 담당 세션 생성은 **서버가 여기서 끝낸다.** 승인만 기록하고 생성을
    # 담당 대화에 맡기면, 그 대화가 아직 없으므로 아무 일도 일어나지 않는다
    # (2026-09-17 CEO 지시 — "내가 승인 후 생성할 수 있게").
    #
    # DB 결정 UPDATE 직후에 실행한다. 생성이 실패해도 **결정은 되돌리지
    # 않는다** — 대표님이 누르신 사실은 기록으로 남아야 하고, 실패는 실패대로
    # 주도 대화에 적어 사람이 볼 수 있게 한다.
    owner_scope: Optional[Dict[str, Any]] = None
    owner_result: Optional[Dict[str, Any]] = None
    if row["action_type"] == _OWNER_SESSION_ACTION:
        try:
            owner_scope = json.loads(row["scope_json"] or "{}")
        except Exception:  # noqa: BLE001
            owner_scope = {}
        if decision == "approved":
            try:
                from app.services.owner_session_provision import (
                    provision_owner_session,
                )

                owner_result = await provision_owner_session(owner_scope)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "owner_session_provision_failed request=%s error=%s",
                    request_id[:8], str(exc)[:200],
                )
                owner_result = {"error": str(exc)[:200]}

    # 결정은 대화로 돌아간다 — 누른 결과가 화면에 보이고, 막힌 작업이 이어진다.
    await _notify_chat_of_approval_decision(
        session_id=row["requested_by"],
        request_id=request_id,
        tool=row["action_type"],
        summary=row["action_summary"],
        decision=decision,
        scope=effective_scope,
        grant_executions=grant_executions,
        hours=hours,
        owner_scope=owner_scope,
        owner_result=owner_result,
    )
    # 승인에는 반드시 시간 상한이 붙는다. 승인해 둔 것이 며칠 뒤 다른
    # 맥락에서 쓰이면 CEO 가 승인한 그 변경이 아니다.
    resp = {
        "id": row["id"],
        "decision": row["decision"],
        "scope": row["scope"] or ("single" if decision == "approved" else ""),
        "valid_hours": hours if decision == "approved" else 0,
        "max_executions": row["max_executions"] if decision == "approved" else 0,
    }
    if owner_result is not None:
        resp["owner_session"] = owner_result
    return resp


@router.post("/approvals/decide-bulk")
async def approvals_decide_bulk(
    payload: Dict[str, Any] = Body(...),
    context: TenantContext = Depends(require_tenant_member),
):
    """여러 건을 한 번에 승인하거나 거절한다.

    2026-09-15 CEO 지시 — "체크박스같은걸 둬서 체크건 승인 가능하게".

    대기가 열 건이면 열 번을 눌러야 했다. 누르는 동안 새 카드가 또 쌓이니
    끝이 안 난다. 화면에서 고른 것만 한 번에 처리한다.

    한 건이 실패해도 나머지는 계속한다 — 이미 처리된 카드가 섞여 있다고
    전체가 멈추면 다시 열 번을 눌러야 한다.
    """
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids 가 비어 있습니다")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="한 번에 100건까지입니다")

    decision = str(payload.get("decision") or "")
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision 은 approved 또는 rejected")
    scope = str(payload.get("scope") or "single")
    if scope not in ("single", "mission", "session", "project"):
        scope = "single"
    reason = str(payload.get("reason") or "")[:500]
    hours = max(1, min(24, int(payload.get("hours") or 12)))
    max_executions = max(1, min(500, int(payload.get("max_executions") or 1)))

    ok: list[str] = []
    failed: list[Dict[str, str]] = []
    for rid in ids:
        try:
            await approvals_decide(
                request_id=str(rid), decision=decision, reason=reason,
                scope=scope, hours=hours, max_executions=max_executions,
                context=context,
            )
            ok.append(str(rid))
        except HTTPException as exc:
            failed.append({"id": str(rid), "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": str(rid), "error": str(exc)[:160]})

    logger.warning(
        "live_trading_gate_decided_bulk count=%s ok=%s failed=%s decision=%s scope=%s",
        len(ids), len(ok), len(failed), decision, scope,
    )
    return {
        "requested": len(ids),
        "decided": len(ok),
        "ids": ok,
        "failed": failed,
        "decision": decision,
        "scope": scope,
    }


@router.get("/kg/stats")
async def kg_stats():
    """지식 그래프 현황."""
    from app.services.kg_query import stats

    return await stats()


@router.get("/kg/list")
async def kg_list(
    type: Optional[str] = Query(None, description="file/doc/commit/deploy/error/..."),
    q: Optional[str] = Query(None, max_length=120),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """그래프에 무엇이 들어 있는지 둘러본다 — 연결 많은 순.

    검색창만 있으면 **뭘 쳐야 할지 모르는 사람은 못 쓴다.**
    """
    from app.services.kg_query import list_nodes

    return await list_nodes(type, limit=limit, offset=offset, q=q)


@router.get("/kg/trace")
async def kg_trace(
    q: str = Query(..., min_length=2, max_length=200, description="파일 경로·커밋·오류 키"),
    limit: int = Query(40, ge=5, le=120),
):
    """한 노드에서 선을 따라간다.

    2026-09-14 신설. 벡터 검색은 "그 문장이 있는 문단" 을 준다. 그래프는
    "그래서 뭐가 어떻게 됐나" 를 이어서 준다.

    답하기 어려웠던 두 질문에 답한다.
      - 이 파일 고치면 뭐가 영향받나
      - 이 파일에 대한 문서가 어디 있나

    그래프는 **구조화된 기록**(오류 사전·변경 원장·배포 원장)에서 만든다.
    추측이 아니라 기계가 남긴 사실이다.
    """
    from app.services.kg_query import find_nodes, neighbors

    matches = await find_nodes(q, limit=8)
    if not matches:
        return {"query": q, "found": False, "matches": [], "node": None,
                "outgoing": [], "incoming": []}

    node = matches[0]
    rel = await neighbors(int(node["id"]), limit=limit)
    return {
        "query": q,
        "found": True,
        "matches": [{"id": int(m["id"]), "type": m["entity_type"], "name": m["name"],
                     "degree": int(m["degree"])} for m in matches],
        "node": {"id": int(node["id"]), "type": node["entity_type"], "name": node["name"],
                 "description": node["description"], "project": node["project"]},
        "outgoing": rel["outgoing"],
        "incoming": rel["incoming"],
    }


@router.get("/changes/digest")
async def changes_digest(
    days: int = Query(7, ge=1, le=60, description="며칠치"),
    project: Optional[str] = Query(None, description="프로젝트 한정"),
):
    """무엇이 언제 바뀌었나 — 날짜별 한 화면.

    2026-09-14 신설. 그 전까지 이걸 볼 방법이 없었다.

    규정(`.claude/rules/flow-rules.md`)은 기획→설계→실행→마무리 4단계
    문서를 요구하는데 실제 산출물은 FIND 0건 / LAYOUT 6건 / WRAP 3건이다.
    같은 기간 커밋 1,068건, 배포 430건이었다. **문서 절차는 사실상 돌지
    않는다.**

    대신 기계가 남기는 기록은 빠짐없이 쌓인다 — 변경 원장 30일 4,725건,
    배포 원장 430건. 문서를 다시 강제하는 대신 이 기록을 볼 수 있게 한다.

    세 가지를 한 줄에 묶는다.
      - 무엇을 고쳤나 (파일·요약)
      - 올라갔나 (커밋/푸시/배포 상태)
      - 배포는 성공했나 (deploy_runs)

    `dirty` 는 고쳐놓고 커밋이 안 된 변경이다. 30일 1,035건이었다 —
    이게 쌓이면 "고쳤는데 반영이 안 된" 상태가 조용히 남는다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    try:
        day_rows = await pool.fetch(
            """
            SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS day,
                   COALESCE(project, '?') AS project,
                   count(*) AS changes,
                   count(DISTINCT file_path) AS files,
                   count(*) FILTER (WHERE status = 'deployed') AS deployed,
                   count(*) FILTER (WHERE status = 'dirty') AS uncommitted,
                   count(DISTINCT commit_sha) FILTER (WHERE commit_sha IS NOT NULL) AS commits
            FROM chat_workspace_change_ledger
            WHERE created_at > now() - ($1::int * INTERVAL '1 day')
              AND ($2::text IS NULL OR project = $2::text)
            GROUP BY 1, 2
            ORDER BY 1 DESC, 3 DESC
            """,
            days, project,
        )
    except Exception as exc:
        logger.warning("changes_digest_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="변경 원장을 읽지 못했습니다") from exc

    days_map: dict = {}
    for r in day_rows:
        key = r["day"].isoformat()
        entry = days_map.setdefault(key, {"date": key, "projects": [], "changes": 0,
                                          "files": 0, "deployed": 0, "uncommitted": 0})
        entry["projects"].append({
            "project": r["project"],
            "changes": int(r["changes"]),
            "files": int(r["files"]),
            "deployed": int(r["deployed"]),
            "uncommitted": int(r["uncommitted"]),
            "commits": int(r["commits"]),
        })
        entry["changes"] += int(r["changes"])
        entry["files"] += int(r["files"])
        entry["deployed"] += int(r["deployed"])
        entry["uncommitted"] += int(r["uncommitted"])

    # 배포는 별도 원장이다. 같은 날짜에 붙여서 "바꿨고, 올라갔나" 를 한눈에.
    deploys = []
    try:
        rows = await pool.fetch(
            """
            SELECT id, project, left(release_sha, 12) AS sha, status, phase,
                   (created_at AT TIME ZONE 'Asia/Seoul')::date AS day,
                   to_char(created_at AT TIME ZONE 'Asia/Seoul', 'HH24:MI') AS at,
                   COALESCE(error_summary, '') AS error
            FROM deploy_runs
            WHERE created_at > now() - ($1::int * INTERVAL '1 day')
            ORDER BY id DESC LIMIT 200
            """,
            days,
        )
        deploys = [{
            "id": int(r["id"]), "project": r["project"] or "?", "sha": r["sha"],
            "status": r["status"], "phase": r["phase"],
            "date": r["day"].isoformat(), "at": r["at"], "error": r["error"],
        } for r in rows]
    except Exception as exc:
        logger.warning("changes_digest_deploys_failed", error=str(exc))

    # 최근 변경 파일 — 무엇을 고쳤는지 실물
    recent = []
    try:
        rows = await pool.fetch(
            """
            SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS day,
                   to_char(created_at AT TIME ZONE 'Asia/Seoul', 'HH24:MI') AS at,
                   COALESCE(project, '?') AS project, file_path, status,
                   COALESCE(commit_message, change_summary, '') AS summary,
                   COALESCE(left(commit_sha, 8), '') AS sha
            FROM chat_workspace_change_ledger
            WHERE created_at > now() - ($1::int * INTERVAL '1 day')
              AND ($2::text IS NULL OR project = $2::text)
            ORDER BY created_at DESC LIMIT 300
            """,
            days, project,
        )
        recent = [{
            "date": r["day"].isoformat(), "at": r["at"], "project": r["project"],
            "file": r["file_path"], "status": r["status"],
            "summary": (r["summary"] or "").strip()[:180], "sha": r["sha"],
        } for r in rows]
    except Exception as exc:
        logger.warning("changes_digest_recent_failed", error=str(exc))

    return {
        "days": sorted(days_map.values(), key=lambda d: d["date"], reverse=True),
        "deploys": deploys,
        "recent": recent,
        "window_days": days,
    }


@router.get("/project-docs/search")
async def search_docs_semantic(
    q: str = Query(..., min_length=2, max_length=300, description="찾는 내용 (뜻으로 찾는다)"),
    limit: int = Query(5, ge=1, le=50),
    project: Optional[str] = Query(None, description="프로젝트 한정 (AADS/GO100/KIS)"),
):
    """문서 **내용** 으로 찾는다 — 파일명이 아니라 뜻으로.

    2026-09-14 신설. 그 전까지 문서함 검색은 파일명과 경로만 봤다. 무엇에
    대한 문서인지 알아야 파일명을 떠올릴 수 있으니, 모르는 것을 찾을 때는
    쓸 수 없었다. 문서가 823건이고 이름은 대부분 `20260914_AADS_..._REPORT.md`
    꼴이라 더 그렇다.

    질문을 숫자로 바꿔 같은 방식으로 바꿔 둔 문서 조각과 비교한다. 단어가
    겹치지 않아도 뜻이 가까우면 찾는다 — "채팅이 왜 느려졌지" 로 물으면
    "첫 응답 타임아웃" 을 다룬 문단이 나온다.

    색인은 `scripts/index_docs.py` 가 만든다. 아직 임베딩이 안 채워진
    조각은 검색되지 않으므로, 응답에 진행률을 같이 준다 — 결과가 적을 때
    "없는 것" 인지 "아직 안 된 것" 인지 구분할 수 있어야 한다.
    """
    from app.core.db_pool import get_pool
    from app.services.chat_embedding_service import EmbeddingRouteUnavailable
    from app.services.doc_index import embed_query, index_status, search_docs

    status = {}
    try:
        status = await index_status()
    except Exception:
        status = {}

    try:
        query_vector = await embed_query(q)
    except EmbeddingRouteUnavailable:
        # 더미 벡터로 검색하면 아무 문서나 그럴듯한 점수로 나온다.
        # 결과가 없다고 하는 편이 거짓 결과보다 낫다.
        raise HTTPException(
            status_code=503,
            detail="임베딩 경로를 쓸 수 없어 내용 검색이 불가합니다. 파일명 검색을 사용하세요.",
        )
    except Exception as exc:
        logger.warning("doc_search_embed_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="내용 검색 일시 불가") from exc

    rows = await search_docs(
        query_vector, top_k=limit * 3, project=project, query_text=q,
    )

    # 같은 문서의 여러 조각이 잡히면 가장 잘 맞는 것 하나만 남긴다.
    # 안 그러면 긴 문서 하나가 결과를 독점한다.
    best: dict = {}
    for r in rows:
        path = r.get("doc_path", "")
        prev = best.get(path)
        if prev is None or r.get("similarity", 0) > prev.get("similarity", 0):
            best[path] = r

    results = sorted(best.values(), key=lambda r: -r.get("similarity", 0))[:limit]

    pool = get_pool()
    out = []
    for r in results:
        path = r["doc_path"]
        content = (r.get("content") or "").strip()
        try:
            total = await pool.fetchval(
                "SELECT count(*) FROM doc_chunks WHERE doc_path = $1", path
            )
        except Exception:
            total = None
        out.append({
            "path": path,
            "name": os.path.basename(path),
            "project": r.get("project", ""),
            "server": r.get("server", ""),
            "title": r.get("title", ""),
            "heading": r.get("heading", ""),
            "snippet": content[:400] + ("…" if len(content) > 400 else ""),
            "similarity": round(float(r.get("similarity", 0.0)), 4),
            "chunks": total,
        })

    return {
        "query": q,
        "count": len(out),
        "results": out,
        "index": {
            "docs": status.get("docs", 0),
            "chunks": status.get("chunks", 0),
            "searchable": status.get("embedded", 0),
            "coverage_pct": status.get("coverage", 0.0),
        },
    }


@router.get("/project-docs/content")
async def get_doc_content(
    project: str = Query(..., description="프로젝트명 (AADS/KIS/GO100/SF/NTV2)"),
    base_path: str = Query(..., description="base_path (스캔 결과에서 제공)"),
    file_path: str = Query(..., description="파일 상대 경로"),
):
    """문서 내용 조회."""
    config = SERVER_CONFIG.get(project)
    if not config:
        raise HTTPException(400, f"Unknown project: {project}")

    # 경로 검증 (traversal 방지)
    if not _is_safe_relative_path(file_path):
        raise HTTPException(400, "Invalid file path")

    project, base_path, file_path, full_path = await _resolve_content_location(project, base_path, file_path)
    config = SERVER_CONFIG.get(project)
    if not config:
        raise HTTPException(400, f"Unknown project: {project}")
    host = config["host"]

    # 확장자 기반 바이너리 판별
    ext = ("." + file_path.rsplit(".", 1)[-1].lower()) if "." in file_path else ""
    is_binary = ext in BINARY_EXTENSIONS
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    if host is None:
        # 로컬 파일
        p = _resolve_local_file(project, base_path, file_path)
        full_path = str(p)
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "File not found")
        size_bytes = p.stat().st_size
        if ext in EXCEL_EXTENSIONS:
            raw = p.read_bytes()
            try:
                content = _excel_bytes_to_csv_text(raw, p.name)
                return {
                    "project": project,
                    "file_path": file_path,
                    "full_path": full_path,
                    "content": content,
                    "size": len(content),
                    "encoding": "text",
                    "mime_type": "text/csv",
                    "is_binary": False,
                    "source_mime_type": mime_type,
                    "converted_from": ext.lstrip("."),
                    "format": "excel-csv",
                }
            except Exception as e:
                logger.warning("project_doc_excel_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if ext in DOCX_EXTENSIONS:
            raw = p.read_bytes()
            try:
                content = _docx_bytes_to_text(raw, p.name)
                return _office_preview_response(
                    project=project,
                    file_path=file_path,
                    full_path=full_path,
                    content=content,
                    source_mime_type=mime_type,
                    ext=ext,
                )
            except Exception as e:
                logger.warning("project_doc_docx_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if ext in (XML_OFFICE_EXTENSIONS - EXCEL_EXTENSIONS - DOCX_EXTENSIONS):
            raw = p.read_bytes()
            try:
                content = _zip_office_bytes_to_text(raw, p.name, ext)
                return _office_preview_response(
                    project=project,
                    file_path=file_path,
                    full_path=full_path,
                    content=content,
                    source_mime_type=mime_type,
                    ext=ext,
                )
            except Exception as e:
                logger.warning("project_doc_xml_office_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if ext in LEGACY_OFFICE_EXTENSIONS:
            raw = p.read_bytes()
            try:
                content = _legacy_office_bytes_to_text(raw, p.name)
                return _office_preview_response(
                    project=project,
                    file_path=file_path,
                    full_path=full_path,
                    content=content,
                    source_mime_type=mime_type,
                    ext=ext,
                )
            except Exception as e:
                logger.warning("project_doc_legacy_office_preview_failed", path=full_path, error=str(e))
                is_binary = True
        # 텍스트 1MB / 바이너리 10MB 한도
        max_size = 10_000_000 if is_binary else 1_000_000
        if size_bytes > max_size:
            raise HTTPException(413, f"File too large (>{max_size // 1_000_000}MB)")
        if is_binary:
            raw = p.read_bytes()
            content = base64.b64encode(raw).decode("ascii")
        else:
            content = p.read_text(encoding="utf-8", errors="replace")
    else:
        # 원격 파일
        normalized_base = str(Path(base_path))
        if normalized_base not in _configured_base_paths(project):
            raise HTTPException(400, "Unsupported base_path")
        if ext in EXCEL_EXTENSIONS:
            quoted_full_path = shlex.quote(full_path)
            b64_output = await _run_cmd(
                ["ssh", "-o", "ConnectTimeout=5", host, f"base64 -w0 {quoted_full_path} 2>/dev/null"],
                timeout=15,
            )
            if not b64_output:
                raise HTTPException(404, "File not found or empty")
            raw = base64.b64decode(b64_output.strip())
            try:
                content = _excel_bytes_to_csv_text(raw, Path(file_path).name)
                return {
                    "project": project,
                    "file_path": file_path,
                    "full_path": full_path,
                    "content": content,
                    "size": len(content),
                    "encoding": "text",
                    "mime_type": "text/csv",
                    "is_binary": False,
                    "source_mime_type": mime_type,
                    "converted_from": ext.lstrip("."),
                    "format": "excel-csv",
                }
            except Exception as e:
                logger.warning("project_doc_remote_excel_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if ext in DOCX_EXTENSIONS:
            quoted_full_path = shlex.quote(full_path)
            b64_output = await _run_cmd(
                ["ssh", "-o", "ConnectTimeout=5", host, f"base64 -w0 {quoted_full_path} 2>/dev/null"],
                timeout=15,
            )
            if not b64_output:
                raise HTTPException(404, "File not found or empty")
            raw = base64.b64decode(b64_output.strip())
            try:
                content = _docx_bytes_to_text(raw, Path(file_path).name)
                return _office_preview_response(
                    project=project,
                    file_path=file_path,
                    full_path=full_path,
                    content=content,
                    source_mime_type=mime_type,
                    ext=ext,
                )
            except Exception as e:
                logger.warning("project_doc_remote_docx_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if ext in (XML_OFFICE_EXTENSIONS - EXCEL_EXTENSIONS - DOCX_EXTENSIONS):
            quoted_full_path = shlex.quote(full_path)
            b64_output = await _run_cmd(
                ["ssh", "-o", "ConnectTimeout=5", host, f"base64 -w0 {quoted_full_path} 2>/dev/null"],
                timeout=15,
            )
            if not b64_output:
                raise HTTPException(404, "File not found or empty")
            raw = base64.b64decode(b64_output.strip())
            try:
                content = _zip_office_bytes_to_text(raw, Path(file_path).name, ext)
                return _office_preview_response(
                    project=project,
                    file_path=file_path,
                    full_path=full_path,
                    content=content,
                    source_mime_type=mime_type,
                    ext=ext,
                )
            except Exception as e:
                logger.warning("project_doc_remote_xml_office_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if ext in LEGACY_OFFICE_EXTENSIONS:
            quoted_full_path = shlex.quote(full_path)
            b64_output = await _run_cmd(
                ["ssh", "-o", "ConnectTimeout=5", host, f"base64 -w0 {quoted_full_path} 2>/dev/null"],
                timeout=15,
            )
            if not b64_output:
                raise HTTPException(404, "File not found or empty")
            raw = base64.b64decode(b64_output.strip())
            try:
                content = _legacy_office_bytes_to_text(raw, Path(file_path).name)
                return _office_preview_response(
                    project=project,
                    file_path=file_path,
                    full_path=full_path,
                    content=content,
                    source_mime_type=mime_type,
                    ext=ext,
                )
            except Exception as e:
                logger.warning("project_doc_remote_legacy_office_preview_failed", path=full_path, error=str(e))
                is_binary = True
        if is_binary:
            # SSH base64 인코딩으로 바이너리 안전 전송
            quoted_full_path = shlex.quote(full_path)
            b64_output = await _run_cmd(
                ["ssh", "-o", "ConnectTimeout=5", host, f"base64 -w0 {quoted_full_path} 2>/dev/null"],
                timeout=15,
            )
            if not b64_output:
                raise HTTPException(404, "File not found or empty")
            content = b64_output.strip()
        else:
            quoted_full_path = shlex.quote(full_path)
            content = await _run_cmd(
                ["ssh", "-o", "ConnectTimeout=5", host, f"cat {quoted_full_path}"],
                timeout=10,
            )
            if not content:
                raise HTTPException(404, "File not found or empty")

    return {
        "project": project,
        "file_path": file_path,
        "full_path": full_path,
        "content": content,
        "size": len(content),
        "encoding": "base64" if is_binary else "text",
        "mime_type": mime_type,
        "is_binary": is_binary,
        "format": "binary" if is_binary and ext not in BASE64_PREVIEW_EXTENSIONS else _detect_format(file_path),
    }
