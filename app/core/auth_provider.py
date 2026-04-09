"""
AADS 인증 중앙 관리 — OAuth 토큰 로딩, 폴백, 클라이언트 생성.

모든 Claude LLM 호출은 이 모듈을 통해 토큰을 얻어야 함.
다른 파일에서 os.getenv("ANTHROPIC_API_KEY") 직접 사용 금지 (R-AUTH).

사용 예시:
    from app.core.auth_provider import get_oauth_tokens, get_base_url, create_anthropic_client

    tokens = get_oauth_tokens()           # [Primary, Fallback]
    client = create_anthropic_client()     # AsyncAnthropic(primary token)
    ok = has_valid_token()                 # True if any token exists
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# ── 토큰 로딩 (모듈 초기화 시 1회) ──────────────────────────────────
_TOKEN_PRIMARY = os.getenv("ANTHROPIC_AUTH_TOKEN", "") or os.getenv("ANTHROPIC_API_KEY", "")
_TOKEN_FALLBACK = os.getenv("ANTHROPIC_AUTH_TOKEN_2", "") or os.getenv("ANTHROPIC_API_KEY_FALLBACK", "")
_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
_LITELLM_URL = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
_LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# 라벨 매핑 (토큰 prefix 20자 기준)
_KEY_LABELS: Dict[str, str] = {}
if _TOKEN_PRIMARY:
    _KEY_LABELS[_TOKEN_PRIMARY[:20]] = "moong76@gmail"
if _TOKEN_FALLBACK:
    _KEY_LABELS[_TOKEN_FALLBACK[:20]] = "moongoby@gmail"

# 런타임 순서 변경 가능한 리스트
_ordered_tokens: List[str] = [k for k in [_TOKEN_PRIMARY, _TOKEN_FALLBACK] if k]


# ── 공개 API ────────────────────────────────────────────────────────

def get_oauth_tokens() -> List[str]:
    """[Primary, Fallback] 순서로 유효한 토큰 반환. 빈 토큰 제외."""
    return list(_ordered_tokens)


def get_primary_token() -> str:
    """1순위 토큰 반환. 없으면 빈 문자열."""
    return _ordered_tokens[0] if _ordered_tokens else ""


def get_fallback_token() -> str:
    """2순위 토큰 반환. 없으면 빈 문자열."""
    return _ordered_tokens[1] if len(_ordered_tokens) > 1 else ""


def get_base_url() -> str:
    """Anthropic API base URL."""
    return _BASE_URL


def get_litellm_config() -> Dict[str, str]:
    """LiteLLM 프록시 설정 반환."""
    return {"url": _LITELLM_URL, "key": _LITELLM_KEY}


def get_token_labels() -> List[Dict[str, str]]:
    """프론트 표시용 토큰 정보. [{'label': 'moong76@gmail', 'prefix': 'sk-ant-oat01-3BC...'}]"""
    result = []
    for token in _ordered_tokens:
        label = _KEY_LABELS.get(token[:20], "Unknown")
        result.append({"label": label, "prefix": token[:12] + "..."})
    return result


def set_token_order(primary: str) -> bool:
    """토큰 순서 변경. primary='naver' → TOKEN_PRIMARY(moong76@gmail)을 1순위로 설정
    primary='gmail' → TOKEN_FALLBACK(moongoby@gmail)을 1순위로 설정 (순환 스위치).

    Returns: True if changed, False if invalid.
    """
    global _ordered_tokens
    if primary.lower() == "naver" and _TOKEN_PRIMARY:
        _ordered_tokens = [k for k in [_TOKEN_PRIMARY, _TOKEN_FALLBACK] if k]
        logger.info("auth_provider: token order set to moong76@gmail-first (slot:naver)")
        return True
    elif primary.lower() == "gmail" and _TOKEN_FALLBACK:
        _ordered_tokens = [k for k in [_TOKEN_FALLBACK, _TOKEN_PRIMARY] if k]
        logger.info("auth_provider: token order set to moongoby@gmail-first (slot:gmail)")
        return True
    return False


def rotate_oauth_primary_fallback() -> bool:
    """한도·크레딧류 API 오류 시 1순위↔2순위 OAuth 토큰 순서 교환 (런타임)."""
    global _ordered_tokens
    if len(_ordered_tokens) < 2:
        return False
    _ordered_tokens = [_ordered_tokens[1], _ordered_tokens[0]]
    logger.warning("auth_provider: primary/fallback rotated (quota or limit-class error)")
    return True


# -- Rate Limit Cooldown Tracking --
_token_cooldowns = {}  # {token_prefix_20: expire_timestamp}

def _parse_rl_reset(headers=None):
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra:
        try: return time.time() + float(ra)
        except: pass
    rr = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if rr:
        try: return float(rr)
        except: pass
    return None

def mark_token_rate_limited(token, headers=None):
    global _ordered_tokens
    expire = _parse_rl_reset(headers) or (time.time() + 3600)
    _token_cooldowns[token[:20]] = expire
    logger.warning("token_rate_limited: prefix=%s until=%s", token[:12], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expire)))
    if token in _ordered_tokens and len(_ordered_tokens) > 1:
        _ordered_tokens = [t for t in _ordered_tokens if t != token] + [token]
        logger.warning("token_order_rotated: rate-limited token moved to end")
    return expire

def is_token_rate_limited(token):
    expire = _token_cooldowns.get(token[:20], 0)
    if time.time() >= expire:
        _token_cooldowns.pop(token[:20], None)
        return False
    return True

def get_available_tokens():
    available = [t for t in _ordered_tokens if not is_token_rate_limited(t)]
    return available if available else list(_ordered_tokens)



def create_anthropic_client(token: Optional[str] = None) -> AsyncAnthropic:
    """AsyncAnthropic 클라이언트 생성. token 미지정 시 primary 사용."""
    key = token or get_primary_token()
    return AsyncAnthropic(api_key=key, base_url=_BASE_URL)


def has_valid_token() -> bool:
    """유효한 OAuth 토큰이 하나 이상 있는지."""
    return bool(_ordered_tokens)
