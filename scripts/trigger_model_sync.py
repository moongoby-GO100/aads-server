#!/usr/bin/env python3
"""One-shot model registry sync trigger."""
import asyncio
import sys

sys.path.insert(0, "/app")

async def main():
    from app.core.db_pool import init_pool
    await init_pool()
    from app.api.llm_report import refresh_static_report
    from app.services.model_registry import sync_model_registry

    result = await sync_model_registry(triggered_by="manual_script", reason="manual_llm_model_refresh")
    report = await refresh_static_report(refresh=True)
    print(
        "sync done: "
        f"models={result.get('total_models', '?')}, "
        f"upserted={result.get('upserted_count', '?')}, "
        f"report_models={report.get('models', '?')}, "
        f"report_bytes={report.get('bytes', '?')}"
    )

asyncio.run(main())
