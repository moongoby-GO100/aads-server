"""244(진아) 서버의 LLM 계정을 AADS 중앙 키 레지스트리에 등록한다.

2026-09-15 대표님 지시: "진아서버에 llm 계정들이 많이 추가되었다고 한다.
확인하고 리스트 요금제등 정리해서 보고하고 aads에 모두 반영해줘"

## 왜 이렇게 넣나

1. **기존 키를 덮지 않는다.** 이미 AADS 에 있는 provider(gemini/groq/deepseek)
   는 `_JINAH` 접미 key_name 으로 **뒤 순위에 추가**한다. 같은 key_name 으로
   넣으면 UPSERT 가 기존 값을 지운다 — 돌아가던 경로가 조용히 바뀐다.
2. **지문으로 먼저 대조한다.** 이미 같은 값이 들어 있으면 건너뛴다.
   SHA-256 앞 12자리만 쓰고 값은 로그에 남기지 않는다.
3. **store_api_key 를 쓴다.** 직접 INSERT 하면 암호화·캐시 무효화·모델
   레지스트리 동기화를 빠뜨린다. 2026-09-14 에 그렇게 넣은 키가 캐시 때문에
   한동안 반영되지 않았다.

재실행해도 안전하다(멱등). `--apply` 없이 실행하면 계획만 출력한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys

sys.path.insert(0, "/app")

ENV_FILE = "/tmp/_jinah_llm.env"

# (env 키, AADS key_name, provider, priority, label)
PLAN: list[tuple[str, str, str, int, str]] = [
    # AADS 에 provider 자체가 없던 것 — 신규
    ("BISEO_KEY_OPENROUTER", "OPENROUTER_API_KEY", "openrouter", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_CEREBRAS", "CEREBRAS_API_KEY", "cerebras", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_TOGETHER", "TOGETHER_API_KEY", "together", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_MISTRAL", "MISTRAL_API_KEY", "mistral", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_NVIDIA", "NVIDIA_API_KEY", "nvidia", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_SAMBANOVA", "SAMBANOVA_API_KEY", "sambanova", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_HUGGINGFACE", "HUGGINGFACE_API_KEY", "huggingface", 1, "jinah-biseo(244)"),
    ("BISEO_KEY_TAVILY", "TAVILY_API_KEY", "tavily", 1, "jinah-biseo(244) 웹검색"),
    # 이미 있는 provider — 뒤 순위로 추가
    ("BISEO_KEY_GROQ", "GROQ_API_KEY_JINAH", "groq", 2, "jinah-biseo(244)"),
    ("BISEO_KEY_DEEPSEEK", "DEEPSEEK_API_KEY_JINAH", "deepseek", 2, "jinah-biseo(244) 잔액 $18.94"),
    ("BISEO_KEY_GEMINI", "GEMINI_API_KEY_JINAH", "gemini", 3, "jinah-biseo(244) #1"),
    ("BISEO_KEY_GEMINI2", "GEMINI_API_KEY_JINAH2", "gemini", 4, "jinah-biseo(244) #2"),
]

# 등록하지 않고 확인만 하는 것 — 이미 들어 있어야 정상.
VERIFY_ONLY = [("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN_3")]

# LLM 이 아니라 옮기지 않는 것. 왜 뺐는지 남긴다.
SKIPPED = [("BISEO_KEY_ODCLOUD", "공공데이터포털(행정 API) — LLM 아님")]


def load_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


async def main() -> int:
    apply = "--apply" in sys.argv

    from app.core.credential_vault import decrypt_value
    from app.core.db_pool import get_pool, init_pool
    from app.core.llm_key_provider import store_api_key

    await init_pool()
    env = load_env(ENV_FILE)
    pool = get_pool()

    rows = await pool.fetch(
        "SELECT key_name, provider, priority, encrypted_value FROM llm_api_keys"
    )
    existing: dict[str, str] = {}
    for r in rows:
        try:
            existing[r["key_name"]] = fp(decrypt_value(r["encrypted_value"]))
        except Exception:  # noqa: BLE001
            existing[r["key_name"]] = "(복호화실패)"
    existing_fps = set(existing.values())

    print(f"AADS 기등록 {len(rows)}건 / 244 env {len(env)}건 / mode={'APPLY' if apply else 'DRY-RUN'}")
    print("-" * 96)

    added = skipped_dup = missing = 0
    for env_key, key_name, provider, priority, label in PLAN:
        val = env.get(env_key)
        if not val:
            print(f"MISS  {key_name:<24} {env_key} 가 244 env 에 없다")
            missing += 1
            continue
        f = fp(val)
        if f in existing_fps:
            owner = next((k for k, v in existing.items() if v == f), "?")
            print(f"DUP   {key_name:<24} 같은 값이 이미 '{owner}' 로 등록됨 (fp={f}) — 건너뜀")
            skipped_dup += 1
            continue
        if apply:
            await store_api_key(key_name, val, provider, label=label, priority=priority)
            print(f"ADD   {key_name:<24} provider={provider:<12} p{priority} fp={f}")
        else:
            print(f"PLAN  {key_name:<24} provider={provider:<12} p{priority} fp={f}")
        added += 1

    print("-" * 96)
    for env_key, key_name in VERIFY_ONLY:
        val = env.get(env_key, "")
        want = fp(val) if val else "-"
        have = existing.get(key_name, "(미등록)")
        mark = "일치" if want == have else "불일치"
        print(f"CHECK {key_name:<24} 244={want} AADS={have} → {mark}")

    for env_key, why in SKIPPED:
        print(f"SKIP  {env_key:<24} {why}")

    print(f"\n요약: {'등록' if apply else '등록 예정'} {added}건 · 중복 {skipped_dup}건 · 누락 {missing}건")
    if not apply:
        print("실제 반영하려면 --apply 를 붙여 다시 실행한다.")
    return 0


sys.exit(asyncio.run(main()))
