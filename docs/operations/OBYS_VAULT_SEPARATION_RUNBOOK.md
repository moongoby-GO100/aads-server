# 오비서 전용 비밀번호관리자 분리 — Runbook

AADS-OBYS-VAULT-SEPARATION-20260930 · 대상: 진아서버 오비서(`app.obys_standalone`)

## 무엇이 바뀌었나

- 오비서(standalone)는 **`OBYS_VAULT_KEY` 만** 읽는다. `VAULT_ENCRYPTION_KEY`
  (AADS)와 `/app/app/.vault.key` 는 standalone 에서 읽지도, 자동 생성하지도 않는다.
  (`app/core/obys_runtime.py` `RuntimeSettings.apply()` →
  `credential_vault.configure_vault_key()`)
- 키가 없거나 형식이 틀리면 **vault 기능만 꺼진다**. 앱은 뜬다.
  `/auth/login/e2e-inject` 는 503 "자격증명 보관소 비활성" 을 낸다.
  로그에는 설정 이름만 남는다(`OBYS_VAULT_KEY: not set; credential vault disabled`).
- AADS 본체는 그대로다(env → 키 파일 → 자동 생성).
- 오비서 인증 DB 에 `e2e_credentials` 를 만드는 SQL:
  `migrations/20260930_obys_e2e_credentials_jinah.sql`.
  AADS 릴리스가 자동 적용하지 않도록 `scripts/migrations_auto_apply_baseline.txt`
  에 HOLD 로 올려 두었고, SQL 자체도 `chat_sessions` 가 있는 DB(AADS)면 거부한다.

## 1. OBYS_VAULT_KEY 생성·배치 (진아서버)

AADS 키와 **반드시 다른** 새 키를 진아서버에서 만든다. 값을 화면·채팅·커밋에 남기지 않는다.

```bash
# 진아서버, partner 계정
cd /home/partner/obys
KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
umask 077
printf 'export OBYS_VAULT_KEY=%s\n' "$KEY" >> /home/partner/obys/obys.env.sh
unset KEY
chmod 600 /home/partner/obys/obys.env.sh
stat -c '%a %U' /home/partner/obys/obys.env.sh        # 600 partner
grep -c '^export OBYS_VAULT_KEY=' obys.env.sh          # 1 (값은 출력하지 않는다)
grep -c 'VAULT_ENCRYPTION_KEY' obys.env.sh             # 0 이어야 한다
```

키 백업은 CEO 가 지정한 비밀 저장소에만 둔다. 키를 잃으면 오비서 암호문은 복구할 수 없다.

## 2. 마이그레이션 실행 순서

1. **테이블 생성** (진아서버 인증 DB, 소유 역할로):
   ```bash
   psql "$OBYS_AUTH_DATABASE_URL" -v ON_ERROR_STOP=1 \
        -f migrations/20260930_obys_e2e_credentials_jinah.sql
   ```
   앱 역할이 테이블 소유자가 아니면 권한을 준다:
   `GRANT SELECT, INSERT, UPDATE, DELETE ON public.e2e_credentials TO <오비서 앱 역할>;`
2. **dry-run** (기본값). AADS 키와 오비서 키가 한 프로세스에만 존재하도록,
   서버 68 에서 진아서버 DB 로 SSH 터널을 열고 실행한다. AADS 키를 진아서버로 옮기지 않는다.
   ```bash
   # 서버 68
   ssh -N -L 15432:127.0.0.1:5432 <진아서버> &          # 작업 후 반드시 종료 (R-BG)
   export AADS_DATABASE_URL=...                         # AADS DB (읽기 전용 트랜잭션으로만 연다)
   export OBYS_AUTH_DATABASE_URL=postgresql://<역할>@127.0.0.1:15432/<인증DB>
   export OBYS_VAULT_KEY="$(ssh <진아서버> '. /home/partner/obys/obys.env.sh; printf %s "$OBYS_VAULT_KEY"')"
   timeout 300 python3 scripts/obys_vault_migrate.py \
       --aads-key-file /root/aads/aads-server/app/.vault.key   # env VAULT_ENCRYPTION_KEY 가 있으면 그것을 쓴다
   ```
   출력은 행마다 `id/tenant_id/service/project/label/host/action/reason` 과 마지막
   `summary` 한 줄이다. 평문·암호문·키·DSN 은 출력하지 않는다.
   `skip` 사유: `source_decrypt_failed:*`(AADS 키로 안 풀림), `tenant_missing_in_target`,
   `natural_key_taken_by_other_id`.
3. **apply** — dry-run 결과를 CEO 가 확인한 뒤:
   ```bash
   timeout 300 python3 scripts/obys_vault_migrate.py --apply --aads-key-file ...
   unset OBYS_VAULT_KEY AADS_DATABASE_URL OBYS_AUTH_DATABASE_URL
   ```
   한 트랜잭션이라 실패하면 전부 롤백된다(종료코드 1). id 기준 upsert 라 다시 돌려도 된다.
4. **오비서 재기동**은 승인된 창구에서 한다(이 작업 범위 아님). 기동 로그에
   `vault_key_configured source=OBYS_VAULT_KEY enabled=True` 가 보여야 한다.

선택 기준: `login_url` 호스트가 `fb.newtalk.kr` 이거나 `project in ('OBYS','FOOD','ACCT')`.

## 3. 롤백

- **코드**: 커밋을 되돌리면 standalone 도 예전처럼 `VAULT_ENCRYPTION_KEY`/키 파일을 찾는다.
  진아서버 env 에서는 이미 `VAULT_ENCRYPTION_KEY` 를 뺐으므로 되돌려도 vault 는 동작하지
  않는다 — AADS 키를 다시 진아서버에 두는 것은 이 분리의 목적에 반하므로 하지 않는다.
- **데이터**: `migrations/rollback/20260930_obys_e2e_credentials_jinah.down.sql`
  (진아서버 인증 DB 에서 수동). 오비서 쪽 사본만 지운다. AADS 원본은 스크립트가 읽기만
  했으므로 그대로다.
- **키**: `obys.env.sh` 의 `OBYS_VAULT_KEY` 줄을 지우면 vault 만 꺼진다(앱은 뜬다).

## 남은 일 (범위 밖)

- AADS `VAULT_ENCRYPTION_KEY` 교체·재암호화 — 별도 작업.
- 오비서 업무 DB 의 `platform_accounts` 등 `yeoljeong_finance_service._encrypt_secret`
  로 만든 암호문은 AADS 키로 만들어졌다. standalone 에서는 `_decrypt_secret` 가 빈 값을
  돌려준다. 이 암호문의 재암호화는 별도 이관이 필요하다.
