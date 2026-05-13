"""로컬 OCR 브릿지 — CEO PC Agent ocr_extract 경유"""
import httpx

PC_AGENT_URL = "http://127.0.0.1:8102/api/v1/pc-agent"


async def _get_agent_id() -> str | None:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{PC_AGENT_URL}/agents")
        agents = r.json().get("agents", [])
        return agents[0]["agent_id"] if agents else None


async def ocr_extract(
    image_url: str | None = None,
    image_base64: str | None = None,
    language: str = "kor+eng",
) -> dict:
    agent_id = await _get_agent_id()
    if not agent_id:
        raise RuntimeError("PC Agent 연결 없음 — OCR 불가")

    params: dict = {"language": language}
    if image_url:
        params["image_url"] = image_url
    elif image_base64:
        params["image_base64"] = image_base64
    else:
        raise ValueError("image_url 또는 image_base64 필요")

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{PC_AGENT_URL}/route-execute",
            json={
                "agent_id": agent_id,
                "command_type": "ocr_extract",
                "params": params,
            },
        )
        r.raise_for_status()
        body = r.json()

    result = body.get("result", {}).get("result", {})
    return {
        "text": result.get("text", ""),
        "confidence": result.get("confidence", 0.0),
        "language": result.get("language", language),
        "error": result.get("error"),
    }
