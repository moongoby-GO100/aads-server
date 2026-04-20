from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.db_pool import close_pool, init_pool
from app.core.llm_key_provider import store_api_key

ROOT_DIR = Path(__file__).resolve().parents[1]

KEY_SPECS = {
    "ANTHROPIC_AUTH_TOKEN": {"provider": "anthropic", "priority": 1, "label": "moong76@gmail"},
    "ANTHROPIC_AUTH_TOKEN_2": {"provider": "anthropic", "priority": 2, "label": "moongoby@gmail"},
    "GEMINI_API_KEY": {"provider": "gemini", "priority": 1, "label": "newtalk"},
    "GEMINI_API_KEY_2": {"provider": "gemini", "priority": 2, "label": "aads"},
    "DEEPSEEK_API_KEY": {"provider": "deepseek", "priority": 1, "label": ""},
    "GROQ_API_KEY": {"provider": "groq", "priority": 1, "label": ""},
    "ALIBABA_API_KEY": {"provider": "alibaba", "priority": 1, "label": ""},
    "KIMI_API_KEY": {"provider": "kimi", "priority": 1, "label": ""},
    "MINIMAX_API_KEY": {"provider": "minimax", "priority": 1, "label": ""},
    "OPENAI_API_KEY": {"provider": "openai", "priority": 1, "label": ""},
    "LITELLM_MASTER_KEY": {"provider": "litellm", "priority": 1, "label": ""},
}


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


async def _seed() -> None:
    merged_env: dict[str, str] = {}
    for env_path in (ROOT_DIR / ".env", ROOT_DIR / ".env.litellm"):
        merged_env.update(_parse_env_file(env_path))

    await init_pool()
    seeded = 0
    skipped = 0
    try:
        for key_name, spec in KEY_SPECS.items():
            value = merged_env.get(key_name, "").strip()
            if not value:
                skipped += 1
                continue
            await store_api_key(
                key_name=key_name,
                plaintext_value=value,
                provider=spec["provider"],
                label=spec["label"],
                priority=spec["priority"],
            )
            seeded += 1
    finally:
        await close_pool()

    print(f"seeded={seeded} skipped={skipped}")


if __name__ == "__main__":
    asyncio.run(_seed())
