# AADS VAULT_ENCRYPTION_KEY 교체 — Runbook

AADS-VAULT-KEY-ROTATION-20260930 · 대상: 서버 68 AADS 본체(`aads-server` 블루/그린, `yeoljeong-finance`, `yeoljeong-finance-worker`)

## 왜

2026-09-30, AADS 비밀번호관리자 마스터 키 `VAULT_ENCRYPTION_KEY` 가 진아서버 오비서 env 파일에
복사돼 있었다. 파일은 지웠지만 값은 여전히 유효하다. 키를 바꾸고 옛 키로 된 암호문을 모두
재암호화한 뒤 옛 키를 버려야 노출이 끝난다.

## 무엇이 바뀌었나 (코드)

- `app/core/credential_vault.py` `_get_fernet()` — AADS 모드에서
  `VAULT_ENCRYPTION_KEY_PREVIOUS`(쉼표 구분 다중)가 있으면 `MultiFernet([주키, 이전키...])`.
  - 암호화는 항상 주키, 복호화는 모든 키로 시도한다. 재암호화 중에도 옛 암호문이 읽힌다.
  - 이전키가 없으면 예전과 똑같이 단일 `Fernet` 이다(env → `/app/app/.vault.key` → 자동 생성).
  - 형식이 틀린 이전키는 건너뛰고 `vault_previous_key_invalid index=N skipped` 경고만 남긴다.
    기동은 막지 않는다. 키 값은 로그에 없다. 주키와 같은 값은 조용히 제외한다.
  - 로드되면 `vault_multifernet_enabled previous_keys=N` 이 한 번 찍힌다.
- 오비서 standalone(`is_standalone_vault()`)은 **바뀌지 않았다.** `OBYS_VAULT_KEY` 단일 키만 쓰고
  `VAULT_ENCRYPTION_KEY_PREVIOUS` 를 읽지 않는다(`configure_vault_key()` 가 이전키 목록을 비운다).
- `scripts/vault_rotate_reencrypt.py` — 재암호화. 기본 dry-run, `--apply` 로만 쓴다.
- 테스트: `tests/unit/test_vault_key_rotation.py`

## 대상 테이블 (2026-09-30 실측 행수)

| 테이블 | 행수 | 암호화 컬럼 |
|---|---:|---|
| agent_vault_credentials | 366 | username_enc, password_enc |
| e2e_credentials | 43 | username_enc, password_enc, extra_fields(JSON 값 단위) |
| llm_api_keys | 30 | encrypted_value |
| user_api_keys | 1 | encrypted_key |

## 0. 사전 확인

- CEO 승인. `.env` 수정과 컨테이너 재생성이 들어간다(R-DOCKER).
- 현재 키가 어디서 오는지 확인한다. 값은 출력하지 않는다.

```bash
cd /root/aads/aads-server
grep -c '^VAULT_ENCRYPTION_KEY=' .env                 # 1 이면 .env 가 원본
grep -c '^VAULT_ENCRYPTION_KEY_PREVIOUS=' .env        # 0 이어야 한다(이전 교체 잔재 없음)
docker exec aads-server sh -c 'test -n "$VAULT_ENCRYPTION_KEY" && echo env || echo file'
```

`file` 이면 현재 키는 `/app/app/.vault.key` 에 있다. 아래 1단계에서 그 파일 값을 이전키로 쓴다.

- `.env` 백업: `cp -p .env .env.bak.$(date +%Y%m%d%H%M)` (권한 600 유지, 커밋 금지).

## 1. 새 키 생성 → `.env` 배치

새 키를 주키로, 기존 값을 이전키로 옮긴다. 값을 화면·채팅·커밋·셸 히스토리에 남기지 않는다.

```bash
cd /root/aads/aads-server
umask 077
python3 - <<'EOF'
import re
from pathlib import Path
from cryptography.fernet import Fernet
p = Path(".env")
text = p.read_text()
m = re.search(r"^VAULT_ENCRYPTION_KEY=(.*)$", text, re.M)
old = m.group(1).strip() if m else Path("/root/aads/aads-server/app/.vault.key").read_text().strip()
Fernet(old.encode())                     # 형식 확인
new = Fernet.generate_key().decode()
lines = [l for l in text.splitlines() if not l.startswith(("VAULT_ENCRYPTION_KEY=", "VAULT_ENCRYPTION_KEY_PREVIOUS="))]
lines += [f"VAULT_ENCRYPTION_KEY={new}", f"VAULT_ENCRYPTION_KEY_PREVIOUS={old}"]
p.write_text("\n".join(lines) + "\n")
print("ok")                              # 값은 출력하지 않는다
EOF
grep -c '^VAULT_ENCRYPTION_KEY=' .env; grep -c '^VAULT_ENCRYPTION_KEY_PREVIOUS=' .env   # 1, 1
```

(키 파일 경로는 호스트 기준으로 확인해서 고친다. 컨테이너 안 경로는 `/app/app/.vault.key`.)
새 키 백업은 CEO 가 지정한 비밀 저장소에만 둔다.

## 2. 무중단 배포 — 키를 읽는 **모든** 프로세스

`docker-compose.prod.yml` 의 해당 서비스는 모두 `env_file: .env` 를 읽으므로
`VAULT_ENCRYPTION_KEY_PREVIOUS` 는 compose 수정 없이 들어간다. 환경변수는 컨테이너
재생성으로만 바뀐다(`reload-api.sh` 로는 안 바뀐다).

1. API: `bash /root/aads/aads-server/deploy.sh bluegreen`
2. `yeoljeong-finance`, `yeoljeong-finance-worker`: 같은 `encrypt_value` 를 쓰므로 반드시 재생성
   (`docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate <서비스>` — 단일 서비스만, R-DOCKER).
   **빠뜨리면 그 컨테이너가 계속 옛 키로 암호화하고, 5단계에서 이전키를 지우는 순간 그 값들을 못 읽는다.**
3. 확인:

```bash
for c in aads-server yeoljeong-finance yeoljeong-finance-worker; do
  docker exec "$c" sh -c 'test -n "$VAULT_ENCRYPTION_KEY_PREVIOUS" && echo "$HOSTNAME prev=set" || echo "$HOSTNAME prev=MISSING"'
done
docker logs --since 10m aads-server 2>&1 | grep -E 'vault_multifernet_enabled|vault_previous_key_invalid'
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
```

`vault_multifernet_enabled` 는 첫 암·복호화 시점에 찍힌다(지연 로드). 대시보드에서 비밀번호관리자
항목 하나, LLM 키 관리 화면을 열어 복호화가 되는지 본다.

## 3. dry-run

컨테이너 안에서 돌린다(키·DSN 이 env 로 이미 있다 — 인자로 넘기지 않는다).

```bash
docker exec aads-server python3 /app/scripts/vault_rotate_reencrypt.py            # 전체
docker exec aads-server python3 /app/scripts/vault_rotate_reencrypt.py --table llm_api_keys
```

출력은 테이블당 JSON 한 줄(건수와 id 만):

```
{"table": "llm_api_keys", "mode": "dry-run", "rows": 30, "current": 0, "rotate": 30,
 "undecryptable": 0, "undecryptable_ids": [], "failed_ids": [], "status": "ok", "updated": 0}
```

- `rotate` — 이전키 암호문. `--apply` 대상.
- `current` — 이미 주키. 건너뛴다(재실행 멱등).
- `undecryptable` — 어떤 키로도 안 풀린다. 교체 전부터 깨진 값이라 손대지 않는다. id 를 따로 조사한다.
  (`e2e_credentials.extra_fields` 의 안 풀리는 값은 평문 메타데이터로 보고 세지 않는다.)
- `failed_ids` 가 있거나 `status != ok` 면 멈추고 원인을 본다. 종료코드 1.

## 4. `--apply`

테이블 하나씩, 작은 것부터.

```bash
for t in user_api_keys llm_api_keys e2e_credentials agent_vault_credentials; do
  docker exec aads-server python3 /app/scripts/vault_rotate_reencrypt.py --apply --table "$t" || break
done
```

- 테이블 단위 트랜잭션, `SELECT ... FOR UPDATE` 로 잠근다(수백 행이라 짧다).
- 행마다 재암호화 → 주키 단독 재복호화 검증 → UPDATE → 다시 읽어 digest 검증.
  하나라도 실패하면 **그 테이블 전체 롤백**, `failed_ids` 에 id 만 남는다.
- 다시 돌려도 안전하다. 두 번째 실행은 `rotate: 0` 이어야 한다.

## 5. 검증

```bash
docker exec aads-server python3 /app/scripts/vault_rotate_reencrypt.py            # 모든 테이블 rotate=0
```

SQL(값은 보지 않는다 — 건수만):

```sql
SELECT 'agent_vault_credentials', count(*) FROM agent_vault_credentials
UNION ALL SELECT 'e2e_credentials', count(*) FROM e2e_credentials
UNION ALL SELECT 'llm_api_keys', count(*) FROM llm_api_keys
UNION ALL SELECT 'user_api_keys', count(*) FROM user_api_keys;   -- 366 / 43 / 30 / 1 과 같은지
SELECT count(*) FROM agent_vault_credentials WHERE updated_at > now() - interval '1 hour';  -- 참고용
```

앱 기능 확인: 비밀번호관리자 조회, LLM 호출(`llm_api_keys` 경유 키), e2e 로그인 주입 1건.

## 6. 안정화 후 이전키 제거

최소 24시간, 위 dry-run 이 계속 `rotate: 0` 이고 `vault_*` 오류 로그가 없을 때:

1. 아래 "범위 밖" 저장소에 옛 키 암호문이 남아 있지 않은지 **먼저** 확인한다. 남아 있으면 제거하지 않는다.
2. `.env` 에서 `VAULT_ENCRYPTION_KEY_PREVIOUS` 줄 삭제.
3. 2단계와 같은 방식으로 세 서비스 모두 재생성.
4. 로그에 `vault_multifernet_enabled` 가 더 이상 없고, 대시보드 복호화가 되는지 확인.
5. 호스트의 `/app/app/.vault.key` (옛 키 사본)가 있으면 CEO 승인 후 삭제. `.env.bak.*` 도 파기.

## 롤백

- **재암호화 전(1~3단계)**: `.env` 에서 두 줄을 원래대로(`VAULT_ENCRYPTION_KEY=<옛 키>`,
  PREVIOUS 삭제) 되돌리거나 `.env.bak.*` 복원 → 2단계처럼 재생성. 그 사이 새 키로 저장된 값이
  있을 수 있으므로 되돌릴 때는 **이전키 자리에 새 키를 둔다**:
  `VAULT_ENCRYPTION_KEY=<옛 키>`, `VAULT_ENCRYPTION_KEY_PREVIOUS=<새 키>`.
- **재암호화 후(4단계 이후)**: 두 값을 맞바꾼다 — `VAULT_ENCRYPTION_KEY=<옛 키>`,
  `VAULT_ENCRYPTION_KEY_PREVIOUS=<새 키>` → 재생성. MultiFernet 이라 양쪽 암호문 모두 읽힌다.
  필요하면 같은 스크립트로 옛 키 쪽으로 재암호화된다(스크립트는 "현재 주키"로 옮긴다).
- 코드 롤백: 이 커밋을 revert 해도 이전키 미설정 상태면 동작이 같다. 단 **재암호화 뒤 코드만
  되돌리면 단일 Fernet 이 되므로** 주키가 새 키인지 반드시 확인한다.

## 범위 밖 (별도 작업)

- `app/services/yeoljeong_finance_service.py::_encrypt_secret` 도 같은 `encrypt_value` 를 쓴다.
  그래서 **진아서버 오비서 업무 DB(`platform_accounts` 등)** 에 옛 AADS 키 암호문이 남아 있다.
  이번 교체 대상이 아니다. 오비서는 이제 `OBYS_VAULT_KEY` 만 쓰므로 그 값들은 별도 작업으로
  `OBYS_VAULT_KEY` 로 재암호화해야 한다. 그 전에 옛 키를 완전히 파기하면 복구할 수 없다 —
  옛 키 값은 CEO 비밀 저장소에 해당 작업이 끝날 때까지 보관한다.
- 서버 68 의 `yeoljeong-finance` 업무 DB 에 `_encrypt_secret` 암호문이 있다면 그것도 이 스크립트
  대상이 아니다. 6단계 전에 확인한다.
- `scripts/materialize_codex_accounts.py` 는 `VAULT_ENCRYPTION_KEY` 단일 Fernet 으로 복호화한다.
  1단계 이후 4단계(`llm_api_keys`) 완료 전에는 옛 키 암호문을 못 읽으니 그 사이에는 돌리지 않는다.
