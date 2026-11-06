"""신분증·등본 판독. OCR 원문과 주민번호는 결과/로그에 보존하지 않는다."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any

from app.services import obys_bankbook_extract as bb

DOCUMENT_TYPES = {"id_card", "resident_register"}
_RRN = re.compile(r"(?<!\d)(\d{6})\s*[-–]?\s*([1-8])[\d*]{6}(?!\d)")
_ADDRESS = re.compile(r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|전라|경상|제주)[가-힣]*(?:\s+[^\n]+)")


def parse_identity_text(text: str, document_type: str, employee_name: str = "") -> dict[str, str]:
    birth = ""
    lines = text.splitlines()
    if document_type == "resident_register":
        name = re.sub(r"\s+", "", employee_name)
        lines = [line for line in lines if name and re.search(
            r"(?<![가-힣])" + r"\s*".join(map(re.escape, name)) + r"(?![가-힣])", line)]
    candidates = set()
    for line in lines:
        for match in _RRN.finditer(line):
            raw, century = match.groups()
            try:
                candidates.add(date((1900 if century in "1256" else 2000) + int(raw[:2]),
                                    int(raw[2:4]), int(raw[4:6])).isoformat())
            except ValueError:
                pass
    if len(candidates) == 1:
        birth = candidates.pop()
    # Remove identifiers BEFORE considering an address; no OCR fragment is returned elsewhere.
    safe = _RRN.sub("", text)
    addresses = []
    for line in safe.splitlines():
        match = _ADDRESS.search(line)
        if match:
            address = match.group().strip()
            if not re.search(r"\d{6,}|주민등록|발급|성명", address):
                addresses.append(address)
    return {"birth_date": birth, "address": addresses[0] if addresses else ""}


def failure(reason: str) -> dict[str, Any]:
    return {"birth_date": "", "address": "", "extract_status": "needs_review",
            "extract_reason": reason, "extract_confidence": 0.0,
            "extracted_at": datetime.now(bb.KST).isoformat(), "extract_attempts": 0, "extract_rotation": 0}


async def extract_identity(data: bytes, *, document_type: str, employee_name: str = "",
                           ocr=None, threshold: float | None = None) -> dict[str, Any]:
    limit = bb._env_float("OBYS_IDENTITY_MIN_CONFIDENCE", 0.5) if threshold is None else threshold
    best = failure("birth_not_found,address_not_found")
    best_score = (-1, -1.0)
    try:
        upright, flipped = await asyncio.to_thread(bb.prepare_images, data)
        for count, image in enumerate((upright, flipped), 1):
            if image is None:
                break
            raw = await (ocr or bb._default_ocr)(image)
            fields = parse_identity_text(str((raw or {}).get("text") or ""), document_type, employee_name)
            confidence = bb._confidence((raw or {}).get("confidence"))
            reasons = [f"{key.split('_')[0]}_not_found" for key, value in fields.items() if not value]
            if confidence < limit:
                reasons.append("low_confidence")
            score = (sum(bool(v) for v in fields.values()), confidence)
            result = {**failure(",".join(reasons)), "extract_confidence": confidence,
                      "extract_attempts": count, "extract_rotation": 0 if count == 1 else 180}
            if not reasons:
                result.update(fields, extract_status="extracted")
            if score > best_score:
                best, best_score = result, score
            best["extract_attempts"] = count
            if not reasons:
                break
        return best
    except Exception:
        return failure("ocr_failed")


async def extract_identity_bounded(data: bytes, **kwargs) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(extract_identity(data, **kwargs),
                                      timeout=bb._env_float("OBYS_IDENTITY_OCR_TIMEOUT_SEC", 45.0))
    except Exception:
        return failure("timeout_or_extract_failed")


def apply_to_record(record: dict[str, Any], result: dict[str, Any]) -> None:
    if result["extract_status"] == "needs_review" and record.get("extract_status") == "extracted":
        record["extract_last_failed_reason"] = result["extract_reason"]
        record["extract_last_failed_at"] = result["extracted_at"]
        return
    for key in ("extract_status", "extract_reason", "extract_confidence", "extracted_at", "extract_attempts", "extract_rotation"):
        record[key] = result[key]
    record["extracted_fields"] = {k: result[k] for k in ("birth_date", "address") if result.get(k)}
