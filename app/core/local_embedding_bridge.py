"""로컬 임베딩 브릿지 — 서버 Ollama 우선, CEO PC Agent 폴백

2026-09-14. 임베딩이 **전부 가짜였다.** Ollama 주소가 `127.0.0.1` 로 박혀
있었는데, 앱은 컨테이너 안에서 돈다. 컨테이너의 127.0.0.1 은 자기 자신이라
호스트의 Ollama 에 닿지 않는다. 실측:

    컨테이너 → 127.0.0.1:11434            연결 실패
    컨테이너 → host.docker.internal:11434  200  (nomic-embed-text 있음)

두 경로가 다 실패하면 `chat_embedding_service` 가 조용히 더미 벡터를
돌려준다(로그는 debug 라 안 보인다). 그 더미가 `chat_messages.embedding`,
`memory_facts`, `doc_chunks` 에 진짜인 것처럼 저장돼 왔다.

증상은 "검색이 되긴 하는데 결과가 이상하다" 였다. 더미도 해시 기반이라
값이 골고루 흩어져서 유사도 숫자는 그럴듯하게 나온다 — 의미만 없다.
문서 7,847청크를 색인하고 "채팅 응답이 왜 느려졌지" 로 검색하니 온보딩
가이드와 은행 검증 문서가 0.97 로 나왔다.

호스트/컨테이너 주소 불일치는 이 서버에서 반복되는 실패 유형이다
(오류 사전 `runner.litellm_instr_path_mismatch` 참고).
"""
import logging
import os
from typing import Union

import httpx

logger = logging.getLogger(__name__)


def _default_ollama_url() -> str:
    """컨테이너 안이면 호스트 게이트웨이를 쓴다.

    `/.dockerenv` 존재로 컨테이너를 판별한다. 환경변수가 있으면 그게 우선이다.
    """
    override = os.getenv("LOCAL_OLLAMA_URL", "").strip()
    if override:
        return override.rstrip("/")
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:11434"
    return "http://127.0.0.1:11434"


SERVER_OLLAMA_URL = _default_ollama_url()
PC_AGENT_URL = os.getenv("PC_AGENT_BASE_URL", "http://127.0.0.1:8102/api/v1/pc-agent")
DEFAULT_EMBED_MODEL = "nomic-embed-text"


# 서버 Ollama 는 CPU 로 돈다(이 서버에 GPU 없음, 8코어). 2026-09-14 실측:
# 1,400자 한 건에 약 6초, 20건을 한 요청에 넣으면 30초를 넘겨 타임아웃이
# 났다. 그 타임아웃이 "경로 없음"으로 떨어져 더미 벡터가 저장됐다.
# 배치를 줄이고 대기를 늘린다.
_OLLAMA_TIMEOUT_SEC = float(os.getenv("LOCAL_OLLAMA_TIMEOUT_SEC", "300"))


async def _server_ollama_embed(texts: list[str], model: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT_SEC) as client:
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
        # 타임아웃은 메시지가 비어 있다. 예외 타입을 같이 남겨야 "연결 실패"와
        # "너무 느림"을 구분할 수 있다 — 2026-09-14 에 이걸 구분 못 해서
        # 주소 문제를 고친 뒤에도 원인을 다시 찾아야 했다.
        logger.warning(
            "server_ollama_embed_failed model=%s url=%s batch=%d type=%s error=%s",
            model, SERVER_OLLAMA_URL, len(texts), type(exc).__name__, str(exc)[:120],
        )
    return None


async def _pc_agent_embed(texts: list[str], model: str) -> dict | None:
    """CEO PC 의 Ollama 로 임베딩 — 2026-09-14 현재 동작하지 않는다.

    이유가 둘이고 둘 다 PC 쪽이다.

    1. 에이전트가 `ollama_embed` 명령을 구현하지 않았다. 지원 명령 93개에
       `ollama_chat`/`ollama_list`/`ollama_pull` 은 있는데 embed 는 없다.
       호출하면 "지원하지 않는 명령" 이 돌아온다.
    2. PC 에 모델이 하나도 없다(`ollama_list` 결과가 빈 목록). 저장공간 때문에
       지웠다고 확인됐다. `nomic-embed-text` 는 0.27GB 로 작으니 다시 받으면
       되지만, 1번이 먼저다.

    고칠 때는 이 주석을 지우고 실제 동작을 확인한 날짜를 남겨라.
    """
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
