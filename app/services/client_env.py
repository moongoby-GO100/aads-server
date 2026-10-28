"""메시지별 접속 환경(client_env) 정규화·저장 직렬화·AI 컨텍스트 문구.

원문 User-Agent·IP·기기 모델명은 다루지 않는다(데이터 최소 수집).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.models.chat import ClientEnv, coerce_client_env

CLIENT_ENV_UNKNOWN_LINE = "[접속 환경] 미확인(구 클라이언트 또는 수집 실패)"


def normalize_client_env(value: Any) -> Optional[Dict[str, Any]]:
    """dict/ClientEnv/JSON 문자열 → 저장용 dict. 해석 불가면 None."""
    if isinstance(value, (str, bytes)):
        if not value:
            return None
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = ClientEnv(env_uncertain=True)
    env = coerce_client_env(value)
    return env.model_dump() if env is not None else None


def client_env_json(value: Any) -> Optional[str]:
    normalized = normalize_client_env(value)
    return json.dumps(normalized, ensure_ascii=False) if normalized is not None else None


def _has_signal(env: Dict[str, Any]) -> bool:
    return bool(
        env.get("device_class") not in (None, "unknown")
        or env.get("viewport_w")
        or env.get("viewport_h")
        or env.get("orientation")
        or env.get("os_family")
        or env.get("browser_family")
    )


def format_client_env_context(value: Any) -> str:
    """해당 메시지의 환경 한 줄. 없거나 신호가 없으면 '미확인' 문구(PC 로 단정 금지)."""
    env = normalize_client_env(value)
    if env is None or not _has_signal(env):
        return CLIENT_ENV_UNKNOWN_LINE
    w, h = env.get("viewport_w"), env.get("viewport_h")
    details = []
    if w and h:
        details.append(f"viewport {w}x{h}")
    if env.get("orientation"):
        details.append(env["orientation"])
    detail = f"({', '.join(details)})" if details else ""
    source = env.get("source") or "unknown"
    uncertain = "true" if env.get("env_uncertain") else "false"
    line = f"[접속 환경] device={env.get('device_class') or 'unknown'}{detail}, source={source}, 불확실={uncertain}"
    if env.get("user_override"):
        line += ", 사용자 지정=true"
    return line


def client_env_prompt_block(value: Any) -> str:
    """system_prompt 에 덧붙일 블록. 이전 메시지의 환경은 재사용하지 않는다."""
    return (
        "\n\n"
        f"{format_client_env_context(value)}\n"
        "- 이 환경은 이번 메시지에만 해당한다. PC와 모바일을 오가며 쓸 수 있으므로 "
        "이전 메시지의 환경을 이번 메시지에 적용하지 않는다."
    )


def interrupt_env_lines(interrupts: Any) -> str:
    """추가 지시 큐 항목들의 환경 줄. 항목마다 자기 환경만 쓰고 env 가 없는 항목은 건너뛴다."""
    lines = []
    for item in interrupts or []:
        env = item.get("client_env") if isinstance(item, dict) else None
        if env:
            lines.append(format_client_env_context(env))
    return "\n".join(dict.fromkeys(lines))
