#!/usr/bin/env python3
"""llm_api_keys(provider=codex) 의 계정을 코덱스 CLI 가 읽는 계정 홈으로 구성한다.

2026-09-16 대표님 지시: "진아서버에 코덱스 계정추가하라고 지시했는데 반영된거 맞아?"
→ DB 등록(CODEX_OAUTH_JINAH, 2026-09-15)은 돼 있었으나 CLI 는 계속
   /root/.codex/auth.json(moongoby@gmail.com) 하나만 봤다. 그 간극을 메운다.
설계: aads-docs/docs/PRD-LLM-ACCOUNT-RUNTIME-BINDING-v1.0.md

## 왜 이렇게 넣나

1. **access_token 을 최초 1회 발급해 넣는다.** 빈 값으로 두면 CLI 가 스스로
   갱신하지 않고 401 로 죽는다 — 2026-09-16 실측: `Missing bearer or basic
   authentication in header`. 그래서 refresh_token 으로 한 번 교환해 채워준다.
   이후 만료 갱신은 CLI 가 알아서 하고 파일에 되쓴다.
   회전된 refresh_token 이 응답에 오면 그 값을 파일에 쓴다 — 이 시점부터
   **파일이 진실의 원천**이고 DB 값은 최초 씨앗으로만 유효하다.
2. **MAIN 은 심볼릭 링크로 둔다.** /root/.codex/auth.json 을 복사하면 같은 계정의
   자격증명이 두 파일로 갈라져 각자 갱신하다 한쪽이 무효화된다. 원본 하나를 가리킨다.
3. **이미 있는 계정 홈은 덮지 않는다.** CLI 가 갱신해 넣은 최신 토큰을 DB 씨앗으로
   되돌리면 퇴행이다. --force 를 준 경우에만 다시 만든다.
4. **호스트에서 돈다.** 계정 홈은 호스트 경로이고 컨테이너에는 마운트돼 있지 않다.
   DB 는 psql(docker exec)로 읽고, 복호화는 호스트의 vault 키로 직접 한다
   — restore_claude_slot1.sh 와 같은 방식이다.
5. 값은 읽지도 출력하지도 않는다 (R-KEY). 지문(SHA-256 앞 12자)만 쓴다.

재실행해도 안전하다(멱등). --apply 없이 실행하면 계획만 출력한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ACCOUNTS_ROOT = Path(os.getenv("CODEX_ACCOUNTS_ROOT", "/root/.codex-accounts"))
LEGACY_AUTH = Path("/root/.codex/auth.json")
VAULT_KEY_FILE = Path("/root/aads/aads-server/app/.vault.key")
MAIN_KEY_NAME = "CODEX_OAUTH_MAIN"
PSQL = ["/usr/bin/docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "aads", "-tAc"]


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"psql 실패: {out.stderr.strip()[:200]}")
    return [line.split("|") for line in out.stdout.strip().splitlines() if line]


def decrypt(ciphertext: str) -> str:
    from cryptography.fernet import Fernet

    key = os.getenv("VAULT_ENCRYPTION_KEY", "") or VAULT_KEY_FILE.read_text().strip()
    return Fernet(key.encode()).decrypt(ciphertext.encode()).decode()


def fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def mint_tokens(cfg: dict) -> dict:
    """refresh_token 을 access_token 으로 교환한다.

    요청 형태는 app/core/codex_oauth.py:get_access_token() 과 같게 맞춘다.
    한쪽만 바뀌면 원인을 두 번 추적하게 된다.
    """
    import urllib.request

    body = json.dumps({
        "client_id": cfg["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
        "scope": "openid profile email",
    }).encode()
    req = urllib.request.Request(
        cfg["token_endpoint"], data=body,
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    if not out.get("access_token"):
        raise RuntimeError("토큰 갱신 응답에 access_token 이 없다")
    return out


def _write_auth(path: Path, refresh_token: str, account_id: str,
                access_token: str = "", id_token: str = "") -> None:
    """CLI 가 읽는 형식으로 기록한다."""
    from datetime import datetime, timezone

    payload = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def _link_main() -> str:
    home = ACCOUNTS_ROOT / MAIN_KEY_NAME
    home.mkdir(parents=True, exist_ok=True)
    target = home / "auth.json"
    if target.is_symlink() and os.readlink(target) == str(LEGACY_AUTH):
        return "이미 연결됨"
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(LEGACY_AUTH)
    return "연결 생성"


def main() -> int:
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv

    rows = psql(
        "SELECT key_name, COALESCE(label,''), priority, is_active, encrypted_value "
        "FROM llm_api_keys WHERE provider = 'codex' ORDER BY priority ASC, id ASC"
    )

    print(f"codex 계정 {len(rows)}건 / mode={'APPLY' if apply else 'DRY-RUN'}"
          f"{' --force' if force else ''}")
    print(f"계정 홈 루트: {ACCOUNTS_ROOT}")
    print("-" * 96)

    if LEGACY_AUTH.exists():
        if apply:
            print(f"MAIN  {MAIN_KEY_NAME:<20} {_link_main()} -> {LEGACY_AUTH}")
        else:
            print(f"PLAN  {MAIN_KEY_NAME:<20} -> {LEGACY_AUTH} (심볼릭 링크)")
    else:
        print(f"WARN  {LEGACY_AUTH} 가 없다 — MAIN 계정 홈을 만들 수 없다")

    built = skipped = failed = 0
    for key_name, label, priority, is_active, enc in rows:
        if key_name == MAIN_KEY_NAME:
            continue
        target = ACCOUNTS_ROOT / key_name / "auth.json"

        if target.exists() and not force:
            print(f"KEEP  {key_name:<20} 계정 홈이 이미 있다 — 건드리지 않음 (재구성은 --force)")
            skipped += 1
            continue

        try:
            cfg = json.loads(decrypt(enc))
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {key_name:<20} 복호화/파싱 실패: {type(exc).__name__}")
            failed += 1
            continue

        refresh = cfg.get("refresh_token") or ""
        account_id = cfg.get("account_id") or ""
        if not refresh:
            print(f"FAIL  {key_name:<20} refresh_token 이 없다 — 대화형 재로그인이 필요하다")
            failed += 1
            continue

        note = f"fp={fp(refresh)} account={account_id[:8]} p{priority} active={is_active} {label}"
        if not apply:
            print(f"PLAN  {key_name:<20} {note}")
            built += 1
            continue

        try:
            minted = mint_tokens(cfg)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "code", "") or type(exc).__name__
            print(f"FAIL  {key_name:<20} 토큰 교환 실패({detail}) — refresh_token 이 만료됐을 수 있다")
            failed += 1
            continue

        # 회전된 refresh_token 이 오면 그것을 쓴다. 안 오면 기존 값을 유지한다.
        new_refresh = minted.get("refresh_token") or refresh
        rotated = " refresh회전" if new_refresh != refresh else ""
        _write_auth(target, new_refresh, account_id,
                    access_token=minted.get("access_token", ""),
                    id_token=minted.get("id_token", ""))
        print(f"BUILD {key_name:<20} {note}{rotated}")
        built += 1

    print("-" * 96)
    print(f"요약: {'구성' if apply else '구성 예정'} {built}건 · 유지 {skipped}건 · 실패 {failed}건")
    if not apply:
        print("실제 반영하려면 --apply 를 붙여 다시 실행한다.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
