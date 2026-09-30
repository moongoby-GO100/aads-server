"""계약서 서명요청/서명완료 알림 발송 (오비서/열정국밥).

채널은 사내 알림센터(inapp) → SMS → 알림톡 순서로 본다. 외부 발송(SMS·알림톡)은
``OBYS_CONTRACT_NOTIFY_CHANNELS`` 로 opt-in 해야만 나간다. 기본값은 ``inapp`` 이라
기본 설정에서는 실제 직원에게 문자가 가지 않는다.

이 모듈은 발송만 하고 저장은 하지 않는다. 발송 결과(채널별 sent/failed/skipped)를
돌려주면 호출자(yeoljeong_finance_service)가 이력 테이블에 남긴다.
어떤 채널이 실패해도 예외를 밖으로 던지지 않는다 — 알림 실패가 서명 요청을
실패시키면 안 된다.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

CHANNELS_ENV = "OBYS_CONTRACT_NOTIFY_CHANNELS"
DEFAULT_CHANNELS = ("inapp",)
KNOWN_CHANNELS = ("inapp", "sms", "alimtalk")
SIGN_URL_ENV = "OBYS_CONTRACT_SIGN_BASE_URL"
ALIMTALK_TEMPLATE_ENV = {
    "signature_requested": "OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGN_REQUEST",
    "signed": "OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGNED",
}
NOTIFICATION_TYPES = {
    "signature_requested": "contract_signature_requested",
    "signed": "contract_signed",
}


def enabled_channels() -> set[str]:
    raw = str(os.getenv(CHANNELS_ENV) or "").strip().lower()
    parts = {part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()}
    selected = {part for part in parts if part in KNOWN_CHANNELS}
    unknown = parts - selected
    if unknown:
        logger.warning("%s 에 알 수 없는 채널 무시: %s", CHANNELS_ENV, ",".join(sorted(unknown)))
    return selected or set(DEFAULT_CHANNELS)


def mask_email(email: str) -> str:
    email = str(email or "").strip().lower()
    if "@" not in email:
        return "***" if email else ""
    local, domain = email.split("@", 1)
    return f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"


def mask_phone(phone: str) -> str:
    """수신번호 원문은 저장하지 않는다. 7자리 미만이면 전부 가린다."""
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}-****-{digits[-4:]}"


def _phone_digits(contract: dict[str, Any]) -> str:
    return re.sub(r"\D+", "", str(contract.get("employee_phone") or ""))


def sign_url(contract: dict[str, Any]) -> str:
    base = str(os.getenv(SIGN_URL_ENV) or "").strip()
    token = str(contract.get("sign_token") or "")
    if not base or not token:
        return ""
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}yf_contract_token={token}"


def compose_message(contract: dict[str, Any], event: str) -> tuple[str, str]:
    """(제목, 본문). 외부 채널 본문에는 급여 등 계약 조건을 넣지 않는다."""
    name = str(contract.get("employee_name") or "").strip() or "직원"
    title = str(contract.get("print_title") or "계약서").strip()
    workplace = str(contract.get("branch") or contract.get("employer_name") or "").strip()
    where = f"[{workplace}] " if workplace else ""
    if event == "signed":
        return (
            f"{title} 서명 완료",
            f"{where}{name}님, {title} 전자서명이 완료되었습니다. "
            "오비서 앱의 계약서 메뉴에서 서명본 PDF 를 내려받아 보관하십시오.",
        )
    link = sign_url(contract)
    body = f"{where}{name}님, {title} 서명 요청이 도착했습니다. 오비서 앱에 본인 계정으로 로그인해 내용을 확인하고 서명해 주십시오."
    if link:
        body += f"\n{link}"
    return f"{title} 서명 요청", body


def _result(channel: str, status: str, target_masked: str, error_detail: str = "") -> dict[str, Any]:
    return {
        "channel": channel,
        "status": status,
        "target_masked": target_masked,
        "error_detail": str(error_detail or "")[:500],
    }


async def dispatch(contract: dict[str, Any], event: str) -> list[dict[str, Any]]:
    """채널별로 발송을 시도하고 결과 목록을 돌려준다. 예외를 던지지 않는다."""
    if event not in NOTIFICATION_TYPES:
        raise ValueError(f"unknown contract notify event: {event}")
    channels = enabled_channels()
    title, body = compose_message(contract, event)
    email = str(contract.get("employee_email") or "").strip().lower()
    phone = _phone_digits(contract)
    results: list[dict[str, Any]] = []

    # 1) 사내 알림센터 — 기존 알림센터 INSERT 경로를 그대로 쓴다.
    if "inapp" not in channels:
        results.append(_result("inapp", "skipped", mask_email(email), "channel_disabled"))
    elif not email:
        results.append(_result("inapp", "skipped", "", "no_employee_email"))
    else:
        try:
            from app.services import yeoljeong_ops_service

            await yeoljeong_ops_service.create_notification(
                business_id=str(contract.get("business_id") or ""),
                target_user=email,
                notification_type=NOTIFICATION_TYPES[event],
                title=title,
                body=compose_message({**contract, "sign_token": ""}, event)[1],
                reference_type="contract",
                reference_id=str(contract.get("id") or ""),
            )
            results.append(_result("inapp", "sent", mask_email(email)))
        except Exception as exc:  # noqa: BLE001 — 알림 실패는 기록만 한다
            logger.warning("contract notify inapp failed: contract=%s err=%s", contract.get("id"), type(exc).__name__)
            results.append(_result("inapp", "failed", mask_email(email), f"{type(exc).__name__}: {exc}"))

    # 외부 채널이 꺼져 있으면 알리고 클라이언트를 아예 건드리지 않는다.
    aligo = None
    if channels & {"sms", "alimtalk"}:
        try:
            from app.services import aligo_client as aligo
        except Exception as exc:  # noqa: BLE001
            logger.warning("aligo client import failed: %s", exc)

    # 2) SMS
    if "sms" not in channels:
        results.append(_result("sms", "skipped", mask_phone(phone), "channel_disabled"))
    elif aligo is None or not aligo.is_available():
        results.append(_result("sms", "skipped", mask_phone(phone), "aligo_unavailable"))
    elif not phone:
        results.append(_result("sms", "skipped", "", "no_employee_phone"))
    else:
        try:
            response = await aligo.send_sms(phone, body, title=title)
            if str(response.get("result_code")) == "1":
                results.append(_result("sms", "sent", mask_phone(phone)))
            else:
                results.append(
                    _result("sms", "failed", mask_phone(phone), f"result_code={response.get('result_code')} {response.get('message') or ''}")
                )
        except Exception as exc:  # noqa: BLE001
            results.append(_result("sms", "failed", mask_phone(phone), f"{type(exc).__name__}: {exc}"))

    # 3) 알림톡 — 사전심사된 템플릿 코드가 환경변수로 있을 때만
    template_code = str(os.getenv(ALIMTALK_TEMPLATE_ENV[event]) or "").strip()
    if "alimtalk" not in channels:
        results.append(_result("alimtalk", "skipped", mask_phone(phone), "channel_disabled"))
    elif not template_code:
        results.append(_result("alimtalk", "skipped", mask_phone(phone), f"template_not_configured ({ALIMTALK_TEMPLATE_ENV[event]})"))
    elif aligo is None or not aligo.is_available():
        results.append(_result("alimtalk", "skipped", mask_phone(phone), "aligo_unavailable"))
    elif not phone:
        results.append(_result("alimtalk", "skipped", "", "no_employee_phone"))
    else:
        try:
            response = await aligo.send_alimtalk(phone, template_code, body, subject=title, failover_sms=False)
            if str(response.get("code")) == "0":
                results.append(_result("alimtalk", "sent", mask_phone(phone)))
            else:
                results.append(
                    _result("alimtalk", "failed", mask_phone(phone), f"code={response.get('code')} {response.get('message') or ''}")
                )
        except Exception as exc:  # noqa: BLE001
            results.append(_result("alimtalk", "failed", mask_phone(phone), f"{type(exc).__name__}: {exc}"))
    return results


def summarize(results: list[dict[str, Any]]) -> str:
    """응답용 요약: 하나라도 sent 면 sent, 전부 skipped 면 skipped, 아니면 failed."""
    statuses = {str(item.get("status")) for item in results}
    if "sent" in statuses:
        return "sent"
    if statuses <= {"skipped"}:
        return "skipped"
    return "failed"
