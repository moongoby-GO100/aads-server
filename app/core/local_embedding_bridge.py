"""로컬 임베딩 브릿지 — CEO PC Ollama qwen3-embedding / bge-m3 경유"""
import httpx
from typing import Union

PC_AGENT_URL = "http://127.0.0.1:8102/api/v1/pc-agent"
DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"


async def _get_agent_id() -> str | None:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{PC_AGENT_URL}/agents")
        data = r.json()
        agents = data.get("agents", [])
        return agents[0]["agent_id"] if agents else None


async def embed(
    input: Union[str, list[str]],
    model: str = DEFAULT_EMBED_MODEL,
) -> dict:
    agent_id = await _get_agent_id()
    if not agent_id:
        raise RuntimeError("PC Agent 연결 없음 — 임베딩 불가")

    texts = [input] if isinstance(input, str) else input
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
    return {
        "embeddings": embeddings,
        "model": model,
        "dimensions": len(embeddings[0]) if embeddings else 0,
        "count": len(embeddings),
    }
