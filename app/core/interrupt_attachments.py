"""스트리밍 중 '추가 지시' 첨부를 모델 입력으로 바꾸는 단일 출입구.

지연 적용 경로(chat_service)·도구 루프(model_selector)·SDK 주입(agent_sdk_service)이
같은 규칙으로 이미지를 싣도록 한 곳에 모은다. 예전에는 지연 적용 경로가 첨부를
`- image.png (image)` 텍스트 줄로만 적고 base64 본문을 버려 모델이 이미지를 보지 못했다.

- 이미지: build_vision_blocks(5MB 상한·포맷 변환·중복 제거) → fit_image_blocks(relay 1MiB 예산)
- 이미지 외 파일: read_uploaded_file 로 열 수 있는 참조(file_id/경로)를 텍스트로 남긴다.
  이미 저장된 파일은 그 참조를 그대로 쓰고, 본문만 있는 파일만 chat_files 에 저장한다.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

NOTE_HEADER = "\n\n[추가 지시 첨부파일]\n"

_MAGIC_PREFIXES: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _strip_data_url(b64: str) -> str:
    if b64.startswith("data:") and "," in b64:
        return b64.split(",", 1)[1]
    return b64


def sniff_image_media_type(b64: str, hint: Optional[str] = None) -> str:
    """base64 앞부분의 매직바이트로 media_type 을 판정한다.

    판정 실패 시 hint(image/*)가 있으면 hint, 없으면 image/png.
    바이트와 선언이 다르면 바이트가 이긴다 — Anthropic 은 불일치 이미지를 거부한다.
    """
    head = b""
    try:
        data = _strip_data_url(b64 or "")[:32]
        data = data[: len(data) // 4 * 4]
        head = base64.b64decode(data)
    except Exception:
        head = b""
    for prefix, media_type in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if isinstance(hint, str) and hint.lower().startswith("image/"):
        return hint.lower()
    return "image/png"


def _iter_attachments(interrupts: Iterable[Any]) -> Iterable[Dict[str, Any]]:
    for item in interrupts or []:
        if not isinstance(item, dict):
            continue
        for att in item.get("attachments") or []:
            if isinstance(att, dict):
                yield att


def _is_inline_image(att: Dict[str, Any]) -> bool:
    media = str(att.get("media_type") or att.get("mime_type") or "")
    is_image = att.get("type") == "image" or media.startswith("image/")
    b64 = att.get("base64")
    return bool(is_image and isinstance(b64, str) and b64)


def build_interrupt_image_blocks(
    interrupts: Iterable[Any],
    *,
    fmt: str = "anthropic",
) -> Tuple[List[Dict[str, Any]], str]:
    """추가 지시 첨부 → (이미지 블록 목록, 제외 안내문).

    fmt="anthropic": {"type":"image","source":{"type":"base64",...}}  (Claude/CLI relay/SDK)
    fmt="image_url": {"type":"image_url","image_url":{"url":"data:...;base64,..."}}  (LiteLLM)
    안내문이 비어 있으면 빠진 이미지가 없다. CPU 작업(Pillow)이 있을 수 있어 동기 함수다.
    """
    from app.core.document_context import build_vision_blocks
    from app.core.vision_budget import fit_image_blocks

    entries: List[Dict[str, Any]] = []
    names: List[str] = []
    for att in _iter_attachments(interrupts):
        if not _is_inline_image(att):
            continue
        b64 = _strip_data_url(att["base64"])
        name = str(att.get("name") or att.get("filename") or "image")
        entries.append({
            "name": name,
            "ext": os.path.splitext(name)[1].lower(),
            "base64_data": b64,
            "media_type": sniff_image_media_type(b64, att.get("media_type") or att.get("mime_type")),
            "is_image": True,
            "readable": True,
        })
        names.append(name)

    if not entries:
        return [], ""

    try:
        blocks = build_vision_blocks(entries)
        notice = ""
        if blocks:
            blocks, notice = fit_image_blocks(blocks, names)
    except Exception as exc:
        logger.warning("interrupt_image_build_failed: %s: %s", type(exc).__name__, str(exc)[:160])
        blocks, notice = [], ""

    if len(blocks) < len(entries) and not notice:
        notice = f"[추가 지시 이미지 {len(entries) - len(blocks)}장은 형식·크기 문제로 전달하지 못했습니다]"

    if fmt == "image_url":
        blocks = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"},
            }
            for b in blocks
            if b.get("type") == "image"
        ]
    return blocks, notice


async def _default_saver(session_id: str, name: str, media_type: str, data: bytes) -> str:
    from types import SimpleNamespace

    from app.services.chat_service import save_chat_file

    result = await save_chat_file(
        session_id, SimpleNamespace(filename=name, content_type=media_type), data,
    )
    return str(result["file_id"])


async def build_interrupt_attachment_payload(
    session_id: str,
    interrupts: Iterable[Any],
    *,
    fmt: str = "anthropic",
    saver: Optional[Callable[[str, str, str, bytes], Awaitable[str]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """추가 지시 첨부 → (본문에 덧붙일 텍스트, 이미지 블록 목록).

    첨부가 없으면 ("", []) — 호출처의 기존 텍스트가 그대로 유지된다.
    """
    import asyncio

    atts = list(_iter_attachments(interrupts))
    if not atts:
        return "", []

    blocks, notice = await asyncio.to_thread(build_interrupt_image_blocks, interrupts, fmt=fmt)
    save = saver or _default_saver

    lines: List[str] = []
    for att in atts:
        name = att.get("name") or att.get("filename") or "attachment"
        kind = att.get("type") or att.get("media_type") or "file"
        line = f"- {name} ({kind})"
        if _is_inline_image(att):
            line += " — 이미지로 이 메시지에 포함됨" if blocks else ""
            lines.append(line)
            continue
        ref = ""
        fid = att.get("file_id") or att.get("saved_file_id")
        path = att.get("path")
        if fid:
            ref = f'read_uploaded_file(filename="{fid}")'
        elif path:
            ref = f"경로 {path}"
        elif isinstance(att.get("base64"), str) and att["base64"]:
            try:
                raw = base64.b64decode(_strip_data_url(att["base64"]))
                fid = await save(
                    session_id, str(name), str(att.get("media_type") or "application/octet-stream"), raw,
                )
                att["saved_file_id"] = fid
                ref = f'read_uploaded_file(filename="{fid}")'
            except Exception as exc:
                logger.warning(
                    "interrupt_attachment_persist_failed session=%s name=%s: %s: %s",
                    str(session_id)[:8], name, type(exc).__name__, str(exc)[:160],
                )
        if ref:
            line += f" — {ref} 로 열 수 있음"
        lines.append(line)

    note = NOTE_HEADER + "\n".join(lines)
    if notice:
        note += "\n" + notice
    return note, blocks


def attach_images(text: str, blocks: List[Dict[str, Any]]) -> Any:
    """이미지 블록이 있으면 [text, *images] content 배열, 없으면 text 그대로."""
    if not blocks:
        return text
    return [{"type": "text", "text": text}] + list(blocks)
