"""Register Antigravity models in llm_models table."""
import asyncio
import asyncpg
import os

async def main():
    db_url = os.getenv("DATABASE_URL", "postgresql://aads:aads2026secure@aads-postgres:5432/aads")
    c = await asyncpg.connect(db_url)
    models = [
        ("antigravity", "Antigravity (Gemini 3.5 Flash)", "antigravity", True, "coding", "GEMINI_API_KEY"),
        ("antigravity-pro", "Antigravity Pro (Gemini 3.1 Pro)", "antigravity", True, "reasoning", "GEMINI_API_KEY"),
        ("antigravity-flash", "Antigravity Flash (Gemini 3.5 Flash)", "antigravity", True, "general", "GEMINI_API_KEY"),
    ]
    for mid, dname, prov, active, cat, key in models:
        exists = await c.fetchval("SELECT id FROM llm_models WHERE model_id = $1", mid)
        if exists:
            await c.execute(
                "UPDATE llm_models SET display_name=$2, provider=$3, is_active=$4, category=$5, linked_key_name=$6 WHERE model_id=$1",
                mid, dname, prov, active, cat, key,
            )
            print(f"Updated: {mid} (id={exists})")
        else:
            new_id = await c.fetchval(
                "INSERT INTO llm_models (model_id, display_name, provider, is_active, category, linked_key_name, supports_tools, supports_thinking, supports_vision, supports_coding) VALUES ($1,$2,$3,$4,$5,$6,true,true,false,true) RETURNING id",
                mid, dname, prov, active, cat, key,
            )
            print(f"Inserted: {mid} (id={new_id})")
    rows = await c.fetch("SELECT id, model_id, display_name, is_active FROM llm_models WHERE provider = 'antigravity' ORDER BY id")
    for r in rows:
        print(f"  -> id={r['id']} model_id={r['model_id']} display_name={r['display_name']} active={r['is_active']}")
    await c.close()

asyncio.run(main())
