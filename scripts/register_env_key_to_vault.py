#!/usr/bin/env python3
"""환경변수로만 존재하는 시크릿을 llm_api_keys 레지스트리(Fernet 암호화)에 등록한다.

배경. GitHub 토큰은 `.env` 와 `~/.config/gh/hosts.yml` 두 곳에만 평문으로 있었다.
llm_api_keys 는 이미 Fernet 암호화 + 감사로그(llm_key_audit_logs) + 교체 UI 를
갖고 있는데, LLM 이 아닌 키라는 이유로 그 밖에 놓여 있었다. 레지스트리에 넣으면
`llm_key_provider.get_api_key(key_name, fallback_env=...)` 한 경로로 읽히고,
교체 이력이 남는다. env 는 DB 장애 시 폴백으로만 남는다.

값은 인자로 받지 않는다 — 셸 히스토리·프로세스 목록에 평문이 남지 않도록
반드시 환경변수에서만 읽는다.

사용:
    docker exec aads-server python3 /app/scripts/register_env_key_to_vault.py \
        --provider github --key-name GITHUB_TOKEN --label "moongoby-GO100 (device flow)"

멱등하다. 같은 key_name 을 다시 실행하면 값이 같으면 no-op, 다르면 갱신한다.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

sys.path.insert(0, "/app")

import asyncpg  # noqa: E402

from app.core.credential_vault import decrypt_value, encrypt_value  # noqa: E402


def _fingerprint(value: str) -> str:
    """평문을 남기지 않고 같은 값인지만 비교하기 위한 지문."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


async def register(
    *,
    provider: str,
    key_name: str,
    env_name: str,
    label: str,
    notes: str,
    priority: int,
    dry_run: bool,
) -> int:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        print(f"FAIL: 환경변수 {env_name} 가 비어 있다. 컨테이너 안에서 실행했는지 확인하라.")
        return 2

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("FAIL: DATABASE_URL 미설정")
        return 2

    encrypted = encrypt_value(raw)
    if decrypt_value(encrypted) != raw:
        print("FAIL: 암복호 왕복 검증 실패 — VAULT_ENCRYPTION_KEY 확인 필요")
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        existing = await conn.fetchrow(
            "SELECT id, provider, encrypted_value, is_active FROM llm_api_keys WHERE key_name = $1",
            key_name,
        )
        if existing:
            try:
                same = decrypt_value(existing["encrypted_value"]) == raw
            except Exception:
                same = False
            if same and existing["is_active"]:
                print(f"OK(no-op): {key_name} 이미 동일 값으로 등록됨 (id={existing['id']}, fp={_fingerprint(raw)})")
                return 0
            action = "update"
        else:
            action = "create"

        if dry_run:
            print(f"DRY-RUN: {action} {provider}/{key_name} fp={_fingerprint(raw)}")
            return 0

        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO llm_api_keys (
                    provider, key_name, encrypted_value, label, priority, notes,
                    is_active, last_verified_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, TRUE, NOW(), NOW())
                ON CONFLICT (key_name) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    encrypted_value = EXCLUDED.encrypted_value,
                    label = EXCLUDED.label,
                    notes = EXCLUDED.notes,
                    is_active = TRUE,
                    last_verified_at = NOW(),
                    updated_at = NOW()
                RETURNING id
                """,
                provider,
                key_name,
                encrypted,
                label,
                priority,
                notes,
            )
            await conn.execute(
                """
                INSERT INTO llm_key_audit_logs (key_id, provider, key_name, event_type, actor, details)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                row["id"],
                provider,
                key_name,
                action,
                "register_env_key_to_vault",
                json.dumps({"source_env": env_name, "fingerprint": _fingerprint(raw), "label": label}),
            )

        verify = await conn.fetchval(
            "SELECT encrypted_value FROM llm_api_keys WHERE key_name = $1", key_name
        )
        if decrypt_value(verify) != raw:
            print("FAIL: 저장 후 복호 검증 불일치")
            return 1
        print(f"OK({action}): {provider}/{key_name} id={row['id']} fp={_fingerprint(raw)} 복호 검증 통과")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--key-name", required=True)
    ap.add_argument("--env-name", default="", help="읽을 환경변수명 (기본: --key-name 과 동일)")
    ap.add_argument("--label", default="")
    ap.add_argument("--notes", default="")
    ap.add_argument("--priority", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    return asyncio.run(
        register(
            provider=args.provider.strip().lower(),
            key_name=args.key_name.strip(),
            env_name=(args.env_name or args.key_name).strip(),
            label=args.label,
            notes=args.notes,
            priority=args.priority,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
