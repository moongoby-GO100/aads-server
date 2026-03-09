"""
AADS-186C: Langfuse Observability 설정
- Langfuse Python SDK 초기화 (langfuse>=3.0.0)
- LiteLLM 콜백 설정 (선택적 의존성 — 미설정 시 graceful 비활성화)
- 트레이스 메타데이터 자동 태깅
"""
from __future__ import annotations

import os

import structlog
from typing import Any, Dict, Optional

logger = structlog.get_logger(__name__)

# Langfuse 활성화 여부 (환경변수 미설정 시 False)
_langfuse_enabled: bool = False
_langfuse_client: Any = None


def _is_configured() -> bool:
    """필수 환경변수 존재 여부 확인."""
    return bool(
        os.getenv("LANGFUSE_SECRET_KEY")
        and os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_HOST")
    )


def init_langfuse() -> bool:
    """
    Langfuse SDK 초기화.
    환경변수 미설정 시 조용히 비활성화 (에러 아님).
    Returns: True=활성화, False=비활성화
    """
    global _langfuse_enabled, _langfuse_client

    if not _is_configured():
        logger.info("langfuse_disabled: LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY 미설정")
        return False

    try:
        from langfuse import Langfuse  # type: ignore[import]

        _langfuse_client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST", "http://localhost:3001"),
        )
        _langfuse_enabled = True
        logger.info(
            "langfuse_initialized",
            host=os.getenv("LANGFUSE_HOST"),
        )

        # LiteLLM 콜백 설정 (litellm 설치 시)
        try:
            import litellm  # type: ignore[import]
            if "langfuse" not in (litellm.success_callback or []):
                litellm.success_callback = ["langfuse"]
            if "langfuse" not in (litellm.failure_callback or []):
                litellm.failure_callback = ["langfuse"]
            logger.info("litellm_langfuse_callbacks_set")
        except ImportError:
            logger.debug("litellm_not_installed: langfuse callbacks skipped")

        return True

    except Exception as e:
        logger.warning("langfuse_init_failed_graceful_degradation", error=str(e))
        _langfuse_enabled = False
        return False


def get_langfuse() -> Optional[Any]:
    """Langfuse 클라이언트 반환 (비활성화 시 None)."""
    return _langfuse_client if _langfuse_enabled else None


def is_enabled() -> bool:
    """Langfuse 활성화 여부."""
    return _langfuse_enabled


def create_trace(
    name: str,
    session_id: Optional[str] = None,
    user_id: str = "CEO",
    metadata: Optional[Dict[str, Any]] = None,
    input_data: Optional[Any] = None,
) -> Optional[Any]:
    """
    Langfuse 트레이스 생성.
    비활성화 시 None 반환 (호출자는 None 체크 필요).
    """
    if not _langfuse_enabled or _langfuse_client is None:
        return None

    try:
        trace = _langfuse_client.trace(
            name=name,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata or {},
            input=input_data,
        )
        return trace
    except Exception as e:
        logger.warning("langfuse_create_trace_failed", error=str(e))
        return None


def flush_langfuse() -> None:
    """서버 종료 시 버퍼 플러시."""
    if _langfuse_enabled and _langfuse_client is not None:
        try:
            _langfuse_client.flush()
            logger.info("langfuse_flushed")
        except Exception as e:
            logger.warning("langfuse_flush_failed", error=str(e))
