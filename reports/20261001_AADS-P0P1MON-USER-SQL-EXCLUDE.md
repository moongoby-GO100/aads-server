# AADS-P0P1MON-USER-SQL-EXCLUDE — p0p1 모니터에서 사용자 SQL 오류 제외

## 변경 파일
| 파일 | 분류 | 내용 |
|---|---|---|
| `app/api/ceo_chat_tools_db.py` | 수정 + 신규 헬퍼 | `_USER_SQL_ERROR_SIGNATURES`, `_is_user_sql_error()` 추가. `query_project_database` / `query_acct_database` 의 `except Exception` 로그만 분기(사용자 SQL 오류 → `logger.warning("... USER_SQL_ERROR | ...")`, 그 외 → 기존 `logger.error("... FAIL | ...")`). 반환 dict·마스킹·TIMEOUT 경로 불변 |
| `deploy.sh` (Phase 7) | 수정 | `MONITOR_EXCLUDE_PATTERN` 추가, 제외를 `tail -20` 앞에 적용, 제외 건수 echo + `suppressed_user_sql=` 관측. `MONITOR_PATTERN` 기본값·300초 하한·health 분기 불변 |
| `tests/unit/test_deploy_observability.py` | 테스트 추가 | 신규 2건(기존 테스트 무변경) |
| `tests/unit/test_ceo_chat_tools_db_user_sql_error.py` | 신규 | 헬퍼 판정 + caplog 로 로그 레벨/문구, 반환 dict 회귀 |

STEP 0 분류: 유지 — MONITOR_PATTERN, TIMEOUT 경로, 반환 dict, ACCT 마스킹, 기존 테스트 전부. 수정 — 위 두 except 블록, Phase 7 루프. 신규 — 헬퍼, 제외 패턴, 테스트. 삭제 — 없음.

## 판정 로직 (`_is_user_sql_error`)
1. `type(exc).__name__` 이 UndefinedTable/UndefinedColumn/UndefinedFunction/SyntaxOrAccess/InsufficientPrivilege/PostgresSyntax → True (asyncpg import 없음)
2. `exc.sqlstate` 가 있으면 그것이 최종 판정: `42*` 만 True. 08(연결)·53(자원)·3D000(DB 없음) 등은 메시지가 비슷해도 False
3. `args[0]` 이 int 면 MySQL errno: {1146,1054,1064,1142,1045} 만 True. 2003 등 연결 오류와 `ConnectionRefusedError(111)` 은 False
4. 그 외는 문자열 폴백(소문자). 단 `database/role/user/extension ... does not exist` 는 SQL 오타가 아니라 접속 대상 문제이므로 제외

지시서 대비 의도적 보강은 2·3·4의 "명확한 신호가 있으면 문자열보다 우선"과 database/role 제외 둘이다. 지시서 문자열 목록 그대로면 `database "x" does not exist`(접속 장애)까지 릴리스 감시에서 사라진다.

## 검증 결과 (원문)
```
$ bash scripts/run_unit_tests.sh tests/unit/test_deploy_observability.py
.....................                                                    [100%]
21 passed in 0.82s
$ bash scripts/run_unit_tests.sh tests/unit/test_ceo_chat_tools_db_user_sql_error.py
.......................                                                  [100%]
23 passed in 0.40s
$ bash scripts/run_unit_tests.sh tests/unit/test_tools_and_pipeline.py tests/unit/test_tool_executor_aliases.py tests/unit/test_db_safe_write_project_routing.py
116 passed, 1 warning in 9.51s
$ bash scripts/run_unit_tests.sh tests/unit/test_deploy_observability.py tests/unit/test_ceo_chat_tools_db_user_sql_error.py tests/unit/test_project_database_config.py
46 passed in 1.00s
$ bash -n deploy.sh                      -> bash -n OK
$ bash scripts/verify-bluegreen-release-contract.sh -> [release-contract] PASS
$ python3 -c "import ast;ast.parse(open('app/api/ceo_chat_tools_db.py').read())" -> OK
$ ruff check --select F821,F811 (변경 3파일) -> All checks passed!
```
`set -Eeuo pipefail`(deploy.sh:11) 하에서 새 파이프라인은 모두 `|| true` 로 감싸져 grep 의 exit 1 이 스크립트를 죽이지 않는다. `MONITOR_RAW` 가 비면 `printf '%s\n' ""` 의 빈 줄이 `grep -E "$MONITOR_PATTERN"` 에서 걸러져 `MONITOR_HITS` 는 빈 문자열(테스트 `hits("") == ""` 로 확인).

## deploy_runs#5440 재현 로그 한 줄 확인
```
old(FAIL 문구, error 레벨):          제외 패턴 통과 → 패턴 hit=1  (여전히 릴리스 실패 처리)
new(USER_SQL_ERROR 토큰 + error 레벨 가정): 제외 패턴 적용 후 hit=0 (제외됨)
```
- 실제 새 코드는 해당 기록을 warning 으로 내리므로 `MONITOR_PATTERN` 자체에 걸리지 않는다(1선). 제외 패턴은 2선이다.
- 즉 **FAIL 문구의 구 로그 형식은 제외되지 않는다.** 이는 의도다 — 제외 키는 `USER_SQL_ERROR` 토큰뿐이며, 수정 이전 이미지가 찍은 로그는 구제하지 않는다. 이 수정은 새 이미지로 릴리스된 뒤부터 효력이 있다.
- `test_p0p1_monitor_exclude_drops_only_user_sql_error_token` 가 실제 grep 서브프로세스로 검증: 토큰 포함 error 줄 제외 / `background task escaped` error 와 `Traceback` 유지 / 제외 대상 30줄 + 진짜 오류 1줄에서 진짜 오류가 tail 에 밀리지 않음.

## 남은 리스크
- **TIMEOUT 경로(query_project_database 962·985, query_acct_database 1141)는 여전히 error → 릴리스를 막는다.** 지시대로 불변. 느린 쿼리 하나가 남의 릴리스를 죽일 수 있는 동일 유형의 위험이 남아 있다.
- **MySQL errno 1045(Access denied)를 사용자 오류로 분류했다(지시 사항).** 1045 는 쿼리 문제가 아니라 접속 자격 증명 실패일 수 있어, 자격 증명이 깨진 상태가 릴리스 감시에서 사라질 수 있다. 정책 재검토 권장.
- 문자열 폴백은 사용자 SQL 이 아닌 오류가 우연히 "does not exist"/"doesn't exist" 를 담으면 오분류할 수 있다(예: 파일 없음 메시지). sqlstate/errno 가 없는 예외에 한한다.
- 제외 건수 echo 는 30초 루프마다 누적 건수로 반복 출력된다(기능 영향 없음, 로그만 길어짐).
- 이 변경은 이미지 리빌드가 있어야 앱 쪽 효력이 생기며, deploy.sh 는 다음 릴리스부터 적용된다.
