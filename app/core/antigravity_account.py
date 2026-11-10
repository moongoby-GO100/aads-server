"""Host OAuth account controls. Absence preserves the pre-registration default."""
from app.core.db_pool import get_pool


async def antigravity_enabled() -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT is_active, manual_disabled FROM llm_api_keys WHERE provider='antigravity' ORDER BY id LIMIT 1")
    return row is None or bool(row["is_active"] and not row["manual_disabled"])
