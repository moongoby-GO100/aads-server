"""신분증·등본 판독. OCR 원문과 주민번호는 결과/로그에 보존하지 않는다."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any

from app.services import obys_bankbook_extract as bb

DOCUMENT_TYPES = {"id_card", "resident_register"}
# OCR 은 주민번호 구분자 "-" 를 "." "·" "," 로 읽기도 한다(운영 서류 실측 2026-11-06).
_RRN = re.compile(r"(?<!\d)(\d{6})\s*[-–.·,]?\s*([1-8])[\d*]{6}(?!\d)")
_ADDRESS = re.compile(r"(?:(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|전라|경상|전북|제주)[가-힣]*"
                      r"|[가-힣]{1,3}\s?(?:특별시|광역시|특별자치시|특별자치도))(?:\s+[^\n]+)")
# 특별시는 서울 하나뿐이다. OCR 이 "서울"을 다른 글자로 읽어도(예: 서올특별시) 서울특별시로 바로잡는다.
_SPECIAL_CITY = re.compile(r"^[가-힣]{1,3}\s?특별시")
_RELATION_WORDS = {"본인", "세대주", "배우자", "자녀", "남편", "아들", "관계", "주민등록번호", "성명", "전입일", "변동사유"}


def _join_spaced_hangul(line: str) -> str:
    """OCR 이 한글을 한 글자씩 띄워 읽은 줄("서 울 특별시 성 북 구")에서 연달은 한 글자 토큰끼리만 붙인다."""
    out: list[str] = []
    prev_single = False
    for token in line.split():
        single = bool(re.fullmatch(r"[가-힣]", token))
        if single and prev_single:
            out[-1] += token
        else:
            out.append(token)
        prev_single = single
    return " ".join(out)


def parse_identity_text(text: str, document_type: str, employee_name: str = "") -> dict[str, str]:
    birth = ""
    lines = text.splitlines()
    if document_type == "resident_register":
        name = re.sub(r"\s+", "", employee_name)
        pattern = re.compile(r"(?<![가-힣])" + r"\s*".join(map(re.escape, name)) + r"(?![가-힣])") if name else None
        hits = [i for i, line in enumerate(lines) if pattern and pattern.search(line)]
        picked = []
        for i in hits:
            picked.append(lines[i])
            # 등본 표는 이름과 주민번호가 다른 줄로 읽히기도 한다. 이름 줄에 번호가 없고, 바로 다음 줄에
            # 다른 사람 이름이 없을 때만 그 줄의 번호를 쓴다(다른 세대원 번호를 가져오지 않는다).
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            words = set(re.findall(r"[가-힣]{2,4}", _RRN.sub("", nxt)))
            if not _RRN.search(lines[i]) and _RRN.search(nxt) and words <= _RELATION_WORDS:
                picked.append(nxt)
        lines = picked
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
        match = _ADDRESS.search(_join_spaced_hangul(line))
        if match:
            address = _SPECIAL_CITY.sub("서울특별시", match.group().strip())
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
            elif confidence >= limit:
                # 신뢰도가 기준 이상이면 찾은 값만이라도 남긴다(주소를 못 읽었다고 생년월일까지 버리지 않는다).
                result.update({key: value for key, value in fields.items() if value})
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
