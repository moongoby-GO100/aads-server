"""공식 LangSmith external export 게이트 — 기본값 OFF.

정책 (PRD FR-010, 지시서 5항): 아래가 **모두** 참일 때만 외부 전송이 열린다.

1. `LANGSMITH_TRACING`이 truthy
2. `LANGSMITH_ENDPOINT`가 https:// 로 시작
3. `LANGSMITH_API_KEY`가 존재
4. 전송 페이로드가 마스킹 정책을 통과 (시크릿 잔재가 없음)

하나라도 빠지면 `enabled=False`이고 어떤 원문도 밖으로 나가지 않는다. 이 모듈은
키를 로그/응답에 절대 싣지 않고, 존재 여부만 보고한다.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from app.services.llmops_store import mask_secrets

TRACING_ENV = "LANGSMITH_TRACING"
ENDPOINT_ENV = "LANGSMITH_ENDPOINT"
API_KEY_ENV = "LANGSMITH_API_KEY"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})

# 마스킹 후에도 남아 있으면 전송을 막는 잔재 패턴
_RESIDUAL_SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_\-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,})"
)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def is_tracing_enabled() -> bool:
    return _env(TRACING_ENV).lower() in _TRUTHY


def masking_passes(payload: Any) -> tuple[bool, Optional[str]]:
    """마스킹 후 시크릿 잔재가 없는지 검사한다. (통과여부, 사유)."""
    text = mask_secrets(str(payload))
    match = _RESIDUAL_SECRET.search(text)
    if match:
        # 사유에도 시크릿을 싣지 않는다 — 패턴 종류만 보고한다.
        return False, f"residual secret pattern ({match.group(0)[:6]}…) after masking"
    return True, None


def export_status() -> dict[str, Any]:
    """외부 export 게이트 상태. 키 값은 절대 포함하지 않는다."""
    endpoint = _env(ENDPOINT_ENV)
    checks = {
        "tracing_flag": is_tracing_enabled(),
        "endpoint_https": endpoint.startswith("https://"),
        "api_key_present": bool(_env(API_KEY_ENV)),
    }
    blockers = [name for name, ok in checks.items() if not ok]
    return {
        "enabled": not blockers,
        "default": "disabled",
        "checks": checks,
        "blockers": blockers,
        # endpoint는 호스트만 노출한다 (경로에 토큰이 실릴 수 있음).
        "endpoint_host": endpoint.split("/")[2] if endpoint.startswith("https://") and len(endpoint.split("/")) > 2 else None,
        "note": "External LangSmith export stays off until env gate and masking policy both pass.",
    }


def prepare_export(trace: dict[str, Any]) -> dict[str, Any]:
    """trace 1건의 외부 전송 가능 여부를 판정하고 마스킹된 페이로드를 만든다.

    실제 HTTP 전송은 하지 않는다. 게이트가 닫혀 있으면 payload 자체를 만들지
    않으므로, 승인 전에는 원문이 메모리 밖으로도 나가지 않는다.
    """
    status = export_status()
    if not status["enabled"]:
        return {
            "exported": False,
            "reason": "export_disabled",
            "blockers": status["blockers"],
        }

    payload = {
        "id": trace.get("id"),
        "name": trace.get("run_type") or "chain",
        "run_type": trace.get("run_type") or "chain",
        "project": trace.get("project"),
        "status": trace.get("status"),
        "inputs": {"summary": mask_secrets(str(trace.get("input_summary") or ""))},
        "outputs": {"summary": mask_secrets(str(trace.get("output_summary") or ""))},
        "error": mask_secrets(str(trace.get("error"))) if trace.get("error") else None,
        "extra": {
            "graph_run_id": trace.get("graph_run_id"),
            "latency_ms": trace.get("latency_ms"),
            "cost_usd": trace.get("cost_usd"),
            "error_class": trace.get("error_class"),
        },
    }

    ok, reason = masking_passes(payload)
    if not ok:
        return {"exported": False, "reason": "masking_policy_failed", "detail": reason}

    return {"exported": False, "reason": "ready_not_sent", "payload": payload}
