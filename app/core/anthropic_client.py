"""
중앙 Anthropic 클라이언트 팩토리 + LiteLLM 폴백.

OAuth 토큰으로 Anthropic API 직접 호출.
Claude OAuth 계정 실패 시 Gemini/LiteLLM 체인으로 자동 폴백.
비Claude 모델(qwen-turbo 등)은 DashScope API 직접 또는 LiteLLM 프록시로 라우팅.
백그라운드 시스템(self_evaluator, fact_extractor, compaction 등)에서 사용.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Optional

import anthropic
import httpx
from anthropic import AsyncAnthropic

from app.core.auth_provider import (
    create_anthropic_client,
    get_litellm_config,
    get_oauth_tokens_async,
    mark_token_rate_limited,
)

logger = logging.getLogger(__name__)

# AADS(2026-09-06): Gemini/DeepSeek 폴백 제거 — CLI 모델(Claude)로 대체 (CEO 지시)
_DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_DASHSCOPE_API_KEY = os.getenv("ALIBABA_API_KEY", "")
# DashScope 계정이 차단돼 모든 호출이 실패한다(2026-09-12 실측):
#   qwen3-235b → 404 "model does not exist or you do not have access"
#   qwen-turbo → 400 "Access denied, please make sure your account is in good standing"
# 키는 남아 있어서 `if _DASHSCOPE_API_KEY:` 가드를 통과하고, 폴백을 탈 때마다
# 헛된 왕복과 경고 로그를 만든다. 계정이 복구되면 이 값을 1 로 되돌린다.
_DASHSCOPE_ENABLED = os.getenv("AADS_DASHSCOPE_ENABLED", "0") == "1"


class DashScopeDisabled(RuntimeError):
    """DashScope 경로가 꺼져 있음 — 호출자는 다음 폴백으로 넘어간다."""
# AADS-LLM-AUTH: DB(llm_fallback_chains) 우선, 환경변수 폴백
_BG_FALLBACK_MODELS_ENV = [
    m.strip()
    for m in os.getenv(
        "LLM_BG_FALLBACK_MODELS", "groq-llama-70b,groq-gpt-oss-120b,qwen-flash"
    ).split(",")
    if m.strip()
]
# AADS-204(2026-09-18): 배경 LLM 1순위 = Groq 무료 모델(input/output $0).
# 종전 1순위 qwen-turbo(DashScope)는 계정 연체로 400 Access denied — 30일 8,255회
# 전량 실패하고 claude-haiku 폴백으로 OAuth 정액 한도를 잠식했다.
# 되돌릴 때는 LLM_BG_PRIMARY_MODEL=qwen-turbo 로 바꾸고 reload-api.sh 만 실행한다.
_BG_PRIMARY_MODEL = os.getenv("LLM_BG_PRIMARY_MODEL", "groq-gpt-oss-120b")
_CLAUDE_RETRY_BASE_SEC = 2.0
_CLAUDE_RETRY_MAX_DELAY_SEC = 30.0
_CLAUDE_RETRY_JITTER_SEC = 1.5
_CLAUDE_MAX_RETRIES = 60
_CLAUDE_RETRY_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_CLAUDE_429_MAX_RETRIES = 30


def _retry_delay(attempt: int, status_code: int | None = None) -> float:
    """429: 고정 3초 (CEO 지시). 그 외: exponential backoff with jitter."""
    if status_code == 429:
        return 3.0
    base = _CLAUDE_RETRY_BASE_SEC
    delay = min(base * (2 ** min(attempt, 6)), _CLAUDE_RETRY_MAX_DELAY_SEC)
    return delay + random.uniform(0, _CLAUDE_RETRY_JITTER_SEC)

_bg_primary_fail_streak: int = 0  # 배경 LLM 1순위 연속 실패 카운터 (AADS-204)


# ── LiteLLM 응답 래퍼 (Anthropic Message 호환) ──────────────────────

class _LiteLLMTextBlock:
    """Anthropic TextBlock 호환."""
    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _LiteLLMUsage:
    """Anthropic Usage 호환."""
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _LiteLLMResponse:
    """LiteLLM/DashScope 응답을 Anthropic Message 형태로 래핑."""
    def __init__(self, text: str, model: str, usage_data: Optional[dict] = None):
        self.content = [_LiteLLMTextBlock(text)]
        self.model = model
        self.usage = _LiteLLMUsage(
            input_tokens=(usage_data or {}).get("prompt_tokens", 0),
            output_tokens=(usage_data or {}).get("completion_tokens", 0),
        )
        self.stop_reason = "end_turn"


# ── BYOK(Bring Your Own Key) 사용자별 API 키 조회 ─────────────────────
# AADS-BYOK(2026-09-02): 사용자가 등록한 본인 API 키를 시스템 키보다 우선 사용.

async def _get_user_api_key(user_id: str, provider: str) -> Optional[str]:
    """user_api_keys 테이블에서 활성 키를 조회, 복호화하여 반환. 실패 시 None."""
    try:
        from app.core.credential_vault import decrypt_value
        from app.core.db_pool import get_pool

        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT id, encrypted_key
            FROM user_api_keys
            WHERE user_id = $1 AND provider = $2 AND is_active = TRUE
            """,
            str(user_id),
            provider,
        )
        if not row:
            return None
        plain = decrypt_value(row["encrypted_key"])
        # 조회 성공 시점에 last_used_at 갱신 (베스트 에포트, 실패해도 무시)
        try:
            await pool.execute(
                "UPDATE user_api_keys SET last_used_at = NOW() WHERE id = $1",
                row["id"],
            )
        except Exception:
            pass
        return plain
    except Exception as e:
        logger.debug("get_user_api_key_failed: user_id=%s provider=%s error=%s", str(user_id)[:12], provider, str(e)[:80])
        return None


def _user_content(prompt: str, images: Optional[list] = None):
    """Anthropic user 메시지 content. images 가 없으면 기존처럼 문자열 그대로."""
    if not images:
        return prompt
    return [{"type": "text", "text": prompt}] + list(images)


def _normalize_images(images: Optional[list]) -> Optional[list]:
    """images 목록에 섞인 파일 경로를 vision block 으로 바꾼다 (AADS-VISION-UNIFY).

    러너·서브에이전트는 스크린샷을 만들어 두고 디스크 경로만 넘긴다. 경로를
    그대로 messages 에 실으면 Anthropic 이 400 을 내므로, document_context 의
    변환 파이프라인(포맷 변환·5MB 축소·PDF document block·중복 제거)을 그대로 태운다.
    이미 만들어진 block(dict)만 들어오면 원본을 그대로 돌려주므로 기존 호출은 동작이 같다.
    """
    if not images:
        return images
    if all(isinstance(b, dict) for b in images):
        return images

    blocks: list = []
    paths: list = []
    for item in images:
        if isinstance(item, dict):
            blocks.append(item)
        elif isinstance(item, (str, os.PathLike)):
            paths.append(os.fspath(item))
        else:
            logger.warning("vision_block_ignored: type=%s", type(item).__name__)

    if paths:
        try:
            from app.core.document_context import build_vision_blocks

            converted = build_vision_blocks([], extra_paths=paths)
            logger.info("vision_paths_converted: paths=%d blocks=%d", len(paths), len(converted))
            blocks.extend(converted)
        except Exception as e:
            logger.warning("vision_path_convert_failed: %s", str(e)[:120])
    return blocks


def _to_openai_image_content(prompt: str, images: list) -> list:
    """Anthropic image/document block → OpenAI 호환 content 배열 (LiteLLM/Gemini 폴백용).

    LiteLLM 프록시는 base64 를 data URL 로 받는다. PDF document block 은
    OpenAI 호환 스키마에 대응물이 없으므로 한 줄 안내 텍스트로 낮춘다.
    """
    parts: list = [{"type": "text", "text": prompt}]
    for block in images or []:
        if not isinstance(block, dict):
            continue
        source = block.get("source") or {}
        if block.get("type") == "image" and source.get("type") == "base64":
            media_type = source.get("media_type", "image/jpeg")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{source.get('data', '')}"},
            })
        elif block.get("type") == "document":
            parts.append({"type": "text", "text": "[첨부 PDF — 이 경로에서는 전달되지 않음]"})
    return parts


async def _try_user_key_claude(
    prompt: str,
    model: str,
    max_tokens: int,
    system: Optional[str],
    user_id: str,
    images: Optional[list] = None,
) -> Optional[str]:
    """BYOK: 사용자 본인 Anthropic API 키로 직접 호출 시도.

    사용자 키가 없거나 호출이 실패하면 None을 반환하여 기존 시스템 키
    폴백 체인(OAuth → Gemini → qwen → LiteLLM)으로 이어지게 한다.
    """
    user_key = await _get_user_api_key(user_id, "anthropic")
    if not user_key:
        return None
    try:
        # OAuth 토큰(sk-ant-oat*)은 x-api-key 가 아니라 Authorization: Bearer 로
        # 보내야 한다. AsyncAnthropic(api_key=...) 로 넘기면 401 이 난다.
        # 2026-09-14: 진아(244) Claude Code OAuth 토큰을 BYOK 로 등록하면서 발견.
        # 판별/분기는 auth_provider.create_anthropic_client 하나로 모은다.
        from app.core.auth_provider import create_anthropic_client
        client = create_anthropic_client(user_key)
        msgs = [{"role": "user", "content": _user_content(prompt, images)}]
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        if system:
            kwargs["system"] = system
        resp = await client.messages.create(**kwargs)
        logger.info("byok_user_key_used: user_id=%s model=%s", str(user_id)[:12], model)
        return resp.content[0].text
    except Exception as e:
        logger.warning("byok_user_key_failed: user_id=%s model=%s error=%s", str(user_id)[:12], model, str(e)[:120])
        return None


# ── 공개 함수 ────────────────────────────────────────────────────────

def get_client(model_hint: str = "claude-haiku") -> AsyncAnthropic:
    """Anthropic API 직접 클라이언트 반환 (auth_provider 경유)."""
    return create_anthropic_client()


def _extract_status_code(exc: Exception) -> Optional[int]:
    """SDK/httpx 예외에서 HTTP 상태 코드를 최대한 보수적으로 추출."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status

    err_str = str(exc)
    for code in sorted(_CLAUDE_RETRY_STATUS_CODES):
        if str(code) in err_str:
            return code
    return None


def _is_timeout_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            asyncio.TimeoutError,
            httpx.ReadTimeout,
            httpx.TimeoutException,
            anthropic.APITimeoutError,
        ),
    ) or "timeout" in str(exc).lower()


def _is_retryable_error(exc: Exception) -> tuple[bool, Optional[int]]:
    status_code = _extract_status_code(exc)
    if _is_timeout_error(exc):
        return True, status_code
    return status_code in _CLAUDE_RETRY_STATUS_CODES, status_code


def _get_error_headers(exc: Exception) -> Optional[dict]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return dict(headers) if headers else None


async def call_llm_with_fallback(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    system: Optional[str] = None,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    images: Optional[list] = None,
) -> Optional[str]:
    """Claude 호출 + 실패 시 qwen/LiteLLM 폴백. 백그라운드 평가/추출용.

    비Claude 모델(qwen-turbo 등) 지정 시 DashScope/LiteLLM으로 직접 라우팅.

    user_id가 주어지면(BYOK, AADS-BYOK) user_api_keys 테이블에서 해당 사용자의
    Anthropic 키를 조회해 시스템 키보다 우선 사용한다. 사용자 키가 없거나
    호출이 실패하면 기존 시스템 키 폴백 체인(0순위 아래)을 그대로 유지한다.

    0순위: 사용자 본인 API 키 (BYOK, user_id 제공 시)
    1순위: Claude moong76@gmail (slot:naver, PRIMARY)
    2순위: Claude moongoby@gmail (slot:gmail, FALLBACK)
    3순위: Gemini (LiteLLM 프록시)

    images 가 주어지면(Anthropic image/document block 목록) Claude 경로는 그대로
    전달하고, LiteLLM/DashScope 폴백 경로는 OpenAI 호환 image_url 로 변환한다.
    러너가 넘기는 디스크 경로(str/Path)가 섞여 있으면 build_vision_blocks 로
    먼저 block 으로 변환한다. 기본값 None 이면 기존 동작과 완전히 동일하다.

    Returns: 응답 텍스트 또는 None (전부 실패 시)
    """
    images = _normalize_images(images)

    if tenant_id:
        from app.services.tenant_usage_limits import check_tenant_usage_limit

        await check_tenant_usage_limit(tenant_id, operation=f"llm:{model}", projected_calls=1)

    if user_id and model.startswith("claude"):
        _user_text = await _try_user_key_claude(prompt, model, max_tokens, system, user_id, images)
        if _user_text is not None:
            return _user_text

    # 비Claude 모델 → DashScope/LiteLLM 직접
    if not model.startswith("claude"):
        try:
            if model.startswith("qwen"):
                return await _call_dashscope(prompt, model, max_tokens, system, images)
            return await _call_litellm(prompt, model, max_tokens, system, images)
        except Exception as e:
            logger.warning("litellm_bg_error: model=%s error=%s", model, str(e)[:80])
            from app.core.llm_fallback_engine import get_bg_fallback_models
            _fb_models = await get_bg_fallback_models()
            for _fb_model in (_fb_models or _BG_FALLBACK_MODELS_ENV):
                if _fb_model == model:
                    continue
                try:
                    _text = await _call_litellm(prompt, _fb_model, max_tokens, system, images)
                    if _text:
                        logger.info("bg_llm_last_resort_ok: model=%s", _fb_model)
                        return _text
                except Exception as e3:
                    logger.warning(
                        "bg_llm_last_resort_error: model=%s error=%s", _fb_model, str(e3)[:80]
                    )
            return None

    from app.services.oauth_usage_tracker import log_usage

    # DB is the source of truth for operator-managed cooldowns.  The previous
    # synchronous cache could still contain a token that /health/api-keys had
    # already reported as rate-limited, causing every background call to spend
    # up to 90 seconds retrying that token before trying the healthy slot.
    keys_to_try = await get_oauth_tokens_async()
    for key in keys_to_try:
        last_error: Optional[Exception] = None
        _429_count = 0
        for retry_count in range(_CLAUDE_MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                client = create_anthropic_client(token=key)
                msgs = [{"role": "user", "content": _user_content(prompt, images)}]
                kwargs = {"model": model, "max_tokens": max_tokens, "messages": msgs}
                if system:
                    kwargs["system"] = system
                raw = await client.messages.with_raw_response.create(**kwargs)
                resp = raw.parse()
                duration_ms = int((time.monotonic() - t0) * 1000)
                log_usage(
                    token=key,
                    model=model,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                    headers=raw.headers,
                    call_source="anthropic_client",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                return resp.content[0].text
            except (httpx.ReadTimeout, anthropic.APITimeoutError) as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                log_usage(
                    token=key,
                    model=model,
                    call_source="anthropic_client",
                    error_code="timeout",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code=None)
                    logger.warning(
                        "claude_bg_retry_timeout: key=%s retry_count=%d/%d wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_bg_timeout_exhausted: key=%s model=%s retry_count=%d last_error=%s",
                    key[:12],
                    model,
                    retry_count,
                    str(e)[:160],
                )
                break
            except Exception as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                retryable, status_code = _is_retryable_error(e)
                error_code = str(status_code) if status_code else "error"
                log_usage(
                    token=key,
                    model=model,
                    call_source="anthropic_client",
                    error_code=error_code,
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if status_code == 429:
                    _429_count += 1
                    if _429_count <= _CLAUDE_429_MAX_RETRIES:
                        wait = _retry_delay(_429_count - 1, 429)
                        logger.warning(
                            "claude_429_retry: key=%s model=%s attempt=%d/%d wait=%.1fs",
                            key[:12], model, _429_count, _CLAUDE_429_MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    mark_token_rate_limited(key, _get_error_headers(e))
                    logger.warning(
                        "claude_429_limit_exhausted: key=%s model=%s after=%d retries → next token",
                        key[:12], model, _CLAUDE_429_MAX_RETRIES,
                    )
                    break
                if retryable and retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code)
                    logger.warning(
                        "claude_bg_retry: key=%s retry_count=%d/%d status=%s wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        status_code or "timeout",
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_bg_error: key=%s model=%s retry_count=%d status=%s last_error=%s",
                    key[:12],
                    model,
                    retry_count,
                    status_code or "n/a",
                    str(e)[:160],
                )
                break
        if last_error is not None:
            logger.warning(
                "claude_bg_token_failed: key=%s model=%s max_retries=%d last_error=%s",
                key[:12],
                model,
                _CLAUDE_MAX_RETRIES,
                str(last_error)[:160],
            )

    _lc = get_litellm_config()

    # 3순위: 외부 모델은 반드시 LiteLLM 프록시를 통한다.
    if _lc.get("key"):
        try:
            _gemini_text = await _call_litellm(
                prompt, "gemini-2.5-flash-lite", max_tokens, system, images
            )
            if _gemini_text:
                return _gemini_text
        except Exception as e:
            logger.warning("gemini_litellm_fallback_error: %s", str(e)[:80])

    # 최종: 운영 DB의 LiteLLM 저비용 체인 — Claude OAuth/Gemini 실패 시 가용성 유지
    if _lc.get("key"):
        from app.core.llm_fallback_engine import get_bg_fallback_models as _get_fb
        _fb_list = await _get_fb()
        for _fb_model in (_fb_list or _BG_FALLBACK_MODELS_ENV):
            try:
                _text = await _call_litellm(prompt, _fb_model, max_tokens, system, images)
                if _text:
                    logger.info("bg_llm_last_resort_ok: model=%s", _fb_model)
                    return _text
            except Exception as e:
                logger.warning(
                    "bg_llm_last_resort_error: model=%s error=%s", _fb_model, str(e)[:80]
                )

    logger.error("all_bg_llm_failed: claude+gemini_litellm_chain exhausted")
    return None


async def call_background_llm(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
    tenant_id: Optional[str] = None,
) -> str:
    """배경 서비스용 LLM 호출 — groq-gpt-oss-120b(무료) 1순위, claude-haiku 폴백.

    compaction, memory_manager, fact_extractor, experience_learner,
    quality_feedback_loop, self_evaluator, smart_search, code_reviewer 등
    OAuth 한도를 소비하지 않는 배경 작업에서 사용.
    1순위 모델은 LLM_BG_PRIMARY_MODEL 로 바꿀 수 있다(_BG_PRIMARY_MODEL).
    """
    global _bg_primary_fail_streak
    if tenant_id:
        from app.services.tenant_usage_limits import check_tenant_usage_limit

        await check_tenant_usage_limit(tenant_id, operation="background_llm", projected_calls=1)
    t0 = time.time()

    # 1순위: _BG_PRIMARY_MODEL (기본 groq-gpt-oss-120b, LiteLLM 경유 / 무료)
    # qwen* 로 되돌린 경우에만 DashScope 직접 경로를 탄다.
    _primary = _BG_PRIMARY_MODEL
    try:
        if _primary.startswith("qwen"):
            result = await _call_dashscope(prompt, _primary, max_tokens, system or None)
        else:
            result = await _call_litellm(prompt, _primary, max_tokens, system or None)
        if result:
            _bg_primary_fail_streak = 0
            await _bg_llm_log(
                "background", _primary, True,
                latency_ms=int((time.time() - t0) * 1000),
                tenant_id=tenant_id,
            )
            return result
    except Exception as e:
        logger.warning("call_background_llm_primary_failed: model=%s error=%s", _primary, str(e)[:80])
        _bg_primary_fail_streak += 1
        await _bg_llm_log(
            "background", _primary, False,
            error_code="bg_primary_failed", tenant_id=tenant_id,
        )
        if _bg_primary_fail_streak >= 3:  # 조기 감지를 위해 3회 (AADS-204)
            await _notify_bg_llm_alert(_bg_primary_fail_streak, _primary)

    # 2순위: claude-haiku (OAuth 폴백)
    fallback = await call_llm_with_fallback(
        prompt=prompt,
        system=system or None,
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        tenant_id=tenant_id,
    )
    return fallback or ""


async def _bg_llm_log(
    service_name: str,
    model: str,
    success: bool,
    latency_ms: int = 0,
    error_code: Optional[str] = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tenant_id: Optional[str] = None,
) -> None:
    """bg_llm_usage_log 테이블에 호출 결과 INSERT. DB 실패 시 예외 무시."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bg_llm_usage_log
                  (service_name, model, success, input_tokens, output_tokens, latency_ms, error_code, tenant_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8::uuid, public.aads_internal_tenant_id()))
                """,
                service_name, model, success,
                input_tokens, output_tokens, latency_ms, error_code, tenant_id,
            )
    except Exception as e:
        logger.debug("bg_llm_log_failed: %s", str(e)[:80])


async def _notify_bg_llm_alert(streak: int, model: str = "") -> None:
    """배경 LLM 1순위 연속 실패 시 텔레그램 긴급알림."""
    try:
        from app.services.telegram_bot import get_telegram_bot
        bot = get_telegram_bot()
        _model = model or _BG_PRIMARY_MODEL
        if bot and bot.is_ready:
            await bot.send_message(
                f"\U0001f6a8 *배경 LLM 1순위 연속 실패 ({streak}회)*\n"
                f"model={_model} 이 {streak}회 연속 실패했습니다.\n"
                f"claude-haiku 폴백 중 — 프로바이더 상태 확인 필요. (AADS-204)"
            )
    except Exception as e:
        logger.debug("bg_llm_alert_failed: %s", str(e)[:80])


async def call_llm_messages_with_fallback(**kwargs) -> object:
    """Anthropic Messages API 직접 호출 + 2계정 폴백 (서브에이전트/tool-use용).

    비Claude 모델(qwen-turbo 등) 지정 시 DashScope/LiteLLM으로 직접 라우팅,
    Anthropic Message 호환 객체로 래핑하여 반환.

    Args:
        **kwargs: AsyncAnthropic.messages.create()에 전달할 전체 파라미터

    Returns:
        Anthropic Message 응답 객체 또는 _LiteLLMResponse (비Claude 경유 시)

    Raises:
        Exception: 모든 키에서 실패 시 마지막 예외를 raise
    """
    tenant_id = kwargs.pop("tenant_id", None)
    _model = kwargs.get("model", "unknown")
    if tenant_id:
        from app.services.tenant_usage_limits import check_tenant_usage_limit

        await check_tenant_usage_limit(tenant_id, operation=f"llm_messages:{_model}", projected_calls=1)

    # 비Claude 모델 → DashScope/LiteLLM 직접
    if not _model.startswith("claude"):
        # 이 자리에는 try/except 가 없다. DashScope 가 꺼져 있으면 예외를 올리는
        # 대신 LiteLLM 으로 흘려보내야 폴백이 성립한다.
        if _model.startswith("qwen") and _DASHSCOPE_ENABLED:
            return await _call_dashscope_messages(
                model=_model,
                messages=kwargs.get("messages", []),
                max_tokens=kwargs.get("max_tokens", 256),
                system=kwargs.get("system"),
            )
        return await _call_litellm_messages(
            model=_model,
            messages=kwargs.get("messages", []),
            max_tokens=kwargs.get("max_tokens", 256),
            system=kwargs.get("system"),
        )

    from app.services.oauth_usage_tracker import log_usage

    # Refresh the DB-backed availability set for every logical model call so
    # persisted cooldowns survive process restarts and health probes cannot
    # accidentally re-enable a cooled-down token in the runtime cache.
    keys_to_try = await get_oauth_tokens_async()
    last_error: Optional[Exception] = None

    for key in keys_to_try:
        _429_count = 0
        for retry_count in range(_CLAUDE_MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                client = create_anthropic_client(token=key)
                raw = await client.messages.with_raw_response.create(**kwargs)
                resp = raw.parse()
                duration_ms = int((time.monotonic() - t0) * 1000)
                log_usage(
                    token=key,
                    model=_model,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                    headers=raw.headers,
                    call_source="anthropic_client_msg",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                return resp
            except (httpx.ReadTimeout, anthropic.APITimeoutError) as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                log_usage(
                    token=key,
                    model=_model,
                    call_source="anthropic_client_msg",
                    error_code="timeout",
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code=None)
                    logger.warning(
                        "claude_msg_retry_timeout: key=%s retry_count=%d/%d wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_msg_timeout_exhausted: key=%s model=%s retry_count=%d last_error=%s",
                    key[:12],
                    _model,
                    retry_count,
                    str(e)[:160],
                )
                break
            except Exception as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_error = e
                retryable, status_code = _is_retryable_error(e)
                error_code = str(status_code) if status_code else "error"
                log_usage(
                    token=key,
                    model=_model,
                    call_source="anthropic_client_msg",
                    error_code=error_code,
                    duration_ms=duration_ms,
                    tenant_id=tenant_id,
                )
                if status_code == 429:
                    _429_count += 1
                    if _429_count <= _CLAUDE_429_MAX_RETRIES:
                        wait = _retry_delay(_429_count - 1, 429)
                        logger.warning(
                            "claude_msg_429_retry: key=%s attempt=%d/%d wait=%.1fs",
                            key[:12], _429_count, _CLAUDE_429_MAX_RETRIES, wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    mark_token_rate_limited(key, _get_error_headers(e))
                    logger.warning(
                        "claude_msg_429_exhausted: key=%s after=%d retries → next token",
                        key[:12], _CLAUDE_429_MAX_RETRIES,
                    )
                    break
                if retryable and retry_count < _CLAUDE_MAX_RETRIES:
                    wait = _retry_delay(retry_count, status_code)
                    logger.warning(
                        "claude_msg_retry: key=%s retry_count=%d/%d status=%s wait=%.1fs last_error=%s",
                        key[:12],
                        retry_count + 1,
                        _CLAUDE_MAX_RETRIES,
                        status_code or "timeout",
                        wait,
                        str(e)[:160],
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.warning(
                    "claude_msg_error: key=%s model=%s retry_count=%d status=%s last_error=%s",
                    key[:12],
                    _model,
                    retry_count,
                    status_code or "n/a",
                    str(e)[:160],
                )
                break

    raise last_error or RuntimeError("no API keys configured")


# ── DashScope 직접 호출 (Alibaba Qwen 모델) ─────────────────────────

_FALLBACK_QUICK_RETRIES = 3
_FALLBACK_QUICK_DELAYS = (1.0, 2.0, 4.0)
_FALLBACK_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


def _is_fallback_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and status in _FALLBACK_RETRYABLE_STATUS:
        return True
    return isinstance(exc, (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException))


async def _call_dashscope(
    prompt: str,
    model: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
    images: Optional[list] = None,
) -> str:
    """DashScope API 직접 호출 (OpenAI 호환). 일시 오류 시 3회 빠른 재시도."""
    if not _DASHSCOPE_ENABLED:
        raise DashScopeDisabled("dashscope_disabled")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": _to_openai_image_content(prompt, images) if images else prompt,
    })

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max(max_tokens, 512),
    }

    last_err: Optional[Exception] = None
    for attempt in range(_FALLBACK_QUICK_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{_DASHSCOPE_BASE_URL}/chat/completions",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {_DASHSCOPE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content:
                    raise ValueError(f"DashScope returned empty content for model {model}")
                logger.info("dashscope_bg_ok: model=%s tokens=%s", model, data.get("usage", {}))
                return content
        except Exception as e:
            last_err = e
            if attempt < _FALLBACK_QUICK_RETRIES and _is_fallback_retryable(e):
                wait = _FALLBACK_QUICK_DELAYS[attempt]
                logger.warning("dashscope_quick_retry: model=%s attempt=%d wait=%.1fs err=%s", model, attempt + 1, wait, str(e)[:80])
                await asyncio.sleep(wait)
                continue
            raise
    raise last_err  # unreachable but satisfies type checker


async def _call_dashscope_messages(
    model: str,
    messages: list,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> _LiteLLMResponse:
    """DashScope API 직접 Messages 호출 — Anthropic Response 호환 래핑."""
    if not _DASHSCOPE_ENABLED:
        raise DashScopeDisabled("dashscope_disabled")
    oai_msgs = []
    if system:
        oai_msgs.append({"role": "system", "content": system})
    for m in messages:
        oai_msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    body = {
        "model": model,
        "messages": oai_msgs,
        "max_tokens": max(max_tokens, 512),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_DASHSCOPE_BASE_URL}/chat/completions",
            json=body,
            headers={
                "Authorization": f"Bearer {_DASHSCOPE_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        if not content:
            raise ValueError(f"DashScope returned empty content for model {model}")
        usage_data = data.get("usage", {})
        logger.info("dashscope_msg_ok: model=%s tokens=%s", model, usage_data)
        return _LiteLLMResponse(content, model, usage_data)


# ── LiteLLM 프록시 호출 (Gemini 등) ─────────────────────────────────

async def _call_litellm(
    prompt: str,
    model: str,
    max_tokens: int = 256,
    system: Optional[str] = None,
    images: Optional[list] = None,
) -> str:
    """LiteLLM 프록시 경유 텍스트 생성 (OpenAI 호환 API). 일시 오류 시 3회 빠른 재시도."""
    _lc = get_litellm_config()
    url = f"{_lc['url']}/v1/chat/completions"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": _to_openai_image_content(prompt, images) if images else prompt,
    })

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max(max_tokens, 512),
    }

    last_err: Optional[Exception] = None
    for attempt in range(_FALLBACK_QUICK_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {_lc['key']}"},
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content:
                    raise ValueError(f"LiteLLM returned empty content for model {model}")
                return content
        except Exception as e:
            last_err = e
            if attempt < _FALLBACK_QUICK_RETRIES and _is_fallback_retryable(e):
                wait = _FALLBACK_QUICK_DELAYS[attempt]
                logger.warning("litellm_quick_retry: model=%s attempt=%d wait=%.1fs err=%s", model, attempt + 1, wait, str(e)[:80])
                await asyncio.sleep(wait)
                continue
            raise
    raise last_err


async def _call_litellm_messages(
    model: str,
    messages: list,
    max_tokens: int = 256,
    system: Optional[str] = None,
) -> _LiteLLMResponse:
    """LiteLLM 프록시 경유 Messages 호출 — Anthropic Response 호환 래핑."""
    _lc = get_litellm_config()
    url = f"{_lc['url']}/v1/chat/completions"

    oai_msgs = []
    if system:
        oai_msgs.append({"role": "system", "content": system})
    for m in messages:
        oai_msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    body = {
        "model": model,
        "messages": oai_msgs,
        "max_tokens": max(max_tokens, 512),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {_lc['key']}"},
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        if not content:
            raise ValueError(f"LiteLLM returned empty content for model {model}")
        usage_data = data.get("usage", {})
        return _LiteLLMResponse(content, model, usage_data)
