"""로그에서 1회용 서명본 PDF 링크 토큰을 가린다. 경로 자체가 인증이라 원문이 로그에 남으면 안 된다.

uvicorn 접근 로그(stdlib 문자열)와 structlog 이벤트(request.url.path 를 필드로 싣는 예외 핸들러 등)
두 경로를 모두 막는다.
"""
from __future__ import annotations

import logging
import re
from typing import Any

SIGNED_PDF_LINK_PREFIX = "/api/v1/yeoljeong-finance/contracts/signed-pdf/t/"
_TOKEN_IN_TEXT = re.compile(re.escape(SIGNED_PDF_LINK_PREFIX) + r"[^\s?#\"']*")
_REDACTED = SIGNED_PDF_LINK_PREFIX + "[redacted]"
_MAX_DEPTH = 6


def redact_signed_pdf_link(text: str) -> str:
    return _TOKEN_IN_TEXT.sub(_REDACTED, text)


def redact_log_value(value: Any, _depth: int = 0) -> Any:
    """문자열·dict·list·tuple 안의 토큰 경로를 가린다. 토큰이 없으면 같은 객체를 돌려준다."""
    if isinstance(value, str):
        return redact_signed_pdf_link(value) if SIGNED_PDF_LINK_PREFIX in value else value
    if _depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {key: redact_log_value(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_log_value(item, _depth + 1) for item in value)
    return value


def redact_signed_pdf_link_processor(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog 프로세서. 렌더러 앞에서 모든 필드를 훑는다."""
    return redact_log_value(event_dict)


class SignedPdfLinkRedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_value(record.msg)
        if record.args:
            record.args = redact_log_value(record.args)
        return True


def install_access_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        target = logging.getLogger(name)
        if not any(isinstance(f, SignedPdfLinkRedactFilter) for f in target.filters):
            target.addFilter(SignedPdfLinkRedactFilter())


def install_handler_redaction() -> None:
    """루트 핸들러에 필터를 단다. 로거 필터는 자식 로거 기록에는 적용되지 않으므로 핸들러에서 막는다."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SignedPdfLinkRedactFilter) for f in handler.filters):
            handler.addFilter(SignedPdfLinkRedactFilter())
