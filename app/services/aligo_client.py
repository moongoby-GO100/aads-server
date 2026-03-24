"""알리고(Aligo) REST API 클라이언트 — 카카오 알림톡 + SMS 발송."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# 알리고 API Base URLs
_SMS_BASE = "https://apis.aligo.in"
_KAKAO_BASE = "https://kakaoapi.aligo.in"

# 환경변수에서 인증 정보 로드
_API_KEY: Optional[str] = None
_USER_ID: Optional[str] = None
_SENDER: Optional[str] = None
_SENDER_KEY: Optional[str] = None


def _load_config() -> bool:
    """환경변수에서 알리고 설정 로드. 설정 완료 시 True."""
    global _API_KEY, _USER_ID, _SENDER, _SENDER_KEY
    _API_KEY = os.environ.get("ALIGO_API_KEY")
    _USER_ID = os.environ.get("ALIGO_USER_ID")
    _SENDER = os.environ.get("ALIGO_SENDER")
    _SENDER_KEY = os.environ.get("ALIGO_SENDER_KEY")
    return bool(_API_KEY and _USER_ID)


def is_available() -> bool:
    """알리고 서비스 사용 가능 여부."""
    if _API_KEY is None:
        _load_config()
    return bool(_API_KEY and _USER_ID)


def _auth_params() -> Dict[str, str]:
    """공통 인증 파라미터."""
    _load_config()
    return {"key": _API_KEY or "", "user_id": _USER_ID or ""}


async def send_sms(
    receiver: str,
    msg: str,
    sender: Optional[str] = None,
    *,
    title: Optional[str] = None,
    rdate: Optional[str] = None,
    rtime: Optional[str] = None,
    testmode: bool = False,
) -> Dict[str, Any]:
    """SMS 발송 (단문/장문 자동 판별).

    Args:
        receiver: 수신번호 (010XXXXXXXX)
        msg: 메시지 내용
        sender: 발신번호 (미지정 시 환경변수 ALIGO_SENDER)
        title: LMS/MMS 제목
        rdate: 예약일 (YYYYMMDD)
        rtime: 예약시간 (HHMM)
        testmode: 테스트모드

    Returns:
        알리고 API 응답 dict
    """
    if not is_available():
        return {"result_code": -1, "message": "알리고 미설정 (ALIGO_API_KEY/ALIGO_USER_ID 필요)"}

    data = {
        **_auth_params(),
        "sender": sender or _SENDER or "",
        "receiver": receiver,
        "msg": msg,
    }

    # 90바이트 초과 시 LMS 자동 전환
    if len(msg.encode("euc-kr", errors="replace")) > 90:
        data["msg_type"] = "LMS"
        if title:
            data["title"] = title

    if rdate:
        data["rdate"] = rdate
    if rtime:
        data["rtime"] = rtime
    if testmode:
        data["testmode_yn"] = "Y"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_SMS_BASE}/send/", data=data)
            result = resp.json()
            logger.info("aligo SMS 발송: receiver=%s result_code=%s", receiver, result.get("result_code"))
            return result
    except Exception as e:
        logger.error("aligo SMS 발송 실패: %s", e)
        return {"result_code": -999, "message": str(e)}


async def send_alimtalk(
    receiver: str,
    template_code: str,
    message: str,
    *,
    subject: Optional[str] = None,
    emtitle: Optional[str] = None,
    button: Optional[str] = None,
    failover_sms: bool = True,
    failover_subject: Optional[str] = None,
    failover_message: Optional[str] = None,
) -> Dict[str, Any]:
    """카카오 알림톡 발송.

    Args:
        receiver: 수신번호
        template_code: 알림톡 템플릿 코드
        message: 메시지 내용 (템플릿에 맞는 치환 완료된 텍스트)
        subject: 제목
        emtitle: 강조표기 제목
        button: 버튼 JSON 문자열
        failover_sms: 알림톡 실패 시 SMS 폴백 여부
        failover_subject: SMS 폴백 제목
        failover_message: SMS 폴백 메시지

    Returns:
        알리고 API 응답 dict
    """
    if not is_available():
        return {"code": -1, "message": "알리고 미설정"}

    if not _SENDER_KEY:
        return {"code": -2, "message": "ALIGO_SENDER_KEY 미설정 (카카오 알림톡 발신 프로필 키 필요)"}

    data = {
        "apikey": _API_KEY or "",
        "userid": _USER_ID or "",
        "senderkey": _SENDER_KEY,
        "tpl_code": template_code,
        "sender": _SENDER or "",
        "receiver_1": receiver,
        "subject_1": subject or template_code,
        "message_1": message,
    }

    if emtitle:
        data["emtitle_1"] = emtitle
    if button:
        data["button_1"] = button
    if failover_sms:
        data["failover"] = "Y"
        if failover_subject:
            data["fsubject_1"] = failover_subject
        if failover_message:
            data["fmessage_1"] = failover_message

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_KAKAO_BASE}/akv10/alimtalk/send/", data=data)
            result = resp.json()
            logger.info("aligo 알림톡 발송: receiver=%s code=%s", receiver, result.get("code"))
            return result
    except Exception as e:
        logger.error("aligo 알림톡 발송 실패: %s", e)
        return {"code": -999, "message": str(e)}


async def get_remain() -> Dict[str, Any]:
    """잔여 포인트/건수 조회."""
    if not is_available():
        return {"result_code": -1, "message": "알리고 미설정"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_SMS_BASE}/remain/", data=_auth_params())
            return resp.json()
    except Exception as e:
        logger.error("aligo 잔여건수 조회 실패: %s", e)
        return {"result_code": -999, "message": str(e)}


async def get_send_list(
    page: int = 1,
    page_size: int = 30,
    start_date: Optional[str] = None,
    limit_day: Optional[int] = None,
) -> Dict[str, Any]:
    """발송 내역 조회.

    Args:
        page: 페이지 번호
        page_size: 페이지 크기
        start_date: 조회 시작일 (YYYYMMDD)
        limit_day: 조회 기간 (일수)
    """
    if not is_available():
        return {"result_code": -1, "message": "알리고 미설정"}

    data = {
        **_auth_params(),
        "page": str(page),
        "page_size": str(page_size),
    }
    if start_date:
        data["start_date"] = start_date
    if limit_day is not None:
        data["limit_day"] = str(limit_day)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_SMS_BASE}/list/", data=data)
            return resp.json()
    except Exception as e:
        logger.error("aligo 발송내역 조회 실패: %s", e)
        return {"result_code": -999, "message": str(e)}


async def cancel_reservation(mid: str) -> Dict[str, Any]:
    """예약 발송 취소.

    Args:
        mid: 발송 메시지 ID
    """
    if not is_available():
        return {"result_code": -1, "message": "알리고 미설정"}

    data = {**_auth_params(), "mid": mid}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_SMS_BASE}/cancel/", data=data)
            return resp.json()
    except Exception as e:
        logger.error("aligo 예약취소 실패: %s", e)
        return {"result_code": -999, "message": str(e)}
