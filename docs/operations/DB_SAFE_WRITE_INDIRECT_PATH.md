# db_safe_write 간접 쓰기 경로 차단과 점검 (2026-09-30)

대상 코드: `app/services/db_write_sql_guard.py`, `app/services/vault_cleanup_guard.py`,
`app/services/tool_executor.py` 의 `_db_safe_write`.
배경: 2026-09-22 계정 일괄삭제 사고(오류사전 `auth.test_account_sweep_deletes_real_tenants`)와
`db.safe_write_multi_statement_commits_before_guard` 의 잔여 위험.

## 1. 무엇이 문제였나

Vault 보호 가드는 SQL 문자열에 `saas_users`·`tenant_memberships`·`tenants` 가 **보이는** 쓰기만
보호 판정을 탄다. 이름이 보이지 않는 경로는 가드를 지나쳤다.

| 우회 경로 | 예 |
|---|---|
| 저장 함수·프로시저 | `SELECT fn(..)`, `CALL proc()`, `DO $$ .. $$`, `SET col = fn(..)` |
| 뷰·INSTEAD OF 트리거 | 업데이트 가능 뷰 `v_accounts` 를 UPDATE |
| 대상 확정 실패 | `db.schema.table` 3단 이름, 예상 밖 문법 → 이름 검사가 빗나가도 조용히 통과 |
| 일반 테이블의 트리거 | 다른 테이블 UPDATE 가 트리거로 보호 테이블을 변경 (정적 차단 불가 — 5절) |

원칙은 **fail closed**: 정적으로 전부 알아낼 수 없으므로 모르는 것은 막거나, 막지 못하면 보호 검사를 강제한다.

## 2. 차단 규칙

| 입력 형태 | 판정 | 사유 | 위치 |
|---|---|---|---|
| 첫 단어 `CALL` / `DO` / `EXECUTE` / `PERFORM` | 거부 (DB 연결 전) | 저장 프로시저·익명 블록·준비된 문장 | `db_write_sql_guard.PROCEDURAL_KEYWORDS` |
| `SELECT ... fn(...)` | 거부 (DB 연결 전) | 저장 함수 호출. 함수 없는 `SELECT` 는 기존 메시지 `INSERT/UPDATE/DELETE만 허용` | `validate_single_write` |
| INSERT/UPDATE/DELETE 안에서 허용 목록 밖 함수 호출(서브쿼리·RETURNING 포함) | 거부 (DB 연결 전) | 저장 함수일 수 있음 | `find_disallowed_function_calls` |
| 인용 식별자 함수 `"fn"(..)` | 거부 | 렉서가 대소문자를 잃어 `"LOWER"` 가 내장으로 오인됨 | 〃 |
| `schema.fn(..)` | `pg_catalog.` 만 허용 목록과 대조, 나머지 거부 | 사용자 스키마 함수 | 〃 |
| `OPERATOR(..)` | 거부 | 사용자 연산자는 함수를 부른다 | 〃 |
| 내장 순수 함수(`now()`, `coalesce`, `lower`, `jsonb_set` …) | 허용 | 실무 오탐 방지 | `ALLOWED_BUILTIN_FUNCTIONS` |
| 대상 테이블이 **뷰**(`information_schema.views` 또는 `pg_class.relkind='v'`) | 거부 (연결 후, 쓰기 전) | 업데이트 가능 뷰·INSTEAD OF 트리거 | `vault_cleanup_guard.assert_target_not_view` |
| 뷰 여부 조회 실패 | 거부 | 확정 못 하면 막는다 | 〃 (`VaultGuardUnavailable`) |
| 대상 테이블 확정 불가(`db.schema.table` 등) | **보호 검사 강제**(전후 스냅샷 비교, 위반 시 전체 롤백) | 이름 검사가 빗나가도 결과는 검증한다 | `target_unresolved` → `_db_safe_write_vault_guarded` |
| 보호 테이블 이름이 식별자·리터럴에 보임 | 보호 검사(기존 동작, 불변) | P1-A | `references_login_tables` |
| 다중 문장·트랜잭션 제어문 | 거부 (기존 동작, 불변) | P1-B | `validate_single_write` |

뷰 조회는 **캐시하지 않는다.** 뷰는 마이그레이션으로 언제든 생기고, 캐시는 그 사이 창을 우회로로 만든다.
쓰기 1건당 쿼리 1회. 스키마 없는 이름은 검색 경로를 추측하지 않고 모든 스키마의 같은 이름 뷰를 대상으로 본다
(같은 이름의 뷰가 다른 스키마에 있으면 오탐 — 스키마를 붙여 쓰면 그 스키마만 본다).

## 3. 점검 쿼리 (읽기 전용 — 운영 DB 를 바꾸지 않는다)

읽기 전용 세션에서만 실행한다. 이 문서는 쿼리만 싣는다. DDL/DML 은 이 작업 범위가 아니다.

```sql
SET default_transaction_read_only = on;
```

### 3-1. 보호 테이블 3종에 붙은 트리거

```sql
SELECT n.nspname        AS table_schema,
       c.relname        AS table_name,
       t.tgname         AS trigger_name,
       t.tgenabled      AS enabled,           -- O=활성 D=비활성 R=replica A=always
       pn.nspname       AS function_schema,
       p.proname        AS function_name,
       pg_get_triggerdef(t.oid) AS definition
  FROM pg_trigger t
  JOIN pg_class c      ON c.oid = t.tgrelid
  JOIN pg_namespace n  ON n.oid = c.relnamespace
  JOIN pg_proc p       ON p.oid = t.tgfoid
  JOIN pg_namespace pn ON pn.oid = p.pronamespace
 WHERE NOT t.tgisinternal
   AND c.relname IN ('saas_users', 'tenant_memberships', 'tenants')
 ORDER BY n.nspname, c.relname, t.tgname;
```

### 3-2. 보호 테이블을 쓰는(참조하는) 저장 함수·프로시저

`prosrc` 본문에서 이름을 찾는 방식이라 **동적 SQL 로 이름을 조립하는 함수는 잡히지 않는다** — 0건이 "없음"의 증명이 아니다.

```sql
SELECT n.nspname AS function_schema,
       p.proname AS function_name,
       CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure' END AS kind,
       l.lanname AS language,
       p.prosecdef AS security_definer,
       pg_get_function_identity_arguments(p.oid) AS arguments,
       (p.prosrc ~* '(insert[[:space:]]+into|update|delete[[:space:]]+from|merge[[:space:]]+into)[[:space:]]+(only[[:space:]]+)?([a-z_"]+\.)?"?(saas_users|tenant_memberships|tenants)"?') AS looks_like_write
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  JOIN pg_language  l ON l.oid = p.prolang
 WHERE p.prokind IN ('f', 'p')
   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND l.lanname NOT IN ('internal', 'c')
   AND p.prosrc ~* '(saas_users|tenant_memberships|tenants)'
 ORDER BY looks_like_write DESC, n.nspname, p.proname;
```

### 3-3. 보호 테이블에 의존하는 뷰

```sql
SELECT DISTINCT v.oid::regclass AS view_name,
                d.refobjid::regclass AS base_table,
                v.relkind
  FROM pg_depend d
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class  v ON v.oid = r.ev_class
 WHERE d.classid    = 'pg_rewrite'::regclass
   AND d.refclassid = 'pg_class'::regclass
   AND d.refobjid IN (to_regclass('public.saas_users'),
                      to_regclass('public.tenant_memberships'),
                      to_regclass('public.tenants'))
   AND v.oid <> d.refobjid
   AND v.relkind IN ('v', 'm')
 ORDER BY 1;
```

뷰 위 INSTEAD OF 트리거는 3-1 의 `table_name` 자리에 뷰 이름으로 나오지 않는다(대상이 보호 테이블이 아니므로). 3-3 에서 나온 뷰마다 아래를 돌린다.

```sql
SELECT c.oid::regclass AS view_name, t.tgname, pg_get_triggerdef(t.oid) AS definition
  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
 WHERE NOT t.tgisinternal AND c.relkind = 'v'
 ORDER BY 1, 2;
```

### 3-4. 허용 목록 이름을 가로채는 사용자 정의 함수

허용 목록은 이름만 본다. `public` 등에 같은 이름·다른 시그니처의 사용자 함수가 있으면 인자 타입에 따라
그쪽이 선택될 수 있다. 결과가 0건이어야 한다.

```bash
python3 -c "from app.services.db_write_sql_guard import ALLOWED_BUILTIN_FUNCTIONS as F; print('{' + ','.join(sorted(F)) + '}')"
```

출력(`{a,b,...}`)을 아래 `:names` 자리에 `'...'::text[]` 로 넣는다.

```sql
SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) AS arguments
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND p.proname = ANY (:names)
 ORDER BY 1, 2;
```

## 4. 오탐 신고 경로

정상 쓰기가 막히면 (`허용 목록에 없는 함수 호출 차단(<이름>)` 또는 `뷰 대상 쓰기 차단`):

1. **우회하지 않는다.** 함수를 인용 식별자·별칭으로 감싸거나 문장을 쪼개는 방법은 모두 막혀 있고,
   시도하면 진짜 위반과 구분되지 않는다. 가드를 끄는 환경변수는 없다.
2. 메시지의 이름이 **순수 내장 함수**인지 확인한다: `SELECT proname, pronamespace::regnamespace, provolatile, prokind FROM pg_proc WHERE proname = '<이름>';`
   `pg_catalog` 소속이고 테이블을 읽거나 SQL 을 실행하지 않는 계산이면 허용 목록 후보다.
3. 뷰 오탐(같은 이름의 뷰가 **다른 스키마**에 있음)은 대상에 스키마를 붙여 다시 시도한다(`public.<이름>`).
   진짜 뷰 대상이면 기반 테이블을 직접 지정한다.
4. 후보로 남긴다: `python3 scripts/error_book.py match -` 로 알려진 것인지 보고, 아니면 `status=candidate` 로 기록한다(R-ERRBOOK).
5. 허용 목록 추가는 **코드 변경**이다. `ALLOWED_BUILTIN_FUNCTIONS` 에 근거 주석과 테스트를 붙여 Runner 작업으로 요청하고
   CEO 승인 후 배포한다. 사용자 정의 함수는 추가하지 않는다 — 그 함수가 하는 일을 SQL 로 풀어 쓴다.

## 5. 남는 위험 (정적으로 못 막는 것)

- **일반 테이블의 트리거**가 보호 테이블을 바꾸는 경우: 이름도 함수 호출도 SQL 에 없다. 3-1·3-3 으로 존재 여부를 점검하고,
  나오면 개별 대응한다(트리거 제거는 별도 CEO 승인 DDL).
- 컬럼 DEFAULT·GENERATED·도메인 CHECK 가 부르는 사용자 함수, 암묵적 형 변환 함수 — 마찬가지로 SQL 에 보이지 않는다.
- 이 경우에도 **대상이 확정되지 않거나 보호 테이블이 보이는** 쓰기는 전후 스냅샷 비교를 타므로 계정 삭제·비활성 결과는 잡힌다.
  가장 강한 대안은 모든 쓰기에 스냅샷 비교를 거는 것이지만 `agent_vault_credentials`·`e2e_credentials` 에
  `SHARE` 락을 쓰기마다 잡는 비용이 있어 이번 범위에 넣지 않았다.
