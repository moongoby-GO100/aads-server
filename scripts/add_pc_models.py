"""PC 로컬 Ollama 모델 DB 등록 스크립트 (2026-05-13 fix)"""
import os
import json
import asyncio
import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads@aads-postgres:5432/aads"
)


def make_metadata(canonical_model: str, aliases: list, supports_vision: bool = False) -> str:
    model_id = "pc-" + canonical_model.replace(":", "-").replace(".", "")
    return json.dumps({
        "aliases": aliases,
        "discovered": False,
        "max_tokens": 2048,
        "model_source": "manual",
        "routing_note": "PC Agent executes the model on CEO PC Ollama.",
        "canonical_model": canonical_model,
        "timeout_seconds": 300,
        "execution_backend": "pc_ollama",
        "template_provider": "litellm",
        "execution_model_id": model_id,
        "runtime_executable": True,
        "supports_vision": supports_vision,
        "raw": {
            "model_name": model_id,
            "litellm_params": {
                "model": f"openai/{model_id}",
                "api_base": "http://aads-server:8080/api/v1/pc-ollama/v1",
                "use_litellm_proxy": False,
            },
        },
    })


# (model_id, display_name, canonical, aliases, supports_vision, category, family)
NEW_MODELS = [
    ("pc-qwen25-coder-7b",  "PC Qwen2.5-Coder 7B",      "qwen2.5-coder:7b", ["qwen2.5-coder:7b"],  False, "coding",   "qwen"),
    ("pc-qwen25-14b",       "PC Qwen2.5 14B General",    "qwen2.5:14b",      ["qwen2.5:14b"],        False, "general",  "qwen"),
    ("pc-exaone35-7b",      "PC EXAONE 3.5 7.8B Korean", "exaone3.5:7.8b",   ["exaone3.5:7.8b"],    False, "general",  "exaone"),
    ("pc-deepseek-r1-8b",   "PC DeepSeek-R1 8B Reason",  "deepseek-r1:8b",   ["deepseek-r1:8b"],    False, "reasoning","deepseek"),
    ("pc-gemma3-4b",        "PC Gemma3 4B Baseline",     "gemma3:4b",        ["gemma3:4b"],          False, "general",  "gemma"),
    # 임베딩 모델
    ("pc-qwen3-embed-0.6b", "PC Qwen3-Embedding 0.6B",   "qwen3-embedding:0.6b", ["qwen3-embedding:0.6b"], False, "embedding", "qwen"),
    ("pc-bge-m3",           "PC BGE-M3 Multilingual",    "bge-m3",           ["bge-m3"],             False, "embedding", "bge"),
]

ACTIVATE_IDS = [
    "pc-qwen3-1.7b",
    "pc-qwen3-0.6b",
]


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # 신규 모델 UPSERT
        inserted = 0
        for model_id, display_name, canonical, aliases, supports_vision, category, family in NEW_MODELS:
            meta = make_metadata(canonical, aliases, supports_vision)
            await conn.execute(
                """
                INSERT INTO llm_models
                    (provider, model_id, display_name, family, category,
                     supports_vision, is_active, activation_source, metadata)
                VALUES
                    ('litellm', $1, $2, $3, $4, $5, true, 'review_required', $6::jsonb)
                ON CONFLICT (provider, model_id) DO UPDATE
                    SET display_name      = EXCLUDED.display_name,
                        is_active         = true,
                        metadata          = EXCLUDED.metadata
                """,
                model_id, display_name, family, category, supports_vision, meta,
            )
            print(f"  [UPSERT] {model_id}")
            inserted += 1

        # 기존 소형 모델 활성화
        for mid in ACTIVATE_IDS:
            row = await conn.fetchrow(
                "SELECT id FROM llm_models WHERE model_id = $1", mid
            )
            if row:
                await conn.execute(
                    "UPDATE llm_models SET is_active = true WHERE model_id = $1", mid
                )
                print(f"  [ACTIVATE] {mid}")
            else:
                print(f"  [SKIP] {mid} not found")

        print(f"\nDone — upserted {inserted}, activated {len(ACTIVATE_IDS)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
