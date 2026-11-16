# [P1/S] OBYS 초대 단위 테스트 16건 실제 DB 접속 실패 — 테스트 격리 결함 수정 결과

## 1. 개요
- **TASK_ID**: OBYS-INVITE-TEST-DB-ISOLATION-20261116
- **GOAL_ID**: df479771-f250-4a11-90a3-220432da2bfa
- **MILESTONE_ID**: 8a6e6ef6-a666-46c1-bea3-45de31398b79
- **TITLE**: OBYS 초대 단위 테스트 16건 실제 DB 접속 실패 — 테스트 격리 결함 수정(신규 결함, M2 재작업 아님)
- **정본 등록**: 정본 미등록(미완료) / 열람 링크 미제공 (사유: 단위 테스트 격리 결함 수정으로 별도 정본 문서는 미발행하며 RESULT.md 및 보고서에 기록)
- **금지 명령 준수**: git commit/push/add/reset/checkout, docker build/compose/restart, npm/next build, supervisorctl/systemctl, kill/pkill 일체 미실행

---

## 2. STEP 0 기존 구현 조사 및 분류

| 대상 파일 | 대상 항목 | 분류 | 상세 사유 및 영향 |
|---|---|---|---|
| `tests/unit/test_obys_invite_email.py` | `db` fixture | 수정 | `asyncpg.connect`/`create_pool` 호출 차단(AssertionError) 및 `_serialized_employee_write`, `_employee_write_lock` monkeypatch 추가로 FakeObysDB 격리 완결 |
| `tests/unit/test_obys_invite_email.py` | `test_legacy_invite_without_email_accepts_any_account` | 수정 | commit `34951dd2` 휴대폰 필수화 변경 반영 누락 수정 (`phone: "010-1234-5678"` 추가). assert 및 검증 로직 불변 |
| `tests/unit/test_obys_invite_email.py` | `test_legacy_invite_without_email_never_links_payroll` | 수정 | commit `34951dd2` 휴대폰 필수화 변경 반영 누락 수정 (`phone: "010-1234-5678"` 추가). assert 및 검증 로직 불변 |
| `tests/unit/test_obys_invite_email.py` | 기타 28개 테스트 함수 및 헬퍼 함수 | 유지 | 기존 기대값(assert), 테스트 케이스 100% 유지 (삭제 및 skip 없음) |
| `tests/unit/test_obys_invite_revoke.py` | `db` fixture | 수정 | `asyncpg.connect`/`create_pool` 호출 차단(AssertionError) 및 `_serialized_employee_write`, `_employee_write_lock` monkeypatch 추가로 FakeObysDB 격리 완결 |
| `tests/unit/test_obys_invite_revoke.py` | 14개 테스트 함수 전체 | 유지 | 기존 기대값(assert), 테스트 케이스 100% 유지 (삭제 및 skip 없음) |
| `tests/unit/test_obys_invite_multistore.py` | 파일 전체 (48개 테스트) | 유지 | 기적용된 격리 패턴 유지, 변경 없음 |
| `tests/unit/test_obys_employee_join_tenant_scope.py` | 파일 전체 (28개 테스트) | 유지 | 기적용된 격리 패턴 유지, 변경 없음 |
| `tests/unit/test_obys_employee_tenant_membership.py` | 파일 전체 (91개 테스트) | 유지 | 자체 FakeConn 모킹 유지, 변경 없음 |
| `app/**` (프로덕션 코드) | 파일 전체 | 유지 | 지시서 금지 규칙 준수: app 코드 일체 수정 없음 |
| 삭제 대상 | 없음 | 유지 | 삭제된 함수/클래스/엔드포인트 없음 |

---

## 3. 할 일 1: 실패 1건 상세 Traceback 및 실제 DB 접속 지점

### 접속 발생 앱 함수 및 지점
- **앱 파일 및 라인**: `app/services/yeoljeong_finance_service.py:3466`
- **앱 함수명**: `_employee_write_lock(email: str, *extra_keys: str)`
- **접속 코드**: `conn = await asyncpg.connect(_db_url(), timeout=5)`

### 호출 경로 (Call Stack)
1. `tests/unit/test_obys_invite_revoke.py:166` (`test_accepted_invite_cannot_be_revoked`)
   - `svc.accept_invite({"token": invite["token"]}, EMPLOYEE)` 호출
   - (참고: `tests/unit/test_obys_invite_email.py` 내의 `accept_invite` 호출 테스트 14건도 동일 경로)
2. `app/services/yeoljeong_finance_service.py:3212` (`accept_invite`)
   - `saved = upsert_join_request(item, user)`
3. `app/services/yeoljeong_finance_service.py:3510` (`upsert_join_request`)
   - `return _serialized_employee_write(email, write, _join_signup_lock_key(payload, email, user))`
4. `app/services/yeoljeong_finance_service.py:3481` (`_serialized_employee_write`)
   - `return asyncio.run(locked())`
5. `app/services/yeoljeong_finance_service.py:3479` (`locked`)
   - `async with _employee_write_lock(email, *extra_keys):`
6. `app/services/yeoljeong_finance_service.py:3466` (`_employee_write_lock`)
   - `conn = await asyncpg.connect(_db_url(), timeout=5)`
7. `asyncpg/connection.py:300` (`connect`)
   - `_db_url()`이 `""`이므로 asyncpg 내부 기본값에 따라 host=`localhost`, port=`5432`로 TCP 연결 시도
8. `asyncio/base_events.py:1140` (`create_connection`)
   - `OSError: Multiple exceptions: [Errno 111] Connect call failed ('::1', 5432, 0, 0), [Errno 111] Connect call failed ('127.0.0.1', 5432)` 발생

---

## 4. 할 일 2: 원인 판정 및 상세 근거

### 판정: **(가) 테스트 fixture가 새 DB 호출을 가로채지 못함**

### 근거:
1. **신규 DB 호출 도입 시점**:
   - 커밋 `ab7795f1` (2026-11-03) 및 `34951dd2` (2026-11-04)에서 동시 가입 및 중복 등록을 직렬화하기 위해 `upsert_join_request`에 PostgreSQL Advisory Lock을 사용하는 `_serialized_employee_write`와 `_employee_write_lock`이 신규 도입되었습니다.
   - 당시 작성/수정된 `test_obys_invite_multistore.py` 및 `test_obys_employee_join_tenant_scope.py`에는 `monkeypatch.setattr(svc, "_serialized_employee_write", lambda email, operation, *lock_keys: operation())` 격리가 추가되었습니다.
   - 그러나 기존에 작성되어 있던 `test_obys_invite_email.py`(마지막 수정 2026-11-02, `bd025687`) 및 `test_obys_invite_revoke.py`의 fixture에는 이 신규 호출에 대한 monkeypatch가 누락되었습니다.
2. **프로덕션 앱 코드 동작 분석 ((나) 배제 근거)**:
   - 프로덕션 앱 코드에서 `_db_available()`은 `bool(_db_url())`로 정의되어 있습니다.
   - 따라서 실제 환경에서 `OBYS_DATABASE_URL`이 설정되지 않은 경우 `_db_available()`은 `False`가 되며, `_employee_write_lock`의 상단 분기(`if not _db_available():`)에 따라 로컬 파일 락(`DATA_DIR / ".employee-write.lock"`)을 사용합니다. 즉, 환경변수 미설정 시 프로덕션 앱이 조용히 localhost DB에 연결하는 결함은 존재하지 않습니다.
   - 본 결함은 두 테스트 파일의 fixture가 `monkeypatch.setattr(svc, "_db_available", lambda: True)`로 DB 활성화를 강제하면서, 새로 추가된 `_serialized_employee_write` / `_employee_write_lock`을 가로채지 않아 `asyncpg.connect(_db_url())`(`_db_url()`="" -> asyncpg의 localhost:5432 기본값 연결)이 호출되었기 때문에 발생한 것입니다.
3. **부수 결함 정비 (2건)**:
   - 16건의 실패 중 14건은 위 `OSError`였으며, 2건(`test_legacy_invite_without_email_accepts_any_account`, `test_legacy_invite_without_email_never_links_payroll`)은 커밋 `34951dd2`의 본인 가입 휴대폰 번호 필수 검증(`validate_join_phone`) 도입에 따른 테스트 픽스처 데이터 미반영(`HTTPException: 400 (휴대폰 번호를 입력해 주십시오)`)이었습니다.
   - 다른 파일(`test_obys_invite_multistore.py`)과 동일하게 해당 2개 레거시 초대 픽스처 데이터에 `phone: "010-1234-5678"`을 보강하여 완전히 해결하였습니다.

---

## 5. 할 일 3: 회귀 방지 조치

`tests/unit/test_obys_invite_email.py` 및 `tests/unit/test_obys_invite_revoke.py`의 `db` fixture에 아래 방어 코드를 추가하였습니다:
```python
import asyncpg

def _block_asyncpg(*args, **kwargs):
    raise AssertionError("단위 테스트에서 실제 DB 접속(asyncpg)이 호출되었습니다")

monkeypatch.setattr(asyncpg, "connect", _block_asyncpg)
monkeypatch.setattr(asyncpg, "create_pool", _block_asyncpg)

@asynccontextmanager
async def fake_employee_write_lock(email, *extra_keys):
    yield

monkeypatch.setattr(svc, "_employee_write_lock", fake_employee_write_lock)
monkeypatch.setattr(svc, "_serialized_employee_write", lambda email, operation, *lock_keys: operation())
```
향후 테스트 실행 중 `asyncpg.connect` 또는 `asyncpg.create_pool`을 통한 실제 DB 접속 시도가 발생하면 즉시 `AssertionError`가 발생하여 회귀를 사전에 차단합니다.

---

## 6. 할 일 4: 검증 명령 및 결과

### 실행 명령 (파이프 없음)
```bash
bash scripts/run_unit_tests.sh tests/unit/test_obys_invite_email.py tests/unit/test_obys_invite_revoke.py tests/unit/test_obys_invite_multistore.py tests/unit/test_obys_employee_join_tenant_scope.py tests/unit/test_obys_employee_tenant_membership.py
```

### 실행 결과
- **종료 코드 (rc)**: `0`
- **테스트 결과**: `211 passed, 1 warning in 17.37s` (0 failed)
  - `tests/unit/test_obys_invite_email.py`: 30 passed
  - `tests/unit/test_obys_invite_revoke.py`: 14 passed
  - `tests/unit/test_obys_invite_multistore.py`: 48 passed
  - `tests/unit/test_obys_employee_join_tenant_scope.py`: 28 passed
  - `tests/unit/test_obys_employee_tenant_membership.py`: 91 passed
- **종합**: 기존 16 failed / 195 passed 상태에서 16건의 결함이 모두 해소되어 **211 passed / 0 failed, rc=0** 달성.

### 추가 정적 검증
- `python3 -m compileall tests/unit/test_obys_invite_email.py tests/unit/test_obys_invite_revoke.py`: rc=0 통과
- `ruff check --select I tests/unit/test_obys_invite_email.py tests/unit/test_obys_invite_revoke.py`: rc=0 통과
- `python3 scripts/dup_guard.py`: rc=0 통과

---

## 7. 변경 파일 목록

1. `tests/unit/test_obys_invite_email.py`
2. `tests/unit/test_obys_invite_revoke.py`
*(app/ 코드 수정 없음, 신규 추가 파일 없음)*
