#!/usr/bin/env python3
"""One-shot model registry sync trigger."""
import asyncio
import sys
sys.path.insert(0, "/app")

async def main():
    from app.core.db_pool import init_pool
    await init_pool()
    from app.services.model_registry import sync_model_registry
    result = await sync_model_registry(triggered_by="manual_script", reason="gpt56_registration")
    print(f"sync done: models={result.get('total_models', '?')}, upserted={result.get('upserted_count', '?')}")

asyncio.run(main())
