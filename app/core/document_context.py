"""
Ephemeral Document Context — 파일 첨부 시 대화 맥락 보호 시스템.

파일 전문을 대화 히스토리에 영구 저장하는 대신:
1. 현재 턴에만 Layer D로 주입 (ephemeral)
2. 히스토리에는 1줄 참조 요약만 저장
3. 다음 턴부터 파일 내용은 컨텍스트에서 사라짐

토큰 예산:
- DOCUMENT_FULL_INSERT_MAX_TOKENS (기본 30000): 이하이면 전문 삽입
- 초과 시 앞뒤 요약 + 중간 생략 모드
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 환경 설정 ────────────────────────────────────────────────────────
FULL_INSERT_MAX_TOKENS = int(os.getenv("DOCUMENT_FULL_INSERT_MAX_TOKENS", "60000"))
CHUNK_MAX_TOKENS = int(os.getenv("DOCUMENT_CHUNK_MAX_TOKENS", "15000"))

# 지원 텍스트 확장자
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".css", ".yaml", ".yml", ".toml", ".sh", ".sql", ".log",
    ".xml", ".ini", ".conf", ".cfg", ".env", ".rs", ".go", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
})

# PDF / Excel (Stage 6 확장 포인트)
PDF_EXTENSIONS = frozenset({".pdf"})
EXCEL_EXTENSIONS = frozenset({".xlsx", ".xls"})

# 이미지 파일 (Claude Vision API 지원)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
IMAGE_MEDIA_TYPES: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Vision API 이미지 크기 제한 (5MB)
IMAGE_MAX_BYTES = int(os.getenv("VISION_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))


def estimate_tokens(text: str) -> int:
    """한국어/다국어를 고려한 토큰 추정 (UTF-8 bytes // 3)."""
    from app.core.token_utils import estimate_tokens as _est
    return _est(text)


def extract_file_contents(
    attachments: List[Dict[str, Any]],
    max_read_bytes: int = 500_000,
) -> List[Dict[str, Any]]:
    """
    첨부파일 목록에서 파일 내용을 추출.

    Returns:
        list of {name, path, ext, content, tokens, readable, error}
        이미지의 경우: 추가로 {base64_data, media_type, is_image} 포함
    """
    results = []
    for att in attachments:
        if not isinstance(att, dict):
            att = {}
        file_path = att.get("path", "")
        file_name = att.get("name", "unknown")
        ext = os.path.splitext(file_path)[1].lower() if file_path else ""

        entry: Dict[str, Any] = {
            "name": file_name,
            "path": file_path,
            "ext": ext,
            "content": "",
            "tokens": 0,
            "readable": False,
            "error": None,
        }

        # ── 인라인 base64 이미지 (Ctrl+V 클립보드 붙여넣기 등, 디스크 저장 없이 직접 전달)
        if att.get("type") == "image" and att.get("base64"):
            entry["base64_data"] = att["base64"]
            entry["media_type"] = att.get("media_type", "image/jpeg")
            entry["is_image"] = True
            entry["readable"] = True
            entry["tokens"] = len(att["base64"]) // 1000  # base64 길이 기반 토큰 추정
            results.append(entry)
            continue

        # ── 인라인 PDF base64 (브라우저에서 직접 전달 — 텍스트 추출)
        if att.get("type") == "pdf" and att.get("base64"):
            try:
                raw_bytes = base64.b64decode(att["base64"])
                pdf_text = _extract_pdf_from_bytes(raw_bytes, max_read_bytes)
                if pdf_text:
                    entry["content"] = pdf_text
                    entry["tokens"] = estimate_tokens(pdf_text)
                    entry["readable"] = True
                else:
                    entry["content"] = f"[PDF 파일: {file_name}] (텍스트 추출 불가)"
                    entry["readable"] = True
                    entry["tokens"] = 10
            except Exception as e:
                entry["error"] = f"pdf_decode_error: {e}"
            results.append(entry)
            continue

        # ── 인라인 텍스트 (클라이언트 측에서 읽어 직접 전달 — 디스크 저장 불필요)
        if att.get("type") == "text" and att.get("content") is not None:
            content_str = att["content"]
            entry["content"] = content_str
            entry["tokens"] = estimate_tokens(content_str)
            entry["readable"] = True
            results.append(entry)
            continue

        if not file_path or not os.path.isfile(file_path):
            entry["error"] = "file_not_found"
            results.append(entry)
            continue

        # 이미지 파일 (Claude Vision API) — 디스크에서 읽기
        if ext in IMAGE_EXTENSIONS:
            try:
                file_size = os.path.getsize(file_path)
                if file_size > IMAGE_MAX_BYTES:
                    entry["error"] = f"image_too_large: {file_size // 1024}KB > {IMAGE_MAX_BYTES // 1024}KB"
                else:
                    with open(file_path, "rb") as f:
                        raw_bytes = f.read()
                    entry["base64_data"] = base64.b64encode(raw_bytes).decode("utf-8")
                    entry["media_type"] = IMAGE_MEDIA_TYPES.get(ext, "image/jpeg")
                    entry["is_image"] = True
                    entry["readable"] = True
                    entry["tokens"] = len(raw_bytes) // 750  # 이미지 토큰 추정
            except Exception as e:
                entry["error"] = str(e)
            results.append(entry)
            continue

        # 텍스트 파일
        if ext in TEXT_EXTENSIONS:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(max_read_bytes)
                entry["content"] = content
                entry["tokens"] = estimate_tokens(content)
                entry["readable"] = True
            except Exception as e:
                entry["error"] = str(e)

        # PDF (Stage 6 — 현재는 placeholder)
        elif ext in PDF_EXTENSIONS:
            entry["content"] = _extract_pdf(file_path, max_read_bytes)
            if entry["content"]:
                entry["tokens"] = estimate_tokens(entry["content"])
                entry["readable"] = True
            else:
                entry["error"] = "pdf_extraction_not_available"

        # Excel (Stage 6 — 현재는 placeholder)
        elif ext in EXCEL_EXTENSIONS:
            entry["content"] = _extract_excel(file_path)
            if entry["content"]:
                entry["tokens"] = estimate_tokens(entry["content"])
                entry["readable"] = True
            else:
                entry["error"] = "excel_extraction_not_available"

        else:
            entry["error"] = f"unsupported_extension: {ext}"

        results.append(entry)

    return results


def _extract_pdf_from_bytes(raw_bytes: bytes, max_bytes: int = 500_000) -> str:
    """인라인 PDF bytes에서 텍스트 추출 (pymupdf 우선, pdfplumber 폴백)."""
    import io
    # pymupdf (fitz)
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
        pages = []
        total_chars = 0
        for page in doc:
            text = page.get_text()
            pages.append(text)
            total_chars += len(text)
            if total_chars > max_bytes:
                break
        doc.close()
        return "\n\n--- 페이지 구분 ---\n\n".join(pages)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pymupdf bytes extraction failed: {e}")

    # pdfplumber fallback
    try:
        import pdfplumber
        pages = []
        total_chars = 0
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages.append(text)
                total_chars += len(text)
                if total_chars > max_bytes:
                    break
        return "\n\n--- 페이지 구분 ---\n\n".join(pages)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pdfplumber bytes extraction failed: {e}")

    return ""


def _extract_pdf(file_path: str, max_bytes: int = 500_000) -> str:
    """PDF 텍스트 추출 (pymupdf 우선, pdfplumber 폴백)."""
    # pymupdf (fitz)
    try:
        import fitz  # pymupdf
        doc = fitz.open(file_path)
        pages = []
        total_chars = 0
        for page in doc:
            text = page.get_text()
            pages.append(text)
            total_chars += len(text)
            if total_chars > max_bytes:
                break
        doc.close()
        return "\n\n--- 페이지 구분 ---\n\n".join(pages)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pymupdf extraction failed: {e}")

    # pdfplumber fallback
    try:
        import pdfplumber
        pages = []
        total_chars = 0
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages.append(text)
                total_chars += len(text)
                if total_chars > max_bytes:
                    break
        return "\n\n--- 페이지 구분 ---\n\n".join(pages)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pdfplumber extraction failed: {e}")

    return ""


def _extract_excel(file_path: str) -> str:
    """Excel 텍스트 추출 (openpyxl)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheets = []
        for ws in wb.worksheets[:5]:  # 최대 5시트
            rows = []
            for row in ws.iter_rows(max_row=500, values_only=True):
                row_str = "\t".join(str(c) if c is not None else "" for c in row)
                rows.append(row_str)
            if rows:
                sheets.append(f"[Sheet: {ws.title}]\n" + "\n".join(rows))
        wb.close()
        return "\n\n".join(sheets)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"openpyxl extraction failed: {e}")

    return ""


def build_ephemeral_document_layer(
    file_contents: List[Dict[str, Any]],
) -> str:
    """
    Layer D 구성: 현재 턴에만 주입되는 첨부파일 전문 컨텍스트.

    - 전체 토큰이 FULL_INSERT_MAX_TOKENS 이하: 전문 삽입
    - 초과 시: 앞 CHUNK_MAX_TOKENS + 뒤 CHUNK_MAX_TOKENS + 중간 생략 표시
    """
    readable = [f for f in file_contents if f.get("readable") and f.get("content")]
    if not readable:
        return ""

    parts = ["<ephemeral_document_context>"]
    parts.append("<!-- 이 섹션은 현재 턴에만 존재하며, 다음 턴에서는 제거됩니다 -->")

    total_tokens = sum(f["tokens"] for f in readable)

    for f in readable:
        name = f["name"]
        tokens = f["tokens"]
        content = f["content"]

        parts.append(f"\n<document name=\"{name}\" tokens=\"{tokens}\">")

        if total_tokens <= FULL_INSERT_MAX_TOKENS:
            # 전문 삽입 모드
            parts.append(content)
        else:
            # 분할 모드: 앞뒤만 삽입
            # 코드 파일(ASCII 위주)은 chars_per_token≈3, 한국어 문서는 2
            _ext = f.get("ext", "")
            _code_exts = {".py",".js",".ts",".tsx",".jsx",".go",".rs",".java",".c",".cpp",".h",".rb",".php",".swift",".kt",".sql",".sh",".css",".html",".xml",".json",".yaml",".yml",".toml",".ini",".conf",".cfg",".env"}
            _cpt = 3 if _ext in _code_exts else 2
            char_limit = CHUNK_MAX_TOKENS * _cpt  # 토큰→문자 역변환
            if len(content) <= char_limit * 2:
                parts.append(content)
            else:
                head = content[:char_limit]
                tail = content[-char_limit:]
                omitted_chars = len(content) - char_limit * 2
                omitted_tokens = estimate_tokens(content[char_limit:-char_limit])
                parts.append(head)
                parts.append(f"\n\n... [중간 {omitted_tokens:,}토큰 ({omitted_chars:,}자) 생략] ...\n")
                parts.append(tail)

        parts.append("</document>")

    parts.append("\n</ephemeral_document_context>")

    return "\n".join(parts)


def build_file_reference_summary(
    file_contents: List[Dict[str, Any]],
) -> str:
    """
    히스토리에 저장할 1줄 파일 참조 요약.
    전문 대신 이것만 DB에 저장된다.
    """
    summaries = []
    for f in file_contents:
        name = f["name"]
        tokens = f["tokens"]
        ext = f["ext"]
        readable = f.get("readable", False)

        if f.get("is_image") and readable:
            summaries.append(f"[첨부이미지: {name} ({ext})]")
        elif readable and tokens > 0:
            # 파일 첫 200자 미리보기
            preview = f["content"][:200].replace("\n", " ").strip()
            if len(f["content"]) > 200:
                preview += "..."
            summaries.append(
                f"[첨부파일: {name} ({ext}, ~{tokens:,}토큰) — 미리보기: {preview}]"
            )
        elif f.get("error"):
            summaries.append(f"[첨부파일: {name} ({ext}) — {f['error']}]")
        else:
            summaries.append(f"[첨부파일: {name} ({ext})]")

    return "\n".join(summaries)


def extract_image_blocks(
    file_contents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Claude Vision API 형식의 이미지 content block 목록 추출.

    Returns:
        list of {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
    """
    blocks = []
    for f in file_contents:
        if f.get("is_image") and f.get("readable") and f.get("base64_data"):
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f.get("media_type", "image/jpeg"),
                    "data": f["base64_data"],
                },
            })
            logger.debug(f"[Vision] image block prepared: {f['name']} ({f.get('media_type')})")
    return blocks


# ── Stage 3: 파일 재참조 ──────────────────────────────────────────────

import re as _re

_REREF_PATTERNS = [
    _re.compile(r"아까\s*(?:그|그\s*)?파일", _re.IGNORECASE),
    _re.compile(r"방금\s*(?:그|그\s*)?파일", _re.IGNORECASE),
    _re.compile(r"위\s*파일", _re.IGNORECASE),
    _re.compile(r"첨부\s*(?:한|했던|된)\s*파일", _re.IGNORECASE),
    _re.compile(r"(?:이전|앞서)\s*(?:첨부|올린|보낸)\s*파일", _re.IGNORECASE),
    _re.compile(r"그\s*파일\s*(?:다시|에서|의|중)", _re.IGNORECASE),
    _re.compile(r"\[첨부파일:\s*(.+?)\].*(?:다시|보여|확인|열어)", _re.IGNORECASE),
    _re.compile(r"(?:the|that)\s+file", _re.IGNORECASE),
]


def detect_file_rereference(user_message: str) -> bool:
    """사용자 메시지에서 이전 첨부파일 재참조 패턴을 감지."""
    return any(p.search(user_message) for p in _REREF_PATTERNS)


async def lookup_session_files(
    session_id: str,
    db_pool,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    세션의 최근 첨부파일 메타데이터를 조회.
    chat_messages.attachments JSON에서 path/name 추출.
    """
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT attachments, created_at
                FROM chat_messages
                WHERE session_id = $1::uuid
                  AND role = 'user'
                  AND attachments IS NOT NULL
                  AND attachments != '[]'::jsonb
                ORDER BY created_at DESC
                LIMIT $2
                """,
                session_id,
                limit,
            )
            files = []
            for row in rows:
                atts = row["attachments"]
                if isinstance(atts, str):
                    import json
                    atts = json.loads(atts)
                for att in (atts or []):
                    if isinstance(att, dict) and att.get("path"):
                        files.append(att)
            return files
    except Exception as e:
        logger.warning(f"[ReRef] session file lookup failed: {e}")
        return []


async def build_rereference_context(
    user_message: str,
    session_id: str,
    db_pool,
) -> str:
    """
    파일 재참조 감지 → 이전 첨부파일 내용을 Layer D로 재주입.
    재참조가 감지되지 않으면 빈 문자열 반환.
    """
    if not detect_file_rereference(user_message):
        return ""

    logger.info(f"[ReRef] file re-reference detected in session {session_id[:8]}")
    prev_files = await lookup_session_files(session_id, db_pool)
    if not prev_files:
        logger.info("[ReRef] no previous files found for session")
        return ""

    # 이전 첨부파일을 다시 읽어서 Layer D 구성
    file_contents = extract_file_contents(prev_files)
    readable = [f for f in file_contents if f.get("readable")]
    if not readable:
        return ""

    total_tokens = sum(f["tokens"] for f in readable)
    logger.info(f"[ReRef] re-injecting {len(readable)} files (~{total_tokens} tokens)")

    return build_ephemeral_document_layer(readable)
