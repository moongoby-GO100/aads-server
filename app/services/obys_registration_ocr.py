"""사업자등록증 OCR 제안 (AADS-OBYS-BIZLICENSE-ORIGINAL-OCR-20260930).

OCR 결과는 **제안만** 한다. 사업자 레코드에 쓰지 않는다 — 저장은 관리자가
확인한 뒤 기존 update_business / 기초설정 저장 경로로 이루어진다.

- OCR 호출은 app.core.local_ocr_bridge.ocr_extract 를 그대로 쓴다.
- 읽지 못한 항목은 값을 만들지 않고 value=None + reason 으로 돌려준다.
- 감사로그에는 항목별 성공/실패와 길이만 남긴다. OCR 원문·추출값은 남기지 않는다.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.core import local_ocr_bridge
from app.services import obys_upload_service as upload_svc

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

AUTOFILL_FIELDS = ("name", "registration_no", "representative", "opened_at", "address")
EXTRA_FIELDS = ("corporate_registration_no", "tax_type")
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "corporate_registration_no": ("법인등록번호",),
    "registration_no": ("사업자등록번호", "등록번호"),
    "name": ("법인명(단체명)", "법인명", "단체명", "상호"),
    "representative": ("대표자", "성명"),
    "opened_at": ("개업연월일", "개업년월일", "개업일자", "개업일"),
    "address": ("사업장소재지", "본점소재지", "사업장주소", "소재지"),
}
# 값 뒤에 같은 줄로 붙어 나오는 다른 항목. 여기서 값을 끊는다.
STOP_LABELS = (
    "생년월일", "주민등록번호", "법인등록번호", "전화번호", "사업의종류", "업태", "종목",
    "발급사유", "공동사업자", "개업연월일", "사업장소재지", "본점소재지", "대표자", "성명", "상호",
)
EXCLUDED_PREFIXES = {"registration_no": ("법인", "주민")}
TAX_TYPES = (("간이과세자", "간이과세"), ("일반과세자", "일반과세"), ("면세사업자", "면세"))


def _spaced(label: str) -> str:
    return r"\s*".join(re.escape(char) for char in label.replace(" ", ""))


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _snippet(line: str) -> str:
    return _clean(line)[:80]


def _field(field: str, value: Any = None, *, confidence: float = 0.0, source: str | None = None,
           valid: bool = False, reason: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "valid": bool(valid and value is not None),
        "confidence": round(confidence, 3) if value is not None else 0.0,
        "source": source,
        "reason": reason,
        "autofill_candidate": bool(valid and value is not None and field in AUTOFILL_FIELDS),
    }


def _cut_at_stop_label(value: str, own_labels: tuple[str, ...]) -> str:
    own = {label.replace(" ", "") for label in own_labels}
    end = len(value)
    for label in STOP_LABELS:
        if label in own:
            continue
        match = re.search(_spaced(label), value)
        if match and match.start() > 0:
            end = min(end, match.start())
    return value[:end]


def _labelled_value(lines: list[str], field: str) -> tuple[str, str, float] | None:
    """(value, source line, factor) for the first line carrying the field label."""
    labels = FIELD_LABELS[field]
    for index, line in enumerate(lines):
        for label in labels:
            for match in re.finditer(_spaced(label), line):
                before = re.sub(r"\s+", "", line[:match.start()])
                if any(before.endswith(prefix) for prefix in EXCLUDED_PREFIXES.get(field, ())):
                    continue
                rest = re.sub(r"^\s*(?:\([^)]*\))?\s*[:：]?\s*", "", line[match.end():])
                value = _clean(_cut_at_stop_label(rest, labels))
                if value:
                    return value, _snippet(line), 1.0
                # 표 형식이라 값이 다음 줄에 있는 경우. 다음 줄이 또 다른 항목이면 포기한다.
                following = next((item for item in lines[index + 1:] if item.strip()), "")
                compact = re.sub(r"\s+", "", following)
                if following and not any(compact.startswith(stop) for stop in (*STOP_LABELS, "등록번호", "사업장")):
                    return _clean(_cut_at_stop_label(following, labels)), _snippet(following), 0.8
                return "", _snippet(line), 1.0
    return None


# --- 검증 규칙 ---------------------------------------------------------------
def business_registration_checksum_ok(digits: str) -> bool:
    """국세청 사업자등록번호 검증번호(10번째 자리)."""
    if not re.fullmatch(r"\d{10}", digits):
        return False
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    numbers = [int(char) for char in digits]
    total = sum(number * weight for number, weight in zip(numbers, weights))
    total += (numbers[8] * 5) // 10
    return (10 - total % 10) % 10 == numbers[9]


def normalize_registration_no(raw: str) -> tuple[str | None, bool, str | None]:
    """Return (formatted value, valid, reason)."""
    match = re.search(r"(?<!\d)(\d{3})\s*-?\s*(\d{2})\s*-?\s*(\d{5})(?!\d)", str(raw or ""))
    if not match:
        return None, False, "사업자등록번호 10자리를 읽지 못했습니다"
    digits = "".join(match.groups())
    formatted = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if not business_registration_checksum_ok(digits):
        return formatted, False, "사업자등록번호 검증번호(체크섬)가 맞지 않습니다"
    return formatted, True, None


def normalize_opened_at(raw: str, *, today: date | None = None) -> tuple[str | None, bool, str | None]:
    match = re.search(r"(\d{4})\s*(?:년|[.\-/])\s*(\d{1,2})\s*(?:월|[.\-/])\s*(\d{1,2})", str(raw or ""))
    if not match:
        return None, False, "개업일 날짜 형식을 읽지 못했습니다"
    try:
        opened = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None, False, "존재하지 않는 날짜입니다"
    if opened > (today or datetime.now(KST).date()):
        return opened.isoformat(), False, "개업일이 미래 날짜입니다"
    return opened.isoformat(), True, None


def _confidence(raw: Any) -> float:
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if value > 1:
        value /= 100
    return max(0.0, min(value, 1.0))


def parse_registration_text(text: str, ocr_confidence: Any = 0.0, *, today: date | None = None) -> dict[str, dict[str, Any]]:
    """Extract suggestion fields from OCR text. Never invents a value."""
    base = _confidence(ocr_confidence)
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    result: dict[str, dict[str, Any]] = {}
    if not lines:
        for field in (*AUTOFILL_FIELDS, *EXTRA_FIELDS):
            result[field] = _field(field, reason="OCR 결과에 글자가 없습니다")
        return result

    for field in ("name", "representative", "address"):
        found = _labelled_value(lines, field)
        if not found:
            result[field] = _field(field, reason="등록증에서 해당 항목 표시를 찾지 못했습니다")
        elif not found[0]:
            result[field] = _field(field, source=found[1], reason="항목 표시는 있으나 값을 읽지 못했습니다")
        else:
            result[field] = _field(field, found[0], confidence=base * found[2], source=found[1], valid=True)

    found = _labelled_value(lines, "registration_no")
    factor, source, raw = (found[2], found[1], found[0]) if found else (0.6, None, "")
    if not found or not raw:
        # 표시를 못 찾았으면 원문 전체에서 000-00-00000 모양이 정확히 한 종류일 때만 쓴다.
        candidates = {m.group(0) for m in re.finditer(r"(?<![\d-])\d{3}-\d{2}-\d{5}(?![\d-])", "\n".join(lines))}
        if len(candidates) == 1:
            raw = candidates.pop()
            source = _snippet(next(line for line in lines if raw in line))
            factor = 0.6
    value, valid, reason = normalize_registration_no(raw) if raw else (None, False, "사업자등록번호를 찾지 못했습니다")
    result["registration_no"] = _field("registration_no", value, confidence=base * factor, source=source, valid=valid, reason=reason)

    found = _labelled_value(lines, "opened_at")
    if not found or not found[0]:
        result["opened_at"] = _field("opened_at", source=found[1] if found else None, reason="개업연월일을 찾지 못했습니다")
    else:
        value, valid, reason = normalize_opened_at(found[0], today=today)
        result["opened_at"] = _field("opened_at", value, confidence=base * found[2], source=found[1], valid=valid, reason=reason)

    found = _labelled_value(lines, "corporate_registration_no")
    corp = re.search(r"(?<!\d)(\d{6})\s*-\s*(\d{7})(?!\d)", found[0]) if found and found[0] else None
    result["corporate_registration_no"] = (
        _field("corporate_registration_no", f"{corp.group(1)}-{corp.group(2)}", confidence=base * found[2], source=found[1], valid=True)
        if corp else _field("corporate_registration_no", reason="법인등록번호가 없거나 읽지 못했습니다")
    )

    compact = re.sub(r"\s+", "", "\n".join(lines))
    tax = next(((marker, label) for marker, label in TAX_TYPES if marker in compact), None)
    result["tax_type"] = (
        _field("tax_type", tax[1], confidence=base, source=tax[0], valid=True)
        if tax else _field("tax_type", reason="과세유형 표시를 찾지 못했습니다")
    )
    return result


def _audit_summary(suggested: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        field: {
            "found": item["value"] is not None,
            "valid": item["valid"],
            "value_length": len(str(item["value"])) if item["value"] is not None else 0,
        }
        for field, item in suggested.items()
    }


async def suggest_from_document(*, user: dict[str, Any], business_id: str, document_id: UUID) -> dict[str, Any]:
    document, business, data = await upload_svc.read_registration_document(
        user=user, business_id=business_id, document_id=document_id,
    )
    started = time.monotonic()
    error: str | None = None
    text, confidence = "", 0.0
    try:
        ocr = await local_ocr_bridge.ocr_extract(image_base64=base64.b64encode(data).decode("ascii"))
        text, confidence = str(ocr.get("text") or ""), ocr.get("confidence") or 0.0
        if ocr.get("error") and not text.strip():
            error = str(ocr["error"])[:200]
    except Exception as exc:  # noqa: BLE001 — OCR 실패는 손입력으로 계속한다
        error = f"OCR 실행 실패: {str(exc)[:200]}"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if error:
        suggested = {field: _field(field, reason=error) for field in (*AUTOFILL_FIELDS, *EXTRA_FIELDS)}
    else:
        suggested = parse_registration_text(text, confidence)

    current = {field: business.get(field) or "" for field in (*AUTOFILL_FIELDS, "tax_type")}
    for field, item in suggested.items():
        item["current"] = current.get(field)
        item["matches_current"] = item["value"] is not None and item["value"] == (current.get(field) or None)

    audit_logged = True
    try:
        await upload_svc.record_registration_audit(
            user=user, business_id=business_id, document_id=document["id"],
            action="business_registration.ocr_suggest",
            details={
                "document_id": document["id"],
                "version": document.get("version"),
                "ocr_ok": error is None,
                "elapsed_ms": elapsed_ms,
                "text_length": len(text),
                "fields": _audit_summary(suggested),
                "error": error,
            },
        )
    except Exception:  # noqa: BLE001
        audit_logged = False
        logger.warning("business registration OCR audit insert failed business=%s document=%s", business_id, document["id"])

    return {
        "document": document,
        "business_id": business_id,
        "ocr": {"ok": error is None, "error": error, "confidence": _confidence(confidence), "elapsed_ms": elapsed_ms, "text_length": len(text)},
        "suggested": suggested,
        "current": current,
        "needs_registration_info": business.get("needs_registration_info", False),
        "missing_registration_fields": business.get("missing_registration_fields", []),
        "applied": False,
        "audit_logged": audit_logged,
    }
