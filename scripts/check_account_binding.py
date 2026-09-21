#!/usr/bin/env python3
"""등록만 되고 런타임에 닿지 않은 LLM 계정을 찾아낸다.

2026-09-16, 진아서버(244) 계정 추가 지시가 DB 등록까지만 되고 CLI 에는
반영되지 않은 채 하루 넘게 방치됐다. 아무도 몰랐던 이유는 둘을 대조하는
곳이 없었기 때문이다. 이 점검이 있었으면 9/15 에 잡혔다.
설계: aads-docs/docs/PRD-LLM-ACCOUNT-RUNTIME-BINDING-v1.0.md

## 무엇을 대조하나

    레지스트리(llm_api_keys)  ↔  런타임 바인딩(자격증명 파일)

API 키 방식(gemini·groq 등)은 DB 등록만으로 동작하므로 대상이 아니다.
**OAuth 구독 계정(codex·anthropic)만** 본다 — 이쪽은 CLI 홈 디렉터리
작업이 따로 필요하고, 그 단계가 빠지는 것이 이번 사고의 형태였다.

## 판정

- 없음    : DB 에 있는데 자격증명 파일이 없다 → 그 계정은 절대 안 쓰인다
- 갱신불가: 파일은 있는데 refresh token 이 없다 → 발급 8시간 뒤 401 로 죽는다
- 정상    : 파일이 있고 refresh token 도 있다

값은 읽지도 출력하지도 않는다 (R-KEY). 존재 여부만 본다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CODEX_ACCOUNTS_ROOT = Path(os.getenv("CODEX_ACCOUNTS_ROOT", "/root/.codex-accounts"))
CLAUDE_SLOT_ROOT = Path(os.getenv("CLAUDE_RELAY_SLOT_HOME_ROOT", "/root/.claude-relay-slots"))
PSQL = ["/usr/bin/docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "aads", "-tAc"]

# ANTHROPIC_AUTH_TOKEN → slot1, _2 → slot2, _3 → slot3, _4 → slot4
SLOT_OF = {
    "ANTHROPIC_AUTH_TOKEN": "1",
    "ANTHROPIC_AUTH_TOKEN_2": "2",
    "ANTHROPIC_AUTH_TOKEN_3": "3",
    "ANTHROPIC_AUTH_TOKEN_4": "4",
}


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"psql 실패: {out.stderr.strip()[:200]}")
    return [line.split("|") for line in out.stdout.strip().splitlines() if line]


def _codex_binding(key_name: str) -> tuple[str, str]:
    path = CODEX_ACCOUNTS_ROOT / key_name / "auth.json"
    if not path.exists():
        return "없음", f"{path} 가 없다"
    try:
        tokens = json.loads(path.read_text()).get("tokens") or {}
    except (OSError, ValueError) as exc:
        return "읽기실패", f"{type(exc).__name__}"
    if not tokens.get("refresh_token"):
        return "갱신불가", "refresh_token 이 없다"
    return "정상", f"account={str(tokens.get('account_id', ''))[:8]}"


def _claude_binding(key_name: str) -> tuple[str, str]:
    slot = SLOT_OF.get(key_name)
    if not slot:
        return "대상아님", "슬롯 매핑이 없는 키다"
    path = CLAUDE_SLOT_ROOT / f"slot{slot}" / ".claude" / ".credentials.json"
    if not path.exists():
        return "없음", f"slot{slot} 자격증명이 없다 — claude auth login 필요"
    try:
        data = json.loads(path.read_text())
        oauth = data.get("claudeAiOauth") or data
    except (OSError, ValueError) as exc:
        return "읽기실패", f"{type(exc).__name__}"
    if not oauth.get("refreshToken"):
        return "갱신불가", f"slot{slot} 에 refreshToken 이 없다 — 8시간 뒤 401"
    return "정상", f"slot{slot} sub={oauth.get('subscriptionType', '?')}"


def main() -> int:
    rows = psql(
        "SELECT provider, key_name, COALESCE(label,''), priority, is_active "
        "FROM llm_api_keys WHERE provider IN ('codex','anthropic') "
        "ORDER BY provider, priority ASC, id ASC"
    )

    print(f"{'provider':<10} {'key_name':<24} {'판정':<8} 비고")
    print("-" * 100)
    bad = 0
    for provider, key_name, label, priority, is_active in rows:
        verdict, note = (_codex_binding if provider == "codex" else _claude_binding)(key_name)
        if verdict not in ("정상", "대상아님"):
            bad += 1
        flag = "" if is_active == "t" else " [비활성]"
        print(f"{provider:<10} {key_name:<24} {verdict:<8} {note}{flag}")
        print(f"{'':<10} {'':<24} {'':<8} p{priority} {label}")
    print("-" * 100)
    if bad:
        print(f"⚠ 런타임에 닿지 않는 계정 {bad}건 — 등록만 되고 실제로는 쓰이지 않는다")
    else:
        print("모든 OAuth 계정이 런타임에 바인딩돼 있다")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
