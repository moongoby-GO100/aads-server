# AADS-LLM-M9-COST-BASIS-20260930 — M9 총비용 계측 정본화

## 결론 요약
- **정본 원장**: `oauth_usage_log` (토큰·호출·비용). `task_cost_log`·`cost_tracking` 은 **폐기**.
- **비용 출처 분리**: `cost_source`(relay_reported / catalog_estimated / unknown) + `cost_usd_catalog`(기록 시점 `llm_models` 정가 재계산, DB 함수 `llm_catalog_cost_usd()`).
- **표면 귀속**: `app/services/llm_cost_basis.classify_surface()` 단일 판정기.
- **러너 귀속**: 러너가 Claude/Codex CLI 사용량을 `oauth_usage_log.job_id` 와 함께 기록 → `pipeline_jobs` 조인 가능.
- **API**: `GET /api/v1/ops/llm-cost/per-success?days=7`.

## 1. task_cost_log / cost_tracking 폐기 판단 (코드 참조 근거)
| 원장 | 쓰는 코드 | 호출처 | 판정 |
|---|---|---|---|
| `task_cost_log` | `POST /dashboard/cost-log` (project_dashboard.py) | 저장소·대시보드 전체 grep 0건 | 폐기 |
| `cost_tracking` | `POST /ops/cost` (ops.py), `record_tenant_llm_usage()` (tenant_usage_limits.py) | 둘 다 호출처 0건 | 폐기 |

되살리지 않은 이유: 되살릴 기록 경로가 모두 `oauth_usage_log` 와 같은 호출을 다시 적는 구조라, 합산하면 이중계상이 된다(`tenant_usage_limits` 의 월간 합계가 이미 두 원장을 UNION 한다).

읽는 쪽 조치:
- `GET /ops/cost/summary`, `GET /dashboard/costs`: 기존 필드는 그대로 두고 `source_status: "retired"`, `canonical_source: "oauth_usage_log"`, `canonical_endpoint` 를 추가 → 0/빈 값이 "비용 0" 이 아니라 "폐기된 원장" 임을 응답에 명시.
- `cross_validator.check_cost_tracking`: 폐기된 원장과 조인해 완료 작업 전부를 `no_cost_record` 로 오탐하던 검사를 `COST_TRACKING_LEDGER_ACTIVE=False` 로 비활성(빈 결과가 정상).
- 대시보드 `/ops` 화면(`aads-dashboard/src/app/ops/page.tsx`)은 별도 저장소라 이 작업에서 수정하지 않았다. 참고로 그 화면은 `today_total/cumulative_total/daily` 를 읽는데 API 는 원래부터 그 필드를 주지 않는다(기존 불일치).

## 2. 기존 행 백필 (지시서와 다르게 한 부분 — 사유)
지시서는 "기존 행 전부 relay_reported" 였으나 코드 확인 결과 그렇지 않다:
- `cli_relay` → `event.total_cost_usd` (CLI 자체 보고) → 값 ≠ 0 이면 **relay_reported**, 값 = 0 이면 **unknown, cost_usd=NULL** (옛 코드가 `float(total_cost_usd or 0)` 으로 적어 미보고와 0 보고가 구분되지 않는다 — 라운드 2, 지적 5)
- `codex_relay` → `_estimate_cost(_COST_MAP)` 앱 추정값 → **catalog_estimated**
- `model_selector_sdk` → 보고값/추정값이 행 단위로 구분 불가 → **unknown** (값은 유지)
- `anthropic_client`·`ceo_chat*` → 비용을 넘기지 않아 0.00 → **unknown, cost_usd=NULL**

전부 relay_reported 로 표시하면 앱 추정값이 실보고로 둔갑해 불일치를 감춘다. `cost_usd_catalog` 는 과거 행 전부 NULL(사후 추정 없음).

temp 사본(최근 14일 19,636행)에서 마이그레이션을 돌린 백필 결과:
| call_source | cost_source | 행 | cost 있음 | catalog 있음 |
|---|---|---|---|---|
| anthropic_client | unknown | 11,298 | 0 | 0 |
| cli_relay | relay_reported | 6,054 | 6,054 | 0 |

(위 표는 라운드 0 측정이다. 라운드 2 에서 cli_relay 의 cost_usd=0 행은 unknown/NULL 로 바뀐다 — 2026-09-30 운영 전체 기간 실측 2행, 둘 다 토큰 0.)
| codex_relay | catalog_estimated | 2,259 | 2,259 | 0 |
| model_selector_sdk | unknown | 25 | 25 | 0 |

## 3. 불일치 재현 (claude-opus-5, cli_relay, 최근 7일)
2026-09-30 02:28 KST 실측(지시서 측정 시점과 창이 약 30분 어긋남):

| 항목 | 값 |
|---|---|
| 호출 | 1,108 |
| 기록된 cost_usd 합 (relay_reported) | **$11,391.92** |
| input / output / cache_read / cache_creation 토큰 | 27,308 / 12,073,379 / 3,198,682,355 / 85,635,627 |
| 카탈로그(5/25 per 1M) input+output | $301.97 (배율 37.73×) |
| 카탈로그 + cache_read $0.50/M 가정 | $1,901.31 → **5.99×** |

지시서 수치(11,253.52 / 1,889.62)로 계산하면 **5.955 ≈ 5.96×** 로 같은 결론이 나온다. 어느 쪽이 맞는지는 이 작업에서 판정하지 않았다(`llm_models` 단가는 변경하지 않음). 앞으로 새로 기록되는 행은 `cost_usd`(보고)와 `cost_usd_catalog`(정가)를 둘 다 가지므로 API 의 `reported_to_catalog_ratio` 가 이 배율을 모델별로 계속 드러낸다.

## 4. 러너 귀속 결함과 수정
원인(확인됨): `pipeline-runner.sh` 가 Claude CLI 를 `-p --output-format text` 로 직접 띄웠고, 사용량을 어디에도 기록하지 않았다. `oauth_usage_log` 의 러너 행은 0건이었다(조인 0건의 원인은 session_id 형식이 아니라 행 부재).

수정:
- Claude: `--output-format json` 으로 받아 `scripts/runner_cli_usage.py` 가 modelUsage 의 모델별 토큰·costUSD 를 `call_source='runner_claude_cli'`, `job_id=<job>`, `cost_source='relay_reported'` 로 기록. 출력 파일은 CLI 종료 직후 text 모드와 같은 결과 텍스트로 되돌리고 **검사한다**(원문은 `<out>.usage.json`). 자세한 실패 처리는 6절.
- Codex: exec 출력에 토큰 분해·비용이 없어 **추측하지 않고** `cost_source='unknown'` 행으로 작업 귀속만 기록.
- LiteLLM 러너: OAuth 원장 범위 밖이라 이번에 다루지 않음.

## 5. 검증
- 단위시험 신규 `tests/unit/test_llm_cost_basis.py` 27건 + 관련 기존 시험: 78 passed.
- 전체 `bash scripts/run_unit_tests.sh`(라운드 0 당시): 5032 passed / 39 failed / 11 skipped. 실패 39건은 **HEAD 원본(git archive)에서도 같은 39건이 실패** → 이번 변경과 무관(기존 실패). 최신 수치는 7절.
- ruff F821/F811: 통과. `bash -n pipeline-runner.sh`: 통과.
- 실DB 검증(TEMP 사본 + ROLLBACK, 운영 테이블 무변경): 마이그레이션 실행, `llm_catalog_cost_usd('claude-opus-5',1M,1M)=30.000000`, 미등록 모델 NULL, 러너 INSERT 가 실제 `done` 작업(runner-37ec9785)과 조인됨을 확인.
- **최근 7일 러너 표면 0 아닌 값**: 현재 원장에 러너 행이 0건이므로 배포·마이그레이션 적용 후 러너 작업이 돌기 시작해야 채워진다. 지금 시점에서는 확인하지 못했다(미검증). 성공 작업이 5건 미만인 동안은 `insufficient_sample` 로 응답한다.

## 6. 재작업 라운드 1 (runner-bee81ee4 리뷰 반려 교정)
출발점 `2da6d59c` 의 변경을 origin/main(`3509fd1c`) 위로 옮겼다. `oauth_usage_tracker.log_usage` 는 그 사이 main 에 들어온 `rate_limit_info` 인자와 충돌해 둘 다 살렸다.

| # | 지적 | 처리 |
|---|---|---|
| 1 | 헬퍼가 없으면 JSON 원문이 남는다 | `runner_cli_usage_ready`(헬퍼 파일 + python3) 가 거짓이면 **json 으로 띄우지 않고 text 로 띄운다**. 헬퍼·시험 파일은 원래 커밋 `2da6d59c` 에 있었고(리뷰가 본 diff 가 잘려 있었다) 이번 변경에도 포함된다. |
| 2 | 되돌리기 실패가 삼켜진다 | 되돌리기(`restore_runner_claude_output`)와 기록(`record_runner_cli_usage`)을 분리. 되돌리기는 헬퍼 실패 시 jq 로 한 번 더 시도하고, 끝나고 나서 출력 파일이 여전히 CLI 결과 JSON 이면 `RUNNER_CLI_JSON_RESTORE_FAILED` 를 남기고 **그 시도를 exit_code=1(실패)로 만든다**. 기록 단계는 파일을 읽기만 해서, 실패해도 사용량 한 건을 잃을 뿐이다. `2>/dev/null` 을 없애고 `RUNNER_CLI_USAGE_FAILED`/`_SKIP` 으로 로그를 남긴다. JSON 판정은 파일이 `{"type":"result"` 로 **시작**할 때만 참이고(되돌린 본문이 그 문자열을 인용해도 오판하지 않음), `grep -q` 파이프가 SIGPIPE(141)로 pipefail 에 걸리지 않도록 변수 비교로 한다. 재시도가 같은 출력 파일을 쓰므로 지난 시도의 `.usage.json` 은 되돌리기 전에 지운다. |
| 3 | 시험 파일이 변경 목록에 없다 | `tests/unit/test_llm_cost_basis.py` 가 포함된다. 이번 라운드에 20건을 더했다(기존 1건 이름·범위 변경 포함)(bash 함수 하네스로 헬퍼 정상/헬퍼 크래시→jq/둘 다 실패/text 출력/64KB 초과 JSON pipefail, 스키마 폴백 재확인, NOT NULL, 분모 중복 제거). |
| 4 | cost_usd NOT NULL 여부 | 운영 DB 실측(2026-09-30 02:3x KST): `cost_usd numeric, is_nullable=YES, default 0`, CHECK·트리거 없음, 제약은 PK·tenant FK 뿐. `042_oauth_usage_log.sql` 도 NOT NULL 없음. 그래도 마이그레이션에 `ALTER COLUMN cost_usd DROP NOT NULL`(멱등)을 `SET cost_usd = NULL` 앞에 넣었고 파괴적 SQL 게이트를 통과함을 시험한다. 앱은 `cost_usd` NOT NULL 위반을 재시도 불가로 분류해 버퍼로 되돌리지 않는다(tenant_id 사고의 무한 재시도 방지). |
| 5 | 폴백 플래그가 영구히 꺼진다 / 함수만 없을 때 cost_source 까지 잃는다 | 영구 bool 을 "이 시각까지 없다고 본다" 두 개(열 / 정가 함수)로 바꿨다. 300초 뒤 다시 새 열로 시도하므로 운영 중 마이그레이션이 재시작 없이 반영된다. 정가 함수·`cost_usd_catalog` 만 없으면 `cost_source`·`job_id` 는 유지하고 `cost_usd_catalog` 만 빼고 적는다. |
| 6 | `total_cost` 미정의 가능성 | 지적은 사실과 다르다. `total_cost = 0.0` 이 `_run_agent_sdk_with_key` 머리(`model_selector.py` 4870행 부근)에서 초기화되고, `cost` 계산도 같은 변수를 이미 쓰고 있었다. 판정을 `_sdk_cost_reported = bool(total_cost)` 하나로 묶어 비용 값과 출처가 같은 기준을 쓰게 했고, 시험이 초기화 순서와 공통 판정을 보증한다. |
| 7 | `pipeline-runner.sh.local` 범위 | HEAD 에서 `.sh` 와 **바이트 동일**한 사본이다(`cmp` 확인, 과거 커밋들도 둘을 같이 고쳤다). 동일 사본으로 유지하고 시험으로 고정했다. |
| 8 | 모델별 분모 중복 / 표면 작업당 비용 없음 | 분모를 표면×비용출처 단위에서 job_id 로 중복 제거한 성공 작업 수로 바꿨다. `by_surface` 에 `jobs`·`success_jobs`·`cost_per_success_usd`·`cost_per_success_status`·`catalog_cost_per_success_usd` 를 추가 — M9 의 "성공 작업당 실제 비용" 은 이 값이다. 모델 행의 `cost_per_success_usd` 는 같은 분모로 나눈 모델 몫(`cost_per_success_scope=model_share_of_surface`)이라 더하면 표면 값과 같다. 모델을 실제로 쓴 성공 작업 수는 `success_jobs_using_model` 로 따로 준다. |

라운드 1 검증:
- `bash scripts/run_unit_tests.sh tests/unit/test_llm_cost_basis.py test_pipeline_runner_script_guards.py test_llm_metric_integrity.py test_llm_response_metrics.py test_dup_guard.py`: **133 passed**.
- 전체 `bash scripts/run_unit_tests.sh`: 5092 passed / 36 failed / 11 skipped. 실패 36건을 HEAD(`3509fd1c`) `git archive` 사본에서 그대로 돌리면 **36건 모두 같이 실패**한다 — 이번 변경에서 새로 생긴 실패는 0건(sandbox·yeoljeong·governance 등 이번 변경과 무관한 기존 실패).
- ruff F821/F811 통과, `bash -n` 두 러너 스크립트 통과, `scripts/dup_guard.py` 통과, 마이그레이션이 `apply_release_migrations.sh` 파괴적 SQL 게이트 통과.
- 최근 7일 러너 표면 0 아닌 값: 여전히 **미검증**이다. 러너 행은 배포·마이그레이션 적용 후 러너 작업이 돌아야 생긴다.

불일치 재측정(2026-09-30 02:41 KST, cli_relay claude-opus-5 7일, `llm_models` 5/25): 1,110호출, 보고 $11,414.11, 카탈로그 입출력 $302.97, cache_read $0.50/M 가정 $1,907.10 → **5.985×**. 지시서 수치로는 11,253.52 / 1,889.62 = **5.955 ≈ 5.96×**.

## 7. 재작업 라운드 2 (runner-b73db42f 리뷰 반려 교정)
출발점 `f87a6408` 의 변경(`.runner_full_diff.patch` 제외)을 origin/main(`9e17d697`) 위로 옮겼다. 충돌 없음.

| # | 지적 | 처리 |
|---|---|---|
| 1 | job_id 를 쓰는 코드가 diff 에 없다 | **지적이 틀렸다(diff 가 잘려 보였다).** 러너 기록 경로는 이번 변경에 들어 있다: 신규 `scripts/runner_cli_usage.py`(`build_insert_sql` 이 `job_id`·`cost_source`·`llm_catalog_cost_usd()` 를 INSERT), `scripts/pipeline-runner.sh`(+`.local` 동일 사본)의 `record_runner_cli_usage` 가 매 시도 뒤 `--job-id "$job_id"` 로 호출해 `db_update` 로 실행한다. 앱 `log_usage(job_id=...)` 는 러너가 쓰지 않는다 — 러너는 앱 밖(호스트 셸)에서 CLI 를 띄우므로 앱 함수를 거치지 않는다. 시험: `test_runner_claude_json_restores_text_and_attributes_job`(job_id 칸·tenant 조회), `test_runner_codex_row_is_attributed_without_guessing_cost`, `test_runner_sql_escapes_quotes`, `test_runner_success_denominator_counts_done_jobs_only`, `test_runner_sql_joins_pipeline_jobs_by_job_id`, `test_runner_script_restores_and_records_usage_before_output_is_read`(스크립트 호출 순서), bash 하네스 `test_runner_restore_*`(헬퍼 정상/크래시→jq/둘 다 실패). |
| 2 | 러너 기록기의 근거·커밋 SHA | 이 기록기는 라운드 0 `2da6d59c`, 라운드 1 `f87a6408` 에서 만들었고 **둘 다 main 에 들어가지 않았다**(`git merge-base --is-ancestor f87a6408 origin/main` → 아님, `origin/main` 에 `scripts/runner_cli_usage.py` 없음). 그래서 이번 변경 파일 목록에 신규 파일로 다시 들어간다. 근거는 4절(러너가 `-p --output-format text` 로 CLI 를 띄우고 아무것도 기록하지 않았다)이다. |
| 3 | 정가 함수가 참조하는 `llm_models` 열 | 열은 모두 있다: `migrations/053_llm_model_registry.sql`(id SERIAL, provider, model_id, input_cost, output_cost, is_active), 운영 DB `information_schema` 실측(2026-09-30 03:0x KST) 6열 모두 존재. 함수 본문을 `SET LOCAL check_function_bodies=on` 으로 `pg_temp` 에 컴파일해 성공(트랜잭션 ROLLBACK, `oauth_usage_log` 에 잠금 없음), `('claude-opus-5', 27120, 11981731)` → **299.678875**(지시서 $299.68 재현). 런타임에 열이 사라지는 경우도 막았다: 정가 함수를 넣은 시도에서 난 42703/42883/42P01 은 문구와 무관하게 catalog 부재로 보고 정가 함수만 빼 재시도한다 — `column m.provider does not exist` 가 버퍼로 되돌아가 무한 재시도되지 않는다(`test_catalog_function_body_column_missing_falls_back_not_requeued`). |
| 4 | 문자열 매칭으로 스키마 부재 판정 | `_cost_basis_schema_gap(exc, *, with_catalog)` 를 **SQLSTATE** 로 바꿨다(42703 undefined_column / 42883 undefined_function / 42P01 undefined_table). 어느 부분이 없는지는 문구가 아니라 어느 시도에서 났는가(정가 함수 포함 여부)로 정한다. `_is_cost_usd_not_null_violation` 도 SQLSTATE 23502 + 서버가 주는 `column_name == "cost_usd"` 로 바꿨다. 문구 비교는 남아 있지 않다. 시험: 한국어 문구+42703 → columns, 영문 "does not exist" 인데 SQLSTATE 없음 → None, 실제 `asyncpg.exceptions` 4종. |
| 5 | cli_relay 의 0.00 을 relay_reported 로 백필 | relay_reported 백필에 `cost_usd <> 0` 을 더했다. 0.00 행은 unknown 으로 남고 `DROP NOT NULL` 뒤 `cost_usd = NULL` 로 적는다. down 은 cli_relay 의 NULL 을 0 으로 되돌린다. 운영 실측: cli_relay 0.00 행 2건(둘 다 토큰 0, 오류 없음). codex_relay 0.00 77건은 앱이 `_COST_MAP` 로 **실제 계산한** 추정값(토큰 0 → 0)이라 catalog_estimated 가 사실이므로 그대로 둔다. |
| 6 | 상태값에 `COST_SOURCE_UNKNOWN` 을 쓴다 | `PER_SUCCESS_COST_UNKNOWN = "cost_unknown"` 을 새로 두고 `PER_SUCCESS_STATUSES` 로 묶었다. 시험이 두 이름공간이 겹치지 않음을 보증한다. |
| 7 | `_is_missing_cost_basis_schema` 가 죽은 코드 | 삭제했다(호출처 0건, grep 확인). 시험이 다시 생기지 않음을 확인한다. |
| 8 | `str(e)` 를 HTTP 응답에 싣는다 | 새 엔드포인트는 고정 문구 `"llm cost per-success query failed"` 만 돌려주고 원문은 로그(`ops_llm_cost_per_success_error`)에만 남긴다. 시험: 테이블·열 이름이 detail 에 없음. 기존 엔드포인트들의 같은 패턴은 범위 밖이라 건드리지 않았다. |

라운드 2 검증:
- `bash scripts/run_unit_tests.sh tests/unit/test_llm_cost_basis.py tests/unit/test_pipeline_runner_script_guards.py`: **84 passed**.
- 전체 `bash scripts/run_unit_tests.sh`: **5139 passed / 36 failed / 11 skipped**. 실패 36건을 HEAD(`9e17d697`) `git archive` 사본에서 같은 파일로 돌리면 **36건 모두 같이 실패**한다 — 새로 생긴 실패 0건.
- ruff `F821,F811` 통과, `python3 -m compileall` 통과, `bash -n scripts/pipeline-runner.sh` 통과, `scripts/dup_guard.py` rc=0.
- 불일치 재측정(2026-09-30 03:11 KST, cli_relay claude-opus-5 7일): 1,115호출, 보고 **$11,496.90**, 토큰 input 27,460 / output 12,167,331 / cache_read 3,223,510,085, 카탈로그 입출력 $304.32, cache_read $0.50/M 가정 $1,916.08 → **6.000×**. 지시서 수치 11,253.52 / 1,889.62 = **5.955 ≈ 5.96×**.
- 최근 7일 러너 표면 0 아닌 값: **여전히 미검증.** 운영 원장에 러너 행(`runner_*_cli` / job_id 있음)이 0건이다 — 기록 경로가 아직 배포되지 않았기 때문이다. 마이그레이션 적용과 러너 스크립트 반영 뒤 성공 작업이 5건 이상 쌓여야 `ok` 값이 나온다.
- 비용 원장이 "기록 경로가 살아 있는가" 를 코드로 확인하지 않으면 테이블만 남아 0 을 정상값처럼 돌려준다. 폐기한 원장은 응답에 `retired` 로 표시해야 한다.
- 출처가 다른 비용(자체 보고 / 앱 추정 / 미측정)을 한 컬럼에 라벨 없이 쌓으면 불일치가 합계 속에 묻힌다.
- 출력 형식을 바꿔 부가 정보를 얻는 경우, 되돌리기는 "부가 작업" 이 아니라 본 경로의 일부다. 되돌리기와 부가 기록을 같은 함수에서 같은 오류 처리로 묶으면 부가 기록의 "실패해도 무시" 가 본 경로로 번진다.
- 러너처럼 앱 밖에서 LLM 을 띄우는 경로는 앱의 계측 훅을 통과하지 않는다. 새 실행 경로를 만들 때 사용량 기록을 같이 만들어야 한다.
