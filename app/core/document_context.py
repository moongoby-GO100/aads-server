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
import hashlib
import io
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 환경 설정 ────────────────────────────────────────────────────────
FULL_INSERT_MAX_TOKENS = int(os.getenv("DOCUMENT_FULL_INSERT_MAX_TOKENS", "30000"))
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

# Pillow 로 PNG 변환 후에야 Vision 에 넣을 수 있는 포맷.
# Anthropic 이 직접 받는 것은 jpeg/png/gif/webp 넷뿐이다.
CONVERTIBLE_IMAGE_EXTENSIONS = frozenset({".bmp", ".tiff", ".tif", ".ico", ".ppm", ".pcx"})

# Pillow 가 기본 빌드로 열지 못하는 포맷. pillow_heif 는 이 서버에 없다.
# 추측으로 OCR 하지 않고 건너뛴다 — 틀린 텍스트를 넣는 것이 안 넣는 것보다 나쁘다.
UNSUPPORTED_IMAGE_EXTENSIONS = frozenset({".heic", ".heif", ".avif", ".jxl", ".svg"})

# iPhone 기본 포맷. pillow_heif 가 깔려 있으면 PNG 로 열 수 있고,
# 없으면 사유를 남기고 건너뛴다 (UNSUPPORTED 보다 먼저 판정한다).
HEIF_EXTENSIONS = frozenset({".heic", ".heif"})
HEIF_MEDIA_TYPES = frozenset({"image/heic", "image/heif"})

# PDF 는 document block 으로 그대로 올린다 (Anthropic 한도 32MB).
PDF_MAX_BYTES = int(os.getenv("VISION_PDF_MAX_BYTES", str(32 * 1024 * 1024)))

# Vision 이 내부적으로 리사이즈하는 장변 상한. 이보다 크게 보내봐야 토큰만 쓴다.
VISION_MAX_DIMENSION = int(os.getenv("VISION_IMAGE_MAX_DIMENSION", "1568"))

# extra_paths 로 디스크를 읽을 때 접근을 거부할 경로.
SENSITIVE_PATH_PREFIXES = ("/etc", "/root/.ssh", "/proc", "/run/secrets")


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

        # ── file_id 기반 첨부파일 (디스크에 저장된 파일 — chat_service.py에서 별도 Vision 처리)
        if att.get("file_id"):
            _fname = att.get("name", "file")
            _fext = os.path.splitext(_fname)[1].lower() if _fname else ""
            entry["name"] = _fname
            entry["ext"] = _fext
            _mime = att.get("media_type", att.get("mime_type", ""))
            if _mime.startswith("image/") or att.get("type") == "image" or _fext in IMAGE_EXTENSIONS:
                entry["is_image"] = True
                entry["readable"] = True
                entry["media_type"] = _mime or IMAGE_MEDIA_TYPES.get(_fext, "image/jpeg")
                entry["tokens"] = 200  # Vision 처리는 chat_service.py에서 별도 수행
            elif _mime.startswith("video/") or att.get("type") == "video":
                entry["is_video"] = True
                entry["readable"] = True
                entry["media_type"] = _mime or "video/mp4"
                entry["tokens"] = 500
            else:
                entry["readable"] = True
                entry["content"] = f"[첨부파일: {_fname} ({_fext})] — file_id 기반 파일"
                entry["tokens"] = 10
            results.append(entry)
            continue

        # ── 인라인 video base64 (브라우저에서 직접 전달 — Gemini Video API로 분석)
        if att.get("type") == "video" and att.get("base64"):
            entry["base64_data"] = att["base64"]
            entry["media_type"] = att.get("media_type", "video/mp4")
            entry["is_video"] = True
            entry["readable"] = True
            entry["tokens"] = len(att["base64"]) // 2000  # base64 길이 기반 토큰 추정
            results.append(entry)
            continue

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

        # Path traversal protection: reject paths containing '..'
        if ".." in file_path:
            entry["error"] = "path_traversal_rejected"
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

        if f.get("is_video") and readable:
            summaries.append(f"[첨부동영상: {name} ({ext})]")
        elif f.get("is_image") and readable:
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


def _is_sensitive_path(path: str) -> bool:
    """extra_paths 로 읽어서는 안 되는 경로인가."""
    try:
        resolved = os.path.realpath(path)
    except Exception:
        return True
    for prefix in SENSITIVE_PATH_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            return True
    return False


def _downscale_to_limit(raw: bytes, name: str) -> Optional[bytes]:
    """장변 VISION_MAX_DIMENSION 이하로 리샘플 + PNG 재인코딩하여 크기 한도에 맞춘다.

    한 번에 안 맞으면 장변을 0.75배씩 줄이며 최대 5회 재시도한다.
    끝내 못 맞추면 None (호출자가 건너뛴다).
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning(f"[Vision] Pillow unavailable — cannot resize {name}")
        return None

    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            limit = VISION_MAX_DIMENSION
            for _ in range(6):
                work = im.copy()
                work.thumbnail((limit, limit), Image.LANCZOS)
                buf = io.BytesIO()
                work.save(buf, format="PNG", optimize=True)
                data = buf.getvalue()
                if len(data) <= IMAGE_MAX_BYTES:
                    return data
                limit = int(limit * 0.75) or 1
    except Exception as e:
        logger.warning(f"[Vision] resize failed: {name} — {type(e).__name__}: {e}")
        return None

    logger.warning(f"[Vision] resize could not reach limit: {name} — skipped")
    return None


def _convert_to_png(raw: bytes, name: str) -> Optional[bytes]:
    """bmp/tiff/ico 등 → PNG 바이트. 실패 시 None."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning(f"[Vision] Pillow unavailable — cannot convert {name}")
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception as e:
        logger.warning(f"[Vision] convert failed: {name} — {type(e).__name__}: {e}")
        return None


def _convert_heif_to_png(raw: bytes, name: str) -> Optional[bytes]:
    """heic/heif → PNG 바이트. pillow_heif 가 없으면 None (호출자가 사유를 남긴다)."""
    try:
        import pillow_heif  # type: ignore
    except ImportError:
        return None
    try:
        pillow_heif.register_heif_opener()
    except Exception as e:
        logger.warning(f"[Vision] pillow_heif register failed: {name} — {type(e).__name__}: {e}")
        return None
    return _convert_to_png(raw, name)


def _build_block_from_bytes(
    raw: bytes,
    name: str,
    ext: str,
    media_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """원본 바이트 하나 → Anthropic content block 하나.

    포맷 변환(bmp→png), 5MB 초과 리샘플, PDF document block, 미지원 포맷 건너뜀을
    이 함수 한 곳에서 처리한다. 반환 block 의 키 구조는 기존과 동일하다.
    """
    ext = (ext or "").lower()
    if not raw:
        return None

    if ext in HEIF_EXTENSIONS or media_type in HEIF_MEDIA_TYPES:
        converted = _convert_heif_to_png(raw, name)
        if converted is None:
            logger.info(
                f"[Vision] [unsupported_image_format: {ext or '.heic'}] {name} — "
                "pillow_heif 미설치로 건너뜀"
            )
            return None
        raw = converted
        ext = ".png"
        media_type = "image/png"
        logger.info(f"[Vision] converted HEIF to PNG: {name}")

    if ext in UNSUPPORTED_IMAGE_EXTENSIONS:
        logger.info(f"[Vision] [unsupported_image_format: {ext}] {name} — skipped")
        return None

    if ext in PDF_EXTENSIONS or media_type == "application/pdf":
        if len(raw) > PDF_MAX_BYTES:
            logger.warning(
                f"[Vision] pdf too large: {name} size={len(raw)} > {PDF_MAX_BYTES} — skipped"
            )
            return None
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(raw).decode("utf-8"),
            },
        }

    original_size = len(raw)
    resized = False

    if ext in CONVERTIBLE_IMAGE_EXTENSIONS:
        converted = _convert_to_png(raw, name)
        if converted is None:
            return None
        raw = converted
        media_type = "image/png"
        logger.info(f"[Vision] converted to PNG: {name} ({ext}) {original_size}→{len(raw)} bytes")
    elif ext in IMAGE_EXTENSIONS:
        media_type = media_type or IMAGE_MEDIA_TYPES.get(ext, "image/jpeg")
    elif media_type and media_type.startswith("image/"):
        # 확장자는 모르지만 mime 이 이미지라고 말한다 (uploaded_files 경로)
        pass
    else:
        logger.info(f"[Vision] [unsupported_image_format: {ext or 'unknown'}] {name} — skipped")
        return None

    if len(raw) > IMAGE_MAX_BYTES:
        shrunk = _downscale_to_limit(raw, name)
        if shrunk is None:
            return None
        raw = shrunk
        media_type = "image/png"
        resized = True

    if resized:
        logger.info(
            f"[Vision] image block prepared: {name} resized=True "
            f"original_size={original_size} final_size={len(raw)}"
        )
    else:
        logger.debug(f"[Vision] image block prepared: {name} ({media_type})")

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type or "image/jpeg",
            "data": base64.b64encode(raw).decode("utf-8"),
        },
    }


def extract_image_blocks(
    file_contents: List[Dict[str, Any]],
    extra_paths: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Claude Vision API 형식의 이미지 content block 목록 추출.

    Args:
        file_contents: extract_file_contents() 결과 (또는 같은 모양의 dict 목록)
        extra_paths: 디스크 절대경로 목록. 지정하면 같은 변환 파이프라인을 통과해
            blocks 뒤에 덧붙는다. 미지정(None)이면 기존과 완전히 같은 동작.

    Returns:
        list of {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
        (PDF 는 {"type": "document", ...})
    """
    return build_vision_blocks(file_contents, extra_paths=extra_paths)


def build_vision_blocks(
    file_contents: List[Dict[str, Any]],
    extra_paths: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """첨부 목록 + 디스크 경로 → Anthropic vision content block 목록.

    비전 입력을 만드는 단 하나의 출입구다 (AADS-VISION-UNIFY). 호출처마다
    image block 을 인라인으로 만들면 포맷 변환·크기 한도·중복 제거가 제각각이 된다.

    - png/jpg/jpeg/gif/webp → 네이티브 image block (원본 그대로)
    - bmp/tiff/ico/ppm/pcx → Pillow 로 PNG 변환 후 image block
    - 5MB 초과 → 장변 VISION_MAX_DIMENSION 으로 축소해 전송 (버리지 않는다)
    - pdf → Anthropic 네이티브 document block (PDF_MAX_BYTES 초과 시 건너뜀)
    - heic/heif → pillow_heif 가 있으면 PNG 변환, 없으면 사유를 남기고 건너뜀
    - SHA-256 으로 같은 바이트를 두 번 싣지 않는다

    Args:
        file_contents: extract_file_contents() 결과 (또는 같은 모양의 dict 목록)
        extra_paths: 디스크 절대경로 목록. 같은 변환 파이프라인을 통과해
            blocks 뒤에 덧붙는다. 민감 경로(SENSITIVE_PATH_PREFIXES)는 거부한다.

    Returns:
        list of {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
        (PDF 는 {"type": "document", ...})
    """
    blocks: List[Dict[str, Any]] = []
    seen: set = set()

    def _append(raw: bytes, name: str, ext: str, media_type: Optional[str]) -> None:
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            logger.debug(f"[Vision] duplicate skipped: {name}")
            return
        block = _build_block_from_bytes(raw, name, ext, media_type)
        if block is None:
            return
        seen.add(digest)
        blocks.append(block)

    for f in file_contents or []:
        if not f.get("readable", True) or not f.get("base64_data"):
            continue
        media_type = f.get("media_type")
        is_pdf = (f.get("ext", "").lower() in PDF_EXTENSIONS) or media_type == "application/pdf"
        if not f.get("is_image") and not is_pdf:
            continue
        try:
            raw = base64.b64decode(f["base64_data"])
        except Exception as e:
            logger.warning(f"[Vision] base64 decode failed: {f.get('name')} — {e}")
            continue
        _append(raw, f.get("name", "unknown"), f.get("ext", ""), media_type)

    for path in extra_paths or []:
        if _is_sensitive_path(path):
            logger.warning(f"[Vision] sensitive path refused: {path}")
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except Exception as e:
            logger.warning(f"[Vision] extra_path read failed: {path} — {type(e).__name__}: {e}")
            continue
        name = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        _append(raw, name, ext, None)

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
