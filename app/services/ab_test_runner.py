from __future__ import annotations
import asyncio, hashlib, uuid, time, logging, os
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

AB_TEST_MODELS = {
    "P1": ["claude-haiku", "qwen3.6-plus", "qwen3-coder-flash", "deepseek-v3.2"],
    "P2": ["claude-sonnet", "qwen3-coder-next", "qwen3.6-plus", "deepseek-v3.2"],
    "P3": ["claude-opus", "claude-sonnet", "qwen3-coder-next", "qwen3-coder-plus"],
}

LITELLM_BASE = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")


async def call_model(model: str, prompt: str, max_tokens: int = 4096) -> dict:
    """LiteLLM 프록시 경유로 모델 호출. Claude/Qwen/DeepSeek 모두 동일 경로."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{LITELLM_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
            )
            data = resp.json()
            latency = int((time.monotonic() - start) * 1000)
            return {
                "response": data.get("choices", [{}])[0].get("message", {}).get("content", ""),
                "latency_ms": latency,
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
                "error": None
            }
    except Exception as e:
        return {"response": "", "latency_ms": int((time.monotonic() - start) * 1000),
                "input_tokens": 0, "output_tokens": 0, "error": str(e)}


async def run_ab_comparison(prompt: str, models: list[str], test_tier: str = "P1",
                            difficulty: int = None, test_type: str = "coding_bench") -> list[dict]:
    """동일 프롬프트를 여러 모델에 동시 전송하고 결과를 DB에 저장."""
    batch_id = str(uuid.uuid4())
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    # asyncio.gather로 동시 실행
    tasks = [call_model(m, prompt) for m in models]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    records = []
    for model, result in zip(models, results):
        if isinstance(result, Exception):
            result = {"response": "", "latency_ms": 0, "input_tokens": 0, "output_tokens": 0, "error": str(result)}
        record = {
            "batch_id": batch_id, "test_tier": test_tier, "test_type": test_type,
            "difficulty_level": difficulty, "prompt_text": prompt, "prompt_hash": prompt_hash,
            "model_name": model, **result
        }
        records.append(record)

    # DB INSERT
    try:
        import asyncpg
        conn = await asyncpg.connect(os.environ.get("DATABASE_URL", ""))
        for r in records:
            await conn.execute("""
                INSERT INTO ab_test_log (batch_id, test_tier, test_type, difficulty_level,
                     prompt_text, prompt_hash, model_name, response_text, latency_ms,
                     input_tokens, output_tokens, error_message)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """, r["batch_id"], r["test_tier"], r["test_type"], r.get("difficulty_level"),
                r["prompt_text"], r["prompt_hash"], r["model_name"], r.get("response", ""),
                r.get("latency_ms", 0), r.get("input_tokens", 0), r.get("output_tokens", 0), r.get("error"))
        await conn.close()
    except Exception as e:
        logger.error(f"AB test DB insert failed: {e}")

    return records


async def blind_judge(batch_id: str, judge_model: str = "qwen3-coder-next") -> list[dict]:
    """모델명을 가린 상태로 judge 모델이 채점."""
    import asyncpg
    conn = await asyncpg.connect(os.environ.get("DATABASE_URL", ""))
    rows = await conn.fetch(
        "SELECT id, model_name, prompt_text, response_text FROM ab_test_log WHERE batch_id=$1",
        batch_id
    )
    if not rows:
        await conn.close()
        return []

    prompt_text = rows[0]["prompt_text"]
    judge_prompt = (
        "다음 프롬프트에 대한 여러 응답을 평가하세요. "
        "평가 기준: 정확성(40%), 완성도(30%), 효율성(20%), 가독성(10%) "
        "각 응답에 1~10점을 매기고 이유를 설명하세요. "
        'JSON 배열로 반환: [{"response_id": "A", "score": 8.5, "reason": "..."}]\n\n'
        f"프롬프트: {prompt_text}\n\n"
    )
    for i, row in enumerate(rows):
        label = chr(65 + i)  # A, B, C, ...
        judge_prompt += f"--- 응답 {label} ---\n{row['response_text'][:2000]}\n\n"

    result = await call_model(judge_model, judge_prompt)

    # 점수 파싱 및 DB 업데이트
    import json, re
    try:
        scores_text = re.search(r'\[.*\]', result["response"], re.DOTALL)
        if scores_text:
            scores = json.loads(scores_text.group())
            for i, score_data in enumerate(scores):
                if i < len(rows):
                    await conn.execute(
                        "UPDATE ab_test_log SET judge_score=$1, judge_reason=$2, judge_model=$3 WHERE id=$4",
                        float(score_data.get("score", 0)), score_data.get("reason", ""), judge_model, rows[i]["id"]
                    )
    except Exception as e:
        logger.error(f"Judge parse error: {e}")

    await conn.close()
    return [{"model": r["model_name"], "score": None} for r in rows]
