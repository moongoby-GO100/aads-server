"""붙여넣은 이미지(inline base64) 영속화·재개·재조회 (AADS-CHAT-PASTED-IMAGE-PERSIST-RESUME).

DB 와 디스크는 mock/tmp 로 대체한다.
"""
from __future__ import annotations

import base64
import logging
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from app.core import document_context as dc
from app.services import chat_service as cs

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 32
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def _pool_returning(conn):
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


# ① 저장 + 요약에 file_id ---------------------------------------------------

async def test_inline_image_is_saved_and_summary_carries_file_id(monkeypatch):
    fid = str(uuid.uuid4())
    save = AsyncMock(return_value={"file_id": fid})
    monkeypatch.setattr(cs, "save_chat_file", save)

    attachments = [{"type": "image", "base64": PNG_B64, "media_type": "image/png"}]
    saved = await cs._persist_inline_image_attachments(str(uuid.uuid4()), attachments)

    assert saved[0]["file_id"] == fid
    assert saved[0]["name"].startswith("pasted-") and saved[0]["name"].endswith(".png")
    _, file_like, data = save.await_args.args
    assert data == PNG_BYTES
    assert file_like.filename == saved[0]["name"] and file_like.content_type == "image/png"
    # 이번 턴 vision 은 base64 경로 그대로여야 한다 — file_id 로 바꾸면 안 된다.
    assert attachments[0]["saved_file_id"] == fid
    assert "file_id" not in attachments[0]

    entries = dc.extract_file_contents(attachments)
    entries[0]["file_id"] = fid
    entries[0]["name"] = saved[0]["name"]
    entries[0]["ext"] = dc.image_ext_from_media_type("image/png")
    summary = dc.build_file_reference_summary(entries)
    assert summary == f"[첨부이미지: {saved[0]['name']} (.png) file_id={fid}]"
    assert PNG_B64 not in summary
    assert len(dc.build_vision_blocks(dc.extract_file_contents(attachments))) == 1


async def test_named_inline_image_keeps_name_and_persist_failure_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(cs, "save_chat_file", AsyncMock(side_effect=RuntimeError("disk full")))
    attachments = [{"type": "image", "name": "shot.png", "base64": PNG_B64}]
    with caplog.at_level(logging.WARNING):
        saved = await cs._persist_inline_image_attachments(str(uuid.uuid4()), attachments)
    assert saved == {}
    assert "saved_file_id" not in attachments[0]
    assert any("persist failed" in r.message for r in caplog.records)
    assert PNG_B64 not in caplog.text


def test_summary_without_file_id_is_unchanged_shape():
    summary = dc.build_file_reference_summary(
        [{"name": "a.png", "ext": ".png", "tokens": 1, "readable": True, "is_image": True}]
    )
    assert summary == "[첨부이미지: a.png (.png)]"


def test_attach_log_summary_has_no_base64():
    text = dc.summarize_attachments_for_log([{"type": "image", "name": "image.png", "base64": PNG_B64}])
    assert PNG_B64 not in text
    assert "type=image" in text and f"len={len(PNG_B64)}" in text


# ② 요약 → file_id 파싱 → vision 블록 재구성 -------------------------------------

def test_extract_image_file_ids_roundtrip():
    fid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    body = (
        "이거 봐줘\n\n"
        f"[첨부이미지: image.png (.png) file_id={fid}]\n"
        f"[첨부이미지: image.png (.png) file_id={fid}]\n"
        f"[첨부이미지: b.jpg (.jpg) file_id={other}]\n"
        "[첨부이미지: old.png ()]"
    )
    assert dc.extract_image_file_ids(body) == [fid, other]
    assert dc.extract_image_file_ids("file_id=" + fid) == []
    assert dc.extract_image_file_ids(None) == []


async def test_resume_rebuilds_vision_block_from_summary(monkeypatch, tmp_path):
    sid = str(uuid.uuid4())
    fid = str(uuid.uuid4())
    img = tmp_path / f"{fid}.webp"
    img.write_bytes(PNG_BYTES)
    monkeypatch.setattr(cs, "get_chat_file", AsyncMock(return_value={
        "session_id": uuid.UUID(sid), "mime_type": "image/png", "storage_path": str(img),
    }))
    body = f"봐줘\n\n[첨부이미지: image.png (.png) file_id={fid}]"
    blocks, text = await cs._build_resume_vision_blocks(
        sid, [{"role": "assistant", "content": "x"}, {"role": "user", "content": body}],
    )
    assert text == body
    assert len(blocks) == 1
    src = blocks[0]["source"]
    assert blocks[0]["type"] == "image" and src["type"] == "base64"
    assert src["media_type"] == "image/webp"  # 실제 저장 바이트(webp)에 맞춘다
    assert base64.b64decode(src["data"]) == PNG_BYTES


async def test_resume_ignores_file_of_other_session_and_missing_refs(monkeypatch, tmp_path):
    fid = str(uuid.uuid4())
    img = tmp_path / "a.png"
    img.write_bytes(PNG_BYTES)
    monkeypatch.setattr(cs, "get_chat_file", AsyncMock(return_value={
        "session_id": uuid.uuid4(), "mime_type": "image/png", "storage_path": str(img),
    }))
    body = f"[첨부이미지: a.png (.png) file_id={fid}]"
    blocks, _ = await cs._build_resume_vision_blocks(
        str(uuid.uuid4()), [{"role": "user", "content": body}],
    )
    assert blocks == []
    assert await cs._build_resume_vision_blocks("s", [{"role": "user", "content": "평문"}]) == ([], "")


# ③ read_uploaded_file 의 chat_files 폴백 ----------------------------------------

async def test_read_uploaded_file_falls_back_to_chat_files(monkeypatch, tmp_path):
    from app.core import db_pool
    from app.services import tool_executor as te

    fid = uuid.uuid4()
    img = tmp_path / "p.webp"
    img.write_bytes(PNG_BYTES)
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])  # chat_drive_files: 없음
    conn.fetchrow = AsyncMock(return_value={
        "id": fid, "session_id": uuid.uuid4(), "original_name": "image.png",
        "mime_type": "image/png", "file_size": len(PNG_BYTES),
        "storage_path": str(img), "created_at": None,
    })
    monkeypatch.setattr(db_pool, "get_pool", lambda: _pool_returning(conn))

    executor = te.ToolExecutor.__new__(te.ToolExecutor)
    token = te.current_chat_session_id.set(str(uuid.uuid4()))
    try:
        result = await executor._read_uploaded_file({"filename": "image.png"})
    finally:
        te.current_chat_session_id.reset(token)

    assert result["status"] == "image"
    assert result["file_id"] == str(fid)
    assert result["filename"] == "image.png"
    assert result["media_type"] == "image/png"
    assert result["path"] == str(img)
    assert "hint" in result
    sql = conn.fetchrow.await_args.args[0]
    assert "chat_files" in sql and "ILIKE" in sql


async def test_read_uploaded_file_not_found_when_both_tables_miss(monkeypatch):
    from app.core import db_pool
    from app.services import tool_executor as te

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(db_pool, "get_pool", lambda: _pool_returning(conn))
    executor = te.ToolExecutor.__new__(te.ToolExecutor)
    result = await executor._read_uploaded_file({"filename": "nope.png"})
    assert result["status"] == "not_found"
