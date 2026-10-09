"""통장사본 판독 (ACCT-OBYS-BANKBOOK-AUTO-EXTRACT-20261009).

직원이 올린 통장사본에서 은행명·예금주·계좌번호(마스킹)를 읽어 계약서에 싣는다.

- OCR 은 app.core.local_ocr_bridge.ocr_extract 를 그대로 쓴다(새 의존성 없음).
- 계좌번호 원문은 이 모듈의 지역 변수 밖으로 나가지 않는다. 반환값·로그·저장 어디에도 없다.
  OCR 원문 텍스트도 남기지 않는다.
- 읽지 못했거나 신뢰할 수 없으면 값을 비우고 extract_status=needs_review 와 사유 코드만 돌려준다.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.core import local_ocr_bridge

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

STATUS_EXTRACTED = "extracted"
STATUS_NEEDS_REVIEW = "needs_review"
NEEDS_REVIEW_LABEL = "계좌 확인 필요"
SOURCE_PRINTED = "printed"
SOURCE_INFERRED = "inferred"

OcrFunc = Callable[[bytes], Awaitable[dict[str, Any]]]

# (표준 이름, 통장에 인쇄되는 표기들). 공백을 뺀 대문자 문자열에서 찾는다.
BANK_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("IBK기업은행", ("IBK기업은행", "기업은행", "IBK")),
    ("KB국민은행", ("KB국민은행", "국민은행")),
    ("신한은행", ("신한은행",)),
    ("우리은행", ("우리은행",)),
    ("하나은행", ("하나은행", "KEB하나은행", "외환은행")),
    ("NH농협은행", ("NH농협은행", "농협은행", "농협")),
    ("카카오뱅크", ("카카오뱅크",)),
    ("토스뱅크", ("토스뱅크",)),
    ("케이뱅크", ("케이뱅크",)),
    ("SC제일은행", ("SC제일은행", "제일은행")),
    ("한국씨티은행", ("씨티은행",)),
    ("부산은행", ("부산은행",)),
    ("대구은행", ("대구은행", "IM뱅크", "iM뱅크")),
    ("광주은행", ("광주은행",)),
    ("전북은행", ("전북은행",)),
    ("경남은행", ("경남은행",)),
    ("제주은행", ("제주은행",)),
    ("수협은행", ("수협은행", "수협")),
    ("새마을금고", ("새마을금고",)),
    ("신협", ("신협",)),
    ("우체국", ("우체국",)),
)

# 은행별 계좌번호 자릿수(숫자만). prefix 가 있으면 그 자리까지 맞아야 한다.
# 여기에 없는 은행은 GENERIC_LENGTHS 로만 점검한다 — 모르는 형식을 만들어 내지 않는다.
BANK_RULES: dict[str, dict[str, Any]] = {
    "IBK기업은행": {"lengths": {12, 14}},
    "KB국민은행": {"lengths": {12, 14}},
    "신한은행": {"lengths": {11, 12, 14}},
    "우리은행": {"lengths": {12, 13}, "prefix_by_length": {13: "100"}},
    "하나은행": {"lengths": {11, 12, 14}},
    "NH농협은행": {"lengths": {12, 13, 14}},
    "카카오뱅크": {"lengths": {13}, "prefix_by_length": {13: "3333"}},
    "토스뱅크": {"lengths": {12}, "prefix_by_length": {12: "1000"}},
    "케이뱅크": {"lengths": {12}, "prefix_by_length": {12: "100"}},
    "SC제일은행": {"lengths": {11, 14}},
    "우체국": {"lengths": {13, 14}},
}
GENERIC_LENGTHS = range(10, 17)

# 구분자 없이 붙어 나온 숫자열을 묶을 때 쓰는 은행별 표준 묶음.
GROUPING: dict[tuple[str, int], tuple[int, ...]] = {
    ("우리은행", 13): (4, 3, 6),
    ("IBK기업은행", 14): (3, 6, 2, 3),
    ("KB국민은행", 14): (6, 2, 6),
    ("하나은행", 14): (3, 6, 5),
    ("NH농협은행", 13): (3, 4, 4, 2),
    ("카카오뱅크", 13): (4, 2, 7),
    ("토스뱅크", 12): (4, 4, 4),
    ("케이뱅크", 12): (3, 3, 6),
}
# 은행명이 인쇄돼 있지 않을 때 계좌 형식만으로 추정하는 규칙(묶음 길이 -> 은행).
INFER_BY_GROUPS: dict[tuple[int, ...], str] = {
    (3, 6, 2, 3): "IBK기업은행",
    (3, 4, 4, 2): "NH농협은행",
    (4, 3, 6): "우리은행",
    (4, 2, 7): "카카오뱅크",
    (4, 4, 4): "토스뱅크",
}
INFER_BY_PREFIX: tuple[tuple[str, int, str], ...] = (
    ("3333", 13, "카카오뱅크"),
    ("1000", 12, "토스뱅크"),
    ("1002", 13, "우리은행"),
    ("1005", 13, "우리은행"),
)

HOLDER_LABELS = ("예금주명", "예금주", "계좌주", "고객명", "성명")
ACCOUNT_LABELS = ("계좌번호", "계좌")
NAME_STOPWORDS = frozenset({
    "계좌번호", "계좌", "예금주", "은행", "고객", "통장", "번호", "지점", "조회", "이체", "거래", "명세", "신규",
    "예금", "입출금", "보통예금", "저축예금", "사본",
})

_DASH = r"\-–—−"
_PHONE = re.compile(r"^01[016789]\d{7,8}$")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def min_confidence() -> float:
    return _env_float("OBYS_BANKBOOK_MIN_CONFIDENCE", 0.5)


def ocr_timeout_sec() -> float:
    return _env_float("OBYS_BANKBOOK_OCR_TIMEOUT_SEC", 45.0)


# --- 마스킹 · 검증 -----------------------------------------------------------
def mask_account(groups: list[str]) -> str:
    """첫 묶음 전체와 끝 4자리만 남기고 나머지 숫자는 * 로 가린다. 구분자는 유지한다.

    1002-123-456846 -> 1002-***-**6846,  348-123456-01-018 -> 348-******-*1-018
    """
    groups = [g for g in groups if g]
    if not groups:
        return ""
    total = sum(len(g) for g in groups)
    visible_from = max(total - 4, len(groups[0]))
    out: list[str] = []
    position = 0
    for index, group in enumerate(groups):
        chars = []
        for char in group:
            keep = index == 0 or position >= visible_from
            chars.append(char if keep else "*")
            position += 1
        out.append("".join(chars))
    return "-".join(out)


def _regroup(bank: str | None, digits: str) -> list[str]:
    pattern = GROUPING.get((bank or "", len(digits)))
    if pattern:
        groups, start = [], 0
        for size in pattern:
            groups.append(digits[start:start + size])
            start += size
        return groups
    return [digits[:3], digits[3:-4], digits[-4:]] if len(digits) > 7 else [digits]


def account_format_ok(bank: str | None, digits: str) -> bool:
    if not digits.isdigit() or _PHONE.match(digits):
        return False
    rule = BANK_RULES.get(bank or "")
    if not rule:
        return len(digits) in GENERIC_LENGTHS
    if len(digits) not in rule["lengths"]:
        return False
    prefix = rule.get("prefix_by_length", {}).get(len(digits))
    return not prefix or digits.startswith(prefix)


def infer_bank(groups: list[str]) -> str | None:
    """은행명이 없을 때 계좌 형식으로만 추정한다. 모호하면 None."""
    digits = "".join(groups)
    if len(groups) > 1:
        bank = INFER_BY_GROUPS.get(tuple(len(g) for g in groups))
        if bank and account_format_ok(bank, digits):
            return bank
    for prefix, length, bank in INFER_BY_PREFIX:
        if len(digits) == length and digits.startswith(prefix) and account_format_ok(bank, digits):
            return bank
    return None


# --- 텍스트 해석 ------------------------------------------------------------
def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def detect_banks(text: str) -> list[str]:
    """텍스트에 인쇄된 은행을 등장 순서대로."""
    compact = _compact(text)
    found: list[tuple[int, str]] = []
    for name, aliases in BANK_ALIASES:
        positions = [compact.find(alias.upper()) for alias in aliases]
        positions = [p for p in positions if p >= 0]
        if positions:
            found.append((min(positions), name))
    return [name for _, name in sorted(found)]


_TIGHT = re.compile(rf"(?<!\d)(\d{{2,6}}(?:\s*[{_DASH}]\s*\d{{1,8}}){{1,4}})(?!\d)")
_LOOSE = re.compile(rf"(?<!\d)(\d{{2,6}}(?:[\s{_DASH}]+\d{{1,8}}){{1,4}})(?!\d)")
_RUN = re.compile(r"(?<![\d*])(\d{10,16})(?![\d*])")


def _groups_of(raw: str) -> list[str]:
    return [g for g in re.split(rf"[\s{_DASH}]+", raw) if g]


def _is_excluded(groups: list[str]) -> bool:
    sizes = tuple(len(g) for g in groups)
    digits = "".join(groups)
    if _PHONE.match(digits):
        return True
    # 사업자등록번호(3-2-5)·주민등록번호(6-7) 모양은 계좌가 아니다.
    return sizes in {(3, 2, 5), (6, 7)}


def account_candidates(text: str) -> list[list[str]]:
    """계좌번호 후보(묶음 목록). 라벨이 붙은 줄의 후보가 앞에 온다. 중복 제거."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    labelled, other = [], []
    for line in lines:
        compact = line.replace(" ", "")
        is_labelled = any(label in compact for label in ACCOUNT_LABELS)
        if is_labelled:
            tail = line
            for label in ACCOUNT_LABELS:
                spaced = r"\s*".join(re.escape(c) for c in label)
                tail = re.sub(spaced, " ", tail, count=1)
            for pattern in (_LOOSE, _TIGHT):
                labelled += [_groups_of(m.group(1)) for m in pattern.finditer(tail)]
        other += [_groups_of(m.group(1)) for m in _TIGHT.finditer(line)]
        other += [[m.group(1)] for m in _RUN.finditer(line)]
    seen: set[str] = set()
    ordered: list[list[str]] = []
    for groups in [*labelled, *other]:
        key = "".join(groups)
        if not groups or key in seen or _is_excluded(groups):
            continue
        seen.add(key)
        ordered.append(groups)
    return ordered


def _hangul_name(tokens: list[str]) -> str:
    if not tokens:
        return ""
    first = re.sub(r"[^가-힣]", "", tokens[0])
    if len(first) >= 2:
        return first if 2 <= len(first) <= 5 and first not in NAME_STOPWORDS else ""
    joined = ""
    for token in tokens[:5]:
        cleaned = re.sub(r"[^가-힣]", "", token)
        if len(cleaned) != 1 or len(joined) >= 4:
            break
        joined += cleaned
    return joined if 2 <= len(joined) <= 5 and joined not in NAME_STOPWORDS else ""


def find_holder(text: str) -> str:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    for label in HOLDER_LABELS:
        spaced = r"\s*".join(re.escape(c) for c in label)
        for index, line in enumerate(lines):
            match = re.search(spaced, line)
            if not match:
                continue
            rest = re.sub(r"^[\s:：=\-]+", "", line[match.end():])
            name = _hangul_name(rest.split())
            if name:
                return name
            if not rest.strip() and index + 1 < len(lines):
                name = _hangul_name(lines[index + 1].split())
                if name:
                    return name
    return ""


def parse_bankbook_text(text: str) -> dict[str, Any]:
    """OCR 텍스트에서 은행명·예금주·마스킹 계좌를 뽑는다. 계좌 원문은 반환하지 않는다."""
    printed = detect_banks(text)
    holder = find_holder(text)
    bank, source, masked = "", "", ""
    for groups in account_candidates(text):
        digits = "".join(groups)
        match = next((b for b in printed if account_format_ok(b, digits)), None)
        if match:
            bank, source = match, SOURCE_PRINTED
        elif not printed:
            inferred = infer_bank(groups)
            if inferred:
                bank, source = inferred, SOURCE_INFERRED
            elif account_format_ok(None, digits):
                bank, source = "", ""
            else:
                continue
        else:
            continue
        shown = groups if len(groups) > 1 else _regroup(bank or None, digits)
        masked = mask_account(shown)
        break
    reasons: list[str] = []
    if not masked:
        reasons.append("account_not_found" if not account_candidates(text) else "account_format_mismatch")
    if not bank:
        reasons.append("bank_unknown" if masked else "bank_not_found")
    if not holder:
        reasons.append("holder_not_found")
    return {
        "bank_name": bank,
        "bank_name_source": source,
        "bank_account_holder": holder,
        "bank_account_masked": masked,
        "reasons": reasons,
        "score": sum(1 for v in (bank, holder, masked) if v),
    }


# --- 이미지 보정 · 판독 ------------------------------------------------------
def prepare_images(data: bytes) -> tuple[bytes, bytes | None]:
    """EXIF 회전을 적용한 정방향 이미지와 180도 돌린 이미지. 이미지가 아니면 (원본, None)."""
    if data[:4] == b"%PDF":
        return data, None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return data, None
    try:
        with Image.open(io.BytesIO(data)) as image:
            upright = ImageOps.exif_transpose(image).convert("RGB")
            first, second = io.BytesIO(), io.BytesIO()
            upright.save(first, format="PNG")
            upright.rotate(180).save(second, format="PNG")
            return first.getvalue(), second.getvalue()
    except Exception as exc:  # noqa: BLE001 — 보정 실패는 원본 한 번 판독으로 계속한다
        logger.info("통장사본 이미지 보정 건너뜀: %s", type(exc).__name__)
        return data, None


async def _default_ocr(image: bytes) -> dict[str, Any]:
    return await local_ocr_bridge.ocr_extract(image_base64=base64.b64encode(image).decode("ascii"))


def _confidence(raw: Any) -> float:
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if value > 1:
        value /= 100
    return max(0.0, min(value, 1.0))


async def _attempt(image: bytes, rotation: int, ocr: OcrFunc) -> dict[str, Any]:
    try:
        result = await ocr(image)
    except Exception as exc:  # noqa: BLE001 — OCR 실패는 needs_review 로 남긴다
        logger.info("통장사본 OCR 실패 rotation=%s: %s", rotation, type(exc).__name__)
        return {"rotation": rotation, "ocr_ok": False, "confidence": 0.0, "score": 0,
                "reasons": ["ocr_failed"], "bank_name": "", "bank_name_source": "",
                "bank_account_holder": "", "bank_account_masked": ""}
    text = str((result or {}).get("text") or "")
    parsed = parse_bankbook_text(text) if text.strip() else {
        "bank_name": "", "bank_name_source": "", "bank_account_holder": "", "bank_account_masked": "",
        "reasons": ["ocr_empty"], "score": 0,
    }
    return {**parsed, "rotation": rotation, "ocr_ok": True, "confidence": _confidence((result or {}).get("confidence"))}


def _is_complete(attempt: dict[str, Any], threshold: float) -> bool:
    if attempt["score"] < 3:
        return False
    return not (0 < attempt["confidence"] < threshold)


def _finalize(attempt: dict[str, Any], attempts: int, threshold: float, now: datetime | None) -> dict[str, Any]:
    complete = _is_complete(attempt, threshold)
    reasons = list(attempt["reasons"])
    if attempt["score"] >= 3 and not complete:
        reasons.append("low_confidence")
    stamp = (now or datetime.now(KST)).isoformat(timespec="seconds")
    base = {
        "extract_status": STATUS_EXTRACTED if complete else STATUS_NEEDS_REVIEW,
        "extract_reason": "" if complete else ",".join(dict.fromkeys(reasons)) or "unreadable",
        "extract_rotation": attempt["rotation"],
        "extract_attempts": attempts,
        "extract_confidence": round(attempt["confidence"], 3),
        "extracted_at": stamp,
    }
    if not complete:
        return {**base, "bank_name": "", "bank_name_source": "", "bank_account_holder": "", "bank_account_masked": ""}
    return {
        **base,
        "bank_name": attempt["bank_name"],
        "bank_name_source": attempt["bank_name_source"],
        "bank_account_holder": attempt["bank_account_holder"],
        "bank_account_masked": attempt["bank_account_masked"],
    }


async def extract_bankbook(
    data: bytes,
    *,
    ocr: OcrFunc | None = None,
    threshold: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """통장사본을 판독한다. 항상 dict 를 돌려주며 예외를 밖으로 던지지 않는다."""
    run_ocr = ocr or _default_ocr
    limit = min_confidence() if threshold is None else threshold
    try:
        upright, flipped = await asyncio.to_thread(prepare_images, data)
        attempt = await _attempt(upright, 0, run_ocr)
        attempts = 1
        if not _is_complete(attempt, limit) and flipped is not None:
            retry = await _attempt(flipped, 180, run_ocr)
            attempts = 2
            # 점수가 같으면 정방향 결과를 유지한다.
            if (retry["score"], retry["confidence"]) > (attempt["score"], attempt["confidence"]):
                attempt = retry
        return _finalize(attempt, attempts, limit, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("통장사본 판독 오류: %s", type(exc).__name__)
        failed = {"rotation": 0, "confidence": 0.0, "score": 0, "reasons": ["extract_error"]}
        return _finalize(failed, 1, limit, now)


async def extract_bankbook_bounded(data: bytes, **kwargs: Any) -> dict[str, Any]:
    """업로드 응답을 오래 붙잡지 않도록 시간 상한을 건다. 초과하면 needs_review."""
    try:
        return await asyncio.wait_for(extract_bankbook(data, **kwargs), timeout=ocr_timeout_sec())
    except asyncio.TimeoutError:
        failed = {"rotation": 0, "confidence": 0.0, "score": 0, "reasons": ["timeout"]}
        return _finalize(failed, 1, 0.0, kwargs.get("now"))


# --- 서류 레코드 반영 --------------------------------------------------------
def apply_to_record(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """판독 결과를 입사서류 레코드에 쓴다. 이미 읽은 값은 실패한 재판독으로 지우지 않는다."""
    previous = record.get("extracted_fields") if isinstance(record.get("extracted_fields"), dict) else {}
    keep_previous = (
        result["extract_status"] == STATUS_NEEDS_REVIEW
        and record.get("extract_status") == STATUS_EXTRACTED
        and bool(previous.get("bank_account_masked"))
    )
    if keep_previous:
        record["extract_last_failed_at"] = result["extracted_at"]
        record["extract_last_failed_reason"] = result["extract_reason"]
        return record
    for key in ("extract_last_failed_at", "extract_last_failed_reason"):
        record.pop(key, None)
    record["extract_status"] = result["extract_status"]
    record["extract_reason"] = result["extract_reason"]
    record["extract_rotation"] = result["extract_rotation"]
    record["extract_attempts"] = result["extract_attempts"]
    record["extract_confidence"] = result["extract_confidence"]
    record["extracted_at"] = result["extracted_at"]
    if result["extract_status"] == STATUS_EXTRACTED:
        record["extracted_fields"] = {
            "bank_name": result["bank_name"],
            "bank_account_holder": result["bank_account_holder"],
            "bank_account_masked": result["bank_account_masked"],
            "bank_name_source": result["bank_name_source"],
        }
    else:
        record["extracted_fields"] = {}
    return record
