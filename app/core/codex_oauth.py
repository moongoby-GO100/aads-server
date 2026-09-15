"""Codex(ChatGPT 구독 OAuth) 호출 어댑터.

Codex 는 API 키로 붙지 않는다. ChatGPT 구독 계정의 OAuth 다. 그래서
`llm_api_keys` 에 들어가는 값도 키 한 줄이 아니라 refresh_token·account_id·
엔드포인트를 담은 JSON 이고, 그 JSON 을 통째로 암호화해 `encrypted_value` 에
넣는다 (provider='codex', key_name='CODEX_OAUTH_JINAH').

access_token 은 짧게 만료된다. 그래서 refresh_token 으로 그때그때 발급받아
쓰고 **프로세스 메모리에만** 캐시한다. 디스크에 쓰지 않는다 — 진아서버의
`/root/.codex/auth.json` 이 정본이고 이쪽은 사본이다.

2026-09-15 실측으로 확인한 두 가지를 적어 둔다.

- ChatGPT 계정으로 붙는 Codex 는 **허용 모델이 제한된다.** `gpt-5.6-sol` 은
  200 이고 `gpt-5.1-codex` / `gpt-5.6-codex` / `codex-mini-latest` 는 전부
  `400 The '<model>' model is not supported when using Codex with a ChatGPT
  account` 를 돌려준다. 모델명을 바꿀 때 이걸 먼저 확인해라.
- 응답은 SSE 로만 온다. `stream: false` 를 줘도 스트림으로 돌아오므로
  본문 파싱은 `parse_sse_text()` 를 쓴다.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.core.credential_vault import decrypt_value
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

DEFAULT_KEY_NAME = "CODEX_OAUTH_JINAH"

# ChatGPT 계정 Codex 가 실제로 받아주는 모델. 위 주석의 실측 근거를 보라.
DEFAULT_MODEL = "gpt-5.6-sol"

# access_token 을 만료 직전까지 쓰지 않는다. 60초 여유를 둔다.
_TOKEN_REFRESH_MARGIN_SEC = 60

# {key_name: (access_token, expires_at_epoch)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


class CodexAuthError(RuntimeError):
    """Codex 자격증명이 없거나 갱신에 실패했다."""


class CodexCallError(RuntimeError):
    """Codex 엔드포인트가 2xx 가 아닌 응답을 돌려줬다."""


async def load_codex_config(key_name: str = DEFAULT_KEY_NAME) -> dict[str, Any]:
    """llm_api_keys 에서 Codex OAuth 묶음을 꺼내 복호화한다."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT encrypted_value FROM llm_api_keys "
            "WHERE key_name = $1 AND provider = 'codex' AND is_active",
            key_name,
        )
    if not row:
        raise CodexAuthError(
            f"Codex 자격증명이 없다 (key_name={key_name}). "
            "/settings 의 'LLM 키 및 모델 레지스트리' 에서 provider=codex 로 등록해라."
        )
    try:
        cfg = json.loads(decrypt_value(row["encrypted_value"]))
    except Exception as exc:  # 복호화 실패 / JSON 아님
        raise CodexAuthError(f"Codex 자격증명을 읽을 수 없다: {exc}") from exc

    for field in ("client_id", "account_id", "refresh_token", "token_endpoint", "responses_endpoint"):
        if not cfg.get(field):
            raise CodexAuthError(f"Codex 자격증명에 {field} 가 없다 (key_name={key_name})")
    return cfg


async def get_access_token(
    cfg: dict[str, Any],
    *,
    key_name: str = DEFAULT_KEY_NAME,
    force: bool = False,
) -> str:
    """refresh_token 으로 access_token 을 받아 온다. 프로세스 캐시를 먼저 본다."""
    cached = _TOKEN_CACHE.get(key_name)
    if cached and not force and cached[1] - _TOKEN_REFRESH_MARGIN_SEC > time.time():
        return cached[0]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            cfg["token_endpoint"],
            json={
                "client_id": cfg["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": cfg["refresh_token"],
                "scope": "openid profile email",
            },
        )
    if resp.status_code != 200:
        raise CodexAuthError(
            f"Codex 토큰 갱신 실패 HTTP {resp.status_code}: {resp.text[:200]}"
        )
    body = resp.json()
    token = body.get("access_token") or ""
    if not token:
        raise CodexAuthError("Codex 토큰 갱신 응답에 access_token 이 없다")

    expires_in = int(body.get("expires_in") or 3600)
    _TOKEN_CACHE[key_name] = (token, time.time() + expires_in)
    return token


def parse_sse_text(body: str) -> tuple[str, dict[str, Any]]:
    """Codex SSE 본문에서 출력 텍스트와 usage 를 뽑는다.

    순수 함수다 — 네트워크를 타지 않으므로 단위 테스트가 이 함수를 직접 부른다.
    """
    text = ""
    usage: dict[str, Any] = {}
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "response.output_text.delta":
            text += event.get("delta") or ""
        elif etype == "response.completed":
            usage = (event.get("response") or {}).get("usage") or {}
    return text.strip(), usage


async def call_codex(
    prompt: str,
    *,
    instructions: str = "",
    model: str | None = None,
    key_name: str = DEFAULT_KEY_NAME,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Codex 를 한 번 호출하고 {text, usage, model} 을 돌려준다.

    401 을 받으면 캐시된 access_token 이 낡은 것이므로 한 번만 강제 갱신해
    재시도한다. 그 이상은 재시도하지 않는다 — 구독 만료나 계정 문제를
    재시도로 덮으면 원인이 로그에서 사라진다.
    """
    cfg = await load_codex_config(key_name)
    chosen_model = model or cfg.get("model") or DEFAULT_MODEL
    payload = {
        "model": chosen_model,
        "instructions": instructions or "You are a helpful coding assistant.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "store": False,
        "stream": True,
    }

    async def _post(token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                cfg["responses_endpoint"],
                headers={
                    "Authorization": f"Bearer {token}",
                    "chatgpt-account-id": cfg["account_id"],
                    "OpenAI-Beta": "responses=experimental",
                    "originator": "codex_cli_rs",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

    resp = await _post(await get_access_token(cfg, key_name=key_name))
    if resp.status_code == 401:
        logger.info("codex 401 — access_token 강제 갱신 후 1회 재시도")
        resp = await _post(await get_access_token(cfg, key_name=key_name, force=True))

    if resp.status_code != 200:
        raise CodexCallError(
            f"Codex 호출 실패 HTTP {resp.status_code} (model={chosen_model}): {resp.text[:300]}"
        )

    text, usage = parse_sse_text(resp.text)
    return {"text": text, "usage": usage, "model": chosen_model}
