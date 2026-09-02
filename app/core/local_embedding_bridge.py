"""로컬 임베딩 브릿지 — 서버 Ollama 우선, CEO PC Agent 폴백"""
import logging
from typing import Union

import httpx

logger = logging.getLogger(__name__)

SERVER_OLLAMA_URL = "http://127.0.0.1:11434"
PC_AGENT_URL = "http://127.0.0.1:8102/api/v1/pc-agent"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


async def _server_ollama_embed(texts: list[str], model: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{SERVER_OLLAMA_URL}/api/embed",
                json={"model": model, "input": texts},
            )
            r.raise_for_status()
            body = r.json()
        embeddings = body.get("embeddings", [])
        if embeddings:
            return {
                "embeddings": embeddings,
                "model": model,
                "dimensions": len(embeddings[0]),
                "count": len(embeddings),
                "source": "server_ollama",
            }
    except Exception as exc:
        logger.warning("server_ollama_embed_failed model=%s error=%s", model, str(exc)[:120])
    return None


async def _pc_agent_embed(texts: list[str], model: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{PC_AGENT_URL}/agents")
            agents = r.json().get("agents", [])
            if not agents:
                return None
            agent_id = agents[0]["agent_id"]

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{PC_AGENT_URL}/route-execute",
                json={
                    "agent_id": agent_id,
                    "command_type": "ollama_embed",
                    "params": {"model": model, "input": texts},
                },
            )
            r.raise_for_status()
            body = r.json()

        result = body.get("result", {}).get("result", {})
        embeddings = result.get("embeddings", [])
        if embeddings:
            return {
                "embeddings": embeddings,
                "model": model,
                "dimensions": len(embeddings[0]),
                "count": len(embeddings),
                "source": "pc_agent_ollama",
            }
    except Exception as exc:
        logger.warning("pc_agent_embed_failed model=%s error=%s", model, str(exc)[:120])
    return None


async def embed(
    input: Union[str, list[str]],
    model: str = DEFAULT_EMBED_MODEL,
) -> dict:
    texts = [input] if isinstance(input, str) else input

    result = await _server_ollama_embed(texts, model)
    if result:
        return result

    result = await _pc_agent_embed(texts, model)
    if result:
        return result

    raise RuntimeError(f"임베딩 실패 — 서버 Ollama·PC Agent 모두 불가 (model={model})")
