---
name: safe-db-query
description: "PostgreSQL 데이터베이스를 안전하게 읽기 전용으로 조회할 때 쓴다. 스키마 먼저 확인, SELECT 한 문장, LIMIT·타임아웃, 민감 열 제외 원칙과 사내 도구(query_database, query_project_database)의 사용 한계를 안내하고, 쓰기가 필요하면 db_safe_write 와 승인 절차로 넘긴다. '이 테이블 조회해줘', '데이터 확인해줘', '건수 세어줘' 같은 요청에 쓴다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
source: "https://github.com/sanjay3290/ai-skills/tree/main/skills/postgres"
license: Apache-2.0
adapted: 2026-10-28
---
# 안전한 읽기 전용 DB 조회

조회는 데이터를 바꾸지 않지만 **느린 질의는 운영 DB 를 막고, 넓은 조회는 민감 정보를 노출**한다.
이 스킬은 조회 요령과 사내 도구의 경계를 안내한다. 질의 실행 권한은 주지 않으며, 호출한 세션이 가진 도구로 수행한다.

## 도구 고르기
| 목적 | 도구 | 비고 |
|---|---|---|
| AADS 본체 DB 조회 | `query_database` | `sql` 파라미터. SELECT 만 |
| KIS·GO100·SF·NTV2 등 프로젝트 DB 조회 | `query_project_database` | 어떤 DB 가 있는지는 `list_project_databases` 로 먼저 본다 |
| 쓰기(추가·수정·삭제) | `db_safe_write` | 이 스킬의 범위 밖. 아래 "쓰기가 필요할 때" |
구현은 `app/services/tool_executor.py` 의 `_query_database`, `_query_project_database`, `_db_safe_write` 이다. 동작이 의심되면 코드를 `Read` 로 직접 확인한다.

## 조회 순서
1. **어느 DB 인가.** 프로젝트가 AADS 가 아니면 `list_project_databases` 로 접근 가능한 DB 를 확인하고 요청 내용과 맞는 것을 고른다. 모르면 추측하지 말고 사용자에게 묻는다.
2. **구조 먼저.** 열 이름을 기억에 의존하지 않는다. `information_schema.columns` 를 SELECT 로 조회해 테이블·열·타입을 확인한다. 마이그레이션 파일(`migrations/`)을 `Grep` 하면 의도와 제약도 볼 수 있다.
3. **작게 시작.** 처음에는 건수(`COUNT(*)`)나 `LIMIT 5` 로 모양을 본다. 그다음 조건을 붙여 좁힌다.
4. **필요한 열만.** `SELECT *` 대신 열을 나열한다. 토큰·비밀번호·개인정보 열(이름에 token, secret, password, key 가 들어간 열 등)은 선택하지 않는다.
5. **조건과 정렬.** 큰 테이블은 시간 범위(`created_at`)나 키로 먼저 좁힌 뒤 `ORDER BY ... LIMIT` 를 쓴다. 인덱스가 없는 열의 넓은 필터는 느리다.
6. **결과를 해석한다.** 건수·시각의 근거를 보고에 쓴다. 결과가 비었으면 "없음" 이 아니라 "이 조건에서 없음" 이라고 쓴다.

## 사내 도구의 보호선 (코드로 막히는 것)
`query_database` 는 다음을 강제한다. 우회하려 하지 않는다.
- `SELECT` 로 시작하는 한 문장만. 세미콜론으로 이은 다중 문장 거부.
- 단어 경계 기준으로 DROP, DELETE, UPDATE, INSERT, ALTER, TRUNCATE, CREATE, GRANT, REVOKE, EXEC 포함 시 거부.
- `LIMIT` 이 없으면 기본 20행(상한 100행)을 붙인다. 더 필요하면 조건으로 나눠 조회한다.
- 문장 실행 시간 상한이 있다(기본 22초, 환경변수로 조정되나 도구 전체 제한보다 짧게 고정된다). 초과하면 `query_database_timeout` 오류가 오니 기간·조건을 줄인다.
- 풀 대기 초과 시 직접 읽기 연결로 한 번 더 시도한다. 반복 실패는 DB 부하 신호이므로 재시도를 쌓지 말고 보고한다.
`query_project_database` 의 `limit` 기본값은 100 이다. 그래도 필요한 행만 가져온다.

## 코드가 막지 않는 것 (스스로 지킨다)
- 막히지 않아도 **한 번에 수천 행을 읽지 않는다.** 집계(`COUNT`, `GROUP BY`)로 답할 수 있으면 집계로 답한다.
- 조회 결과를 그대로 외부에 붙여 넣지 않는다. 민감 값이 보이면 마스킹하고 사실만 전한다.
- 테이블 이름을 런타임에 문자열로 이어 붙이는 질의는 만들지 않는다. 이름은 확인한 것을 그대로 쓴다.
- 조회 결과로 얻은 값을 근거 없이 일반화하지 않는다. 표본과 전체를 구분해 쓴다.

## 쓰기가 필요할 때
이 스킬은 쓰기를 하지 않는다. 쓰기가 필요하다는 결론이 나오면:
1. 쓰려는 SQL, 대상 행 수 예상, 이유를 사용자에게 보고하고 **승인**을 받는다.
2. 실행은 `db_safe_write` 로만 한다: INSERT/UPDATE/DELETE 한 문장, 트랜잭션, 전후 건수 비교, `dry_run` 지원.
3. 먼저 `dry_run` 으로 영향 행 수를 확인한다.
4. `DROP`, `TRUNCATE`, `ALTER`, `CREATE DATABASE` 는 도구가 막으며 어떤 경우에도 시도하지 않는다. 스키마 변경은 마이그레이션 파일과 승인된 창구로 한다.
5. 로그인·테넌트 관련 테이블(saas_users 등)은 추가 보호 대상이다. 보호에 걸리면 우회하지 말고 보고한다.

## 보고 형식
`질의 목적 / 사용한 도구와 DB / 질의(민감 값 제외) / 결과 요약(건수·시각) / 한계(LIMIT 로 잘렸는가, 시간 범위)`.

## 승격 조건
현재 `status: candidate`. 아래를 모두 만족해야 `active` 로 올린다.
1. 이 스킬을 따라 조회한 **사용 증거 2건** — 세션 id 또는 러너 job_id 를 각각 기록한다.
2. 두 건 모두 스키마 확인 → 작은 조회 → 좁힌 조회 순서를 따랐고, 쓰기 질의가 없었다.
3. 위험 등급이 `read` 로 유지된다 (`infer_risk_tier` 확인).
승격은 증거 확인 후 사람이 승인한다. 이 문서는 스스로 승격하지 않는다.

## 출처 및 변경 고지
- 출처: sanjay3290/ai-skills 저장소의 postgres 스킬 (https://github.com/sanjay3290/ai-skills/tree/main/skills/postgres). 라이선스: Apache License 2.0. 라이선스 전문은 해당 저장소에서 확인한다.
- 변경 사실: 원문을 옮기지 않고 방법론(읽기 전용 원칙, 단일 문장 검증, 실행 시간과 결과 행 수 상한, 스키마 먼저 확인, 오류 메시지 노출 주의)을
  이해한 뒤 한국어로 새로 썼다. 사내 도구(`query_database`, `query_project_database`, `db_safe_write`)의 실제 보호선과 승인 절차,
  보고 형식으로 내용을 바꾸었다. 원본의 보조 스크립트와 연결 설정은 가져오지 않았다. 2026-10-28 각색.
