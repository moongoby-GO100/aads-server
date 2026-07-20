# tests/ 보안 제어 검증 레코드

## 2026-07-20 - yeoljeong platform_accounts 보안 제어 테스트 기록

### 격리 픽스처 (autouse)

`tests/unit/test_yeoljeong_finance_service.py`의 모든 테스트는 `monkeypatch.setenv("YEOLJEONG_FINANCE_DATA_DIR", str(tmp_path))`로 운영 JSON 디렉토리를 임시 경로로 교체한다. 이를 통해 테스트가 `app/data/yeoljeong_finance/platform_accounts.json` 등 실운영 파일에 쓰이지 않는다.

이전 284줄 버전의 테스트 파일(autouse 없음)은 컨테이너 내부에 구버전이 남아 운영 데이터를 오염시킨 바 있다(커밋 0c4218ce에서 복원). 현재 427줄 버전은 `tmp_path`/`monkeypatch` 기반 격리로 동일 문제를 방지한다.

### 보안 테스트 4건 — 코드와 1:1 대응

| 테스트 | 검증 대상 | 관련 코드 |
|---|---|---|
| `test_upsert_account_stores_encrypted_password_only` (L149) | `upsert_account()` 응답에 `password`·`password_enc` 없음, JSON에 `password_enc` 저장, `password` 없음 | `yeoljeong_finance_service.py` L1880-1881, L1902-1903 |
| `test_list_accounts_migrates_legacy_plain_password` (L175) | 레거시 평문 `password` → `password_enc` 자동 마이그레이션 후 API 응답에서 제거 | `yeoljeong_finance_service.py` L816-826, L1635-1645 |
| `test_upsert_account_rejects_cross_business_branch_scope` (L201) | 사업자-지점 불일치 시 HTTP 400 | `yeoljeong_finance_service.py` L833-841 |
| `test_delivery_ledger_requires_admin` (L221) | 비관리자 정산 원장 접근 차단 | `yeoljeong_finance_service.py` L1907-1910 |

### 핵심 보안 제어 코드 스니펫

**① 비밀번호 API 응답에서 제거 (`list_accounts`, L1642)**
```python
item = {k: v for k, v in row.items() if k not in {"password", "password_enc"}}
item["password_masked"] = "********" if _has_account_secret(row) else ""
```

**② 입력 비밀번호 암호화 + 원문 제거 (`upsert_account`, L1880-1881, 1902-1903)**
```python
record["password_enc"] = _encrypt_secret(incoming_password)
record.pop("password", None)
# ...
public = {k: v for k, v in record.items() if k not in {"password", "password_enc"}}
public["password_masked"] = "********" if _has_account_secret(record) else ""
```

**③ 레거시 평문 JSON 마이그레이션 (`_migrate_platform_account_secrets`, L816-826)**
```python
def _migrate_platform_account_secrets(rows: list[dict[str, Any]]) -> bool:
    changed = False
    for row in rows:
        plaintext = str(row.get("password") or "")
        if not plaintext:
            continue
        if not row.get("password_enc"):
            row["password_enc"] = _encrypt_secret(plaintext)
        row.pop("password", None)
        changed = True
    return changed
```

**④ DB 레벨 평문 제거 (`migrations/116_yeoljeong_finance_delivery_ledgers.sql`, L65-67)**
```sql
UPDATE yeoljeong_platform_accounts
   SET payload = payload - 'password'
 WHERE payload ? 'password';
```

### Git 커밋 귀속

- 보안 제어 코드 최초 도입: `a6578cfb` (feat: add yeoljeong finance module)
- 마이그레이션·수집 경로 강화: `591388ab` (feat: harden yeoljeong delivery collection)
- 테스트 오염 복원 + 재검증: `0c4218ce` (fix: purge test-contaminated platform_accounts)

### 최신 테스트 실행 결과

```
tests/unit/test_yeoljeong_finance_service.py  17 passed  (2026-07-20)
tests/unit/test_tools_and_pipeline.py         56 passed  (2026-07-20)
```
