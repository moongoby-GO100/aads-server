"""Recover dynamic model acceptance from CLI before dispatch after a restart."""
from __future__ import annotations


async def ensure_model(model, allowed_models, refresh, lock):
    """Serialize refresh; only the collector can extend the accepted model map."""
    if model in allowed_models:
        return
    async with lock:
        if model not in allowed_models:
            await refresh()
