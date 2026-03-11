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

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 환경 설정 ────────────────────────────────────────────────────────
FULL_INSERT_MAX_TOKENS = int(os.getenv("DOCUMENT_FULL_INSERT_MAX_TOKENS", "30000"))
CHUNK_MAX_TOKENS = int(os.getenv("DOCUMENT_CHUNK_MAX_TOKENS", "6000"))

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


def estimate_tokens(text: str) -> int:
    """빠른 토큰 추정 (chars // 4 휴리스틱)."""
    return len(text) // 4 if text else 0


def extract_file_contents(
    attachments: List[Dict[str, Any]],
    max_read_bytes: int = 200_000,
) -> List[Dict[str, Any]]:
    """
    첨부파일 목록에서 파일 내용을 추출.

    Returns:
        list of {name, path, ext, content, tokens, readable, error}
    """
    results = []
    for att in attachments:
        file_path = att.get("path", "") if isinstance(att, dict) else ""
        file_name = att.get("name", "") if isinstance(att, dict) else str(att)
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

        if not file_path or not os.path.isfile(file_path):
            entry["error"] = "file_not_found"
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


def _extract_pdf(file_path: str, max_bytes: int = 200_000) -> str:
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
            char_limit = CHUNK_MAX_TOKENS * 4  # 토큰→문자 역변환
            if len(content) <= char_limit * 2:
                parts.append(content)
            else:
                head = content[:char_limit]
                tail = content[-char_limit:]
                omitted_chars = len(content) - char_limit * 2
                omitted_tokens = omitted_chars // 4
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

        if readable and tokens > 0:
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
