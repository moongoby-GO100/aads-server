"""Tenant-scoped OBYS business registry and durable ledger uploads."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from openpyxl import load_workbook

from app.core.obys_db import obys_db_url

MAX_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
UPLOAD_ROOT = Path("app/data/yeoljeong_finance/uploads/ledgers")
CATEGORY_EXTENSIONS = {
    "sales": {".csv", ".xlsx", ".pdf", ".jpg", ".jpeg", ".png"},
    "purchase": {".csv", ".xlsx", ".pdf", ".jpg", ".jpeg", ".png"},
    "transaction": {".csv", ".xlsx"},
}
MIME_BY_EXTENSION = {
    ".csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".pdf": {"application/pdf"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
}
SIGNATURES = {
    ".pdf": b"%PDF-",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".xlsx": b"PK\x03\x04",
}


def _tenant(user: dict[str, Any]) -> UUID:
    try:
        return UUID(str(user.get("tenant_id") or ""))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=403, detail="유효한 테넌트가 필요합니다") from exc


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("email") or user.get("user_id") or user.get("sub") or "unknown")[:320]


def _safe_name(filename: str) -> str:
    if not filename or Path(filename).name != filename or "\x00" in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="안전하지 않은 파일명입니다")
    cleaned = re.sub(r"[^0-9A-Za-z._가-힣 -]", "_", filename).strip(" .")
    if not cleaned:
        raise HTTPException(status_code=400, detail="파일명이 비어 있습니다")
    return cleaned[:240]


def _validate_file(category: str, filename: str, content_type: str, data: bytes) -> tuple[str, str]:
    if category not in CATEGORY_EXTENSIONS:
        raise HTTPException(status_code=400, detail="지원하지 않는 업로드 구분입니다")
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in CATEGORY_EXTENSIONS[category]:
        raise HTTPException(status_code=415, detail="지원하지 않는 파일 형식입니다")
    if not data:
        raise HTTPException(status_code=422, detail="빈 원장은 업로드할 수 없습니다")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="파일은 10MB 이하여야 합니다")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in MIME_BY_EXTENSION[ext]:
        raise HTTPException(status_code=415, detail="확장자와 MIME 형식이 일치하지 않습니다")
    signature = SIGNATURES.get(ext)
    if signature and not data.startswith(signature):
        raise HTTPException(status_code=415, detail="파일 서명이 확장자와 일치하지 않습니다")
    if ext == ".xlsx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                unpacked = sum(item.file_size for item in members)
                packed = sum(max(item.compress_size, 1) for item in members)
                if len(members) > 2000 or unpacked > MAX_UNPACKED_BYTES or unpacked / max(packed, 1) > 100:
                    raise HTTPException(status_code=413, detail="Excel 압축 해제 한도를 초과했습니다")
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=422, detail="손상된 Excel 파일입니다") from exc
    return name, ext


def _raw_rows(ext: str, data: bytes) -> list[dict[str, Any]]:
    try:
        if ext == ".csv":
            text = None
            for encoding in ("utf-8-sig", "cp949"):
                try:
                    text = data.decode(encoding)
                    break
                except UnicodeDecodeError:
                    pass
            if text is None:
                raise HTTPException(status_code=422, detail="CSV 문자 인코딩을 읽을 수 없습니다")
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or not any(str(item or "").strip() for item in reader.fieldnames):
                raise HTTPException(status_code=422, detail="CSV 헤더가 없습니다")
            rows = list(reader)
        else:
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            values = book.active.iter_rows(values_only=True)
            headers = [str(value or "").strip() for value in next(values, ())]
            if not headers or not any(headers):
                raise HTTPException(status_code=422, detail="Excel 헤더가 없습니다")
            rows = [dict(zip(headers, row)) for row in values if any(value not in (None, "") for value in row)]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="원장 파일을 해석할 수 없습니다") from exc
    if not rows:
        raise HTTPException(status_code=422, detail="빈 원장은 업로드할 수 없습니다")
    return rows


def _value(row: dict[str, Any], *names: str) -> Any:
    def normalize(value: Any) -> str:
        return re.sub(r"[\s_()-]", "", str(value)).lower()

    normalized = {normalize(key): value for key, value in row.items()}
    return next((normalized[normalize(name)] for name in names if normalize(name) in normalized), "")


def _canonical(category: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    accepted: list[dict[str, Any]] = []
    rejected = 0
    for row in rows:
        raw_date = _value(row, "date", "거래일자", "매출일자", "일자")
        raw_amount = _value(row, "amount", "금액", "매출액", "결제금액", "입금액", "출금액")
        try:
            occurred = raw_date.date() if isinstance(raw_date, datetime) else date.fromisoformat(str(raw_date)[:10].replace(".", "-").replace("/", "-"))
            amount = Decimal(re.sub(r"[^0-9.-]", "", str(raw_amount)))
        except (ValueError, InvalidOperation):
            rejected += 1
            continue
        counterparty = str(_value(row, "counterparty", "거래처", "매입처", "가맹점", "상대방") or "").strip()
        description = str(_value(row, "description", "적요", "항목", "품목", "메모") or "").strip()
        stable = "|".join((category, occurred.isoformat(), str(amount), counterparty, description))
        accepted.append({"occurred_on": occurred, "amount": amount, "counterparty": counterparty, "description": description, "source_hash": hashlib.sha256(stable.encode()).hexdigest(), "payload": row})
    if not accepted:
        raise HTTPException(status_code=422, detail="반영할 수 있는 원장 행이 없습니다")
    return accepted, rejected


async def _connect():
    import asyncpg

    return await asyncpg.connect(obys_db_url(), timeout=5)


async def _require_business(conn: Any, tenant_id: UUID, business_id: str) -> None:
    owns = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM yeoljeong_businesses WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL)",
        business_id,
        tenant_id,
    )
    if not owns:
        raise HTTPException(status_code=403, detail="해당 사업자는 현재 테넌트 소유가 아닙니다")


async def list_businesses(*, user: dict[str, Any]) -> list[dict[str, Any]]:
    tenant_id = _tenant(user)
    conn = await _connect()
    try:
        rows = await conn.fetch("SELECT id,entity_type,name,registration_no,representative,tax_type,opened_at,address,memo,created_at,updated_at FROM yeoljeong_businesses WHERE tenant_id=$1 AND deleted_at IS NULL ORDER BY sort_order,id", tenant_id)
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def create_business(*, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(user)
    business_id = str(payload.get("id") or f"biz-{uuid4().hex[:16]}").strip()
    name = str(payload.get("name") or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", business_id):
        raise HTTPException(status_code=400, detail="사업자 ID와 상호를 확인하십시오")
    conn = await _connect()
    try:
        try:
            row = await conn.fetchrow("""INSERT INTO yeoljeong_businesses (id,tenant_id,entity_type,name,registration_no,representative,tax_type,opened_at,address,memo,sort_order,updated_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,(SELECT COALESCE(MAX(sort_order),0)+1 FROM yeoljeong_businesses WHERE tenant_id=$2),$11) RETURNING *""", business_id, tenant_id, str(payload.get("entity_type") or "individual"), name, str(payload.get("registration_no") or ""), str(payload.get("representative") or ""), str(payload.get("tax_type") or ""), str(payload.get("opened_at") or ""), str(payload.get("address") or ""), str(payload.get("memo") or ""), _actor(user))
        except Exception as exc:
            if exc.__class__.__name__ == "UniqueViolationError":
                raise HTTPException(status_code=409, detail="이미 사용 중인 사업자 ID입니다") from exc
            raise
        return dict(row)
    finally:
        await conn.close()


async def update_business(*, user: dict[str, Any], business_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(user)
    allowed = {"name", "registration_no", "representative", "tax_type", "opened_at", "address", "memo", "entity_type"}
    updates = {key: str(value) for key, value in payload.items() if key in allowed and value is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="변경할 항목이 없습니다")
    columns = list(updates)
    assignments = ",".join(f"{column}=${index}" for index, column in enumerate(columns, 1))
    conn = await _connect()
    try:
        row = await conn.fetchrow(f"UPDATE yeoljeong_businesses SET {assignments},updated_by=${len(columns)+1},updated_at=NOW() WHERE id=${len(columns)+2} AND tenant_id=${len(columns)+3} AND deleted_at IS NULL RETURNING *", *[updates[column] for column in columns], _actor(user), business_id, tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="사업자를 찾을 수 없습니다")
        return dict(row)
    finally:
        await conn.close()


async def create_upload(*, user: dict[str, Any], business_id: str, category: str, filename: str, content_type: str, data: bytes) -> dict[str, Any]:
    tenant_id = _tenant(user)
    original, ext = _validate_file(category, filename, content_type, data)
    parsed, rejected = ([], 0) if ext in {".pdf", ".jpg", ".jpeg", ".png"} else _canonical(category, _raw_rows(ext, data))
    digest, upload_id = hashlib.sha256(data).hexdigest(), uuid4()
    stored_name = f"{upload_id.hex}{ext}"
    directory = UPLOAD_ROOT / str(tenant_id) / hashlib.sha256(business_id.encode()).hexdigest()[:32] / category
    target = directory / stored_name
    conn = await _connect()
    try:
        await _require_business(conn, tenant_id, business_id)
        existing = await conn.fetchrow("SELECT * FROM yeoljeong_uploads WHERE tenant_id=$1 AND business_id=$2 AND category=$3 AND sha256=$4 AND deleted_at IS NULL", tenant_id, business_id, category, digest)
        if existing:
            result = dict(existing)
            result["status"] = "duplicate"
            return result
        directory.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        status = "pending_review" if not parsed else "imported"
        async with conn.transaction():
            await conn.execute("""INSERT INTO yeoljeong_uploads (id,tenant_id,business_id,category,original_filename,stored_filename,content_type,byte_size,sha256,status,rejected_rows,created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""", upload_id, tenant_id, business_id, category, original, stored_name, content_type, len(data), digest, status, rejected, _actor(user))
            imported = duplicates = 0
            for row in parsed:
                command = await conn.execute("""INSERT INTO yeoljeong_uploaded_ledger_rows (id,tenant_id,business_id,upload_id,category,source_hash,occurred_on,counterparty,description,amount,payload) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb) ON CONFLICT (tenant_id,business_id,category,source_hash) DO NOTHING""", uuid4(), tenant_id, business_id, upload_id, category, row["source_hash"], row["occurred_on"], row["counterparty"], row["description"], row["amount"], json.dumps(row["payload"], ensure_ascii=False, default=str))
                imported += command.endswith(" 1")
                duplicates += command.endswith(" 0")
            await conn.execute("UPDATE yeoljeong_uploads SET imported_rows=$2,duplicate_rows=$3,status=$4 WHERE id=$1", upload_id, imported, duplicates, "duplicate" if parsed and not imported else status)
        return {"id": str(upload_id), "tenant_id": str(tenant_id), "business_id": business_id, "category": category, "original_filename": original, "status": "duplicate" if parsed and not imported else status, "imported_rows": imported, "duplicate_rows": duplicates, "rejected_rows": rejected}
    except Exception:
        if target.exists():
            target.unlink()
        raise
    finally:
        await conn.close()


async def list_uploads(*, user: dict[str, Any], business_id: str, category: str | None = None) -> list[dict[str, Any]]:
    tenant_id = _tenant(user)
    conn = await _connect()
    try:
        await _require_business(conn, tenant_id, business_id)
        rows = await conn.fetch("SELECT id,business_id,category,original_filename,content_type,byte_size,sha256,status,imported_rows,duplicate_rows,rejected_rows,error_message,created_by,created_at FROM yeoljeong_uploads WHERE tenant_id=$1 AND business_id=$2 AND deleted_at IS NULL AND ($3::text IS NULL OR category=$3) ORDER BY created_at DESC LIMIT 100", tenant_id, business_id, category)
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def list_ledger_rows(*, user: dict[str, Any], business_id: str, category: str, limit: int = 500) -> list[dict[str, Any]]:
    tenant_id = _tenant(user)
    if category not in CATEGORY_EXTENSIONS:
        raise HTTPException(status_code=400, detail="지원하지 않는 원장 구분입니다")
    conn = await _connect()
    try:
        await _require_business(conn, tenant_id, business_id)
        rows = await conn.fetch("SELECT id,upload_id,category,occurred_on,counterparty,description,amount,payload,created_at FROM yeoljeong_uploaded_ledger_rows WHERE tenant_id=$1 AND business_id=$2 AND category=$3 ORDER BY occurred_on DESC NULLS LAST,created_at DESC LIMIT $4", tenant_id, business_id, category, min(max(limit, 1), 1000))
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def download_path(*, user: dict[str, Any], upload_id: UUID) -> tuple[Path, str, str]:
    tenant_id = _tenant(user)
    conn = await _connect()
    try:
        row = await conn.fetchrow("SELECT business_id,category,stored_filename,original_filename,content_type FROM yeoljeong_uploads WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL", upload_id, tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="업로드 파일을 찾을 수 없습니다")
        await _require_business(conn, tenant_id, row["business_id"])
        path = UPLOAD_ROOT / str(tenant_id) / hashlib.sha256(row["business_id"].encode()).hexdigest()[:32] / row["category"] / row["stored_filename"]
        if not path.is_file() or UPLOAD_ROOT.resolve() not in path.resolve().parents:
            raise HTTPException(status_code=404, detail="업로드 파일을 찾을 수 없습니다")
        return path, row["original_filename"], row["content_type"]
    finally:
        await conn.close()


async def delete_upload(*, user: dict[str, Any], upload_id: UUID) -> bool:
    tenant_id = _tenant(user)
    conn = await _connect()
    try:
        row = await conn.fetchrow("SELECT business_id FROM yeoljeong_uploads WHERE id=$1 AND tenant_id=$2 AND deleted_at IS NULL", upload_id, tenant_id)
        if not row:
            raise HTTPException(status_code=404, detail="업로드 파일을 찾을 수 없습니다")
        await _require_business(conn, tenant_id, row["business_id"])
        # Canonical rows are derived from this upload. Remove them atomically so
        # a same-SHA retry can import fresh rows instead of hitting stale hashes.
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM yeoljeong_uploaded_ledger_rows WHERE upload_id=$1 AND tenant_id=$2",
                upload_id,
                tenant_id,
            )
            await conn.execute("UPDATE yeoljeong_uploads SET deleted_at=NOW() WHERE id=$1 AND tenant_id=$2", upload_id, tenant_id)
        return True
    finally:
        await conn.close()
