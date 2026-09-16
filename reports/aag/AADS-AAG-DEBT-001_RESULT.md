# AADS-AAG-DEBT-001 — AAG ROUTE_MISSING 상환 1차

대시보드가 부르는데 백엔드에 없던 `/api/v1/ops` GET 6개를 `app/api/ops.py` 에
**추가만** 했다. 기존 라우트·모델·미들웨어·마운트는 한 줄도 건드리지 않았다.

## STEP 0 — 기존 구현 조사 및 분류

`app/api/ops.py` 는 수정 전 3,008행 / 라우트 66개(`@router.*` 데코레이터 69개,
`/ops/codex-usage` 중복 1건 포함)였다. 분류:

| 항목 | 분류 | 근거 |
|---|---|---|
| 기존 `/ops/*` 라우트 66개 전부 | **유지** | 한 줄도 수정하지 않음. `git diff` 는 파일 끝 추가분과 신규 상수/헬퍼뿐 |
| `@router.get("/ops/cost/summary")`(1007행) | **유지** | 인증·`_get_conn()`·예외→500 관례를 신규 6개가 그대로 따름 |
| `@router.get("/ops/health-check")`(1300행) | **유지 + 재사용** | `/ops/status` 가 이 함수를 **호출**해 결과만 접는다. 상태 계산을 다시 만들지 않음 |
| `@router.get("/ops/pipeline-status")`(2293행) | **유지** | 응답 관례 참조만 |
| `app/main.py:3575` `include_router(ops_router, prefix="/api/v1")` | **유지** | 마운트 추가 없음 (DOUBLE_MOUNT 9건 불변 확인) |
| `_get_conn()`, `KST`, `normalize_project_label`, `get_server_config` | **유지(재사용)** | 신규 코드가 그대로 사용 |
| `app/services/circuit_breaker.get_all_states`, `FAILURE_THRESHOLD` | **유지(재사용)** | `/ops/status` 서킷 요약 |
| `app/services/server_registry.CANONICAL_SERVER_IDS` | **유지(재사용)** | 서버 카드 3장. 별칭 중복 집계 방지를 위해 레지스트리 직접 순회 안 함 |
| 신규 GET 6개 + 순수 헬퍼 15개 + 상수 6개 | **신규** | 파일 끝에 한 블록으로 추가 |
| `tests/unit/test_ops_dashboard_endpoints.py` | **신규** | 38건 |
| `tools/aag/baseline_aads.json`, `reports/aag/*` | **갱신** | 지시서 5항 (`--write-baseline`). 스캐너가 자동 기록 |
| **삭제** | **없음** | 삭제한 함수·라우트·테이블 접점 0건 |

지시서에 없던 파일 변경 사유:
- `tools/aag/baseline_aads.json`, `reports/aag/aads-findings.md`, `aads-graph.json`,
  `aads-arch.mmd` — 지시서 5항이 요구한 `--write-baseline` 의 산출물이다.
  스캐너가 같은 실행에서 함께 기록한다. `tools/aag/scan_aads.py`·`rules_aads.yml`·
  `scripts/hooks/**`·`scripts/aads-aag-*` 는 **미접촉**(7항).

## 1. 신규 엔드포인트

| 경로 | 메서드 | 출처 테이블 | 파라미터(클램프) | 호출처 |
|---|---|---|---|---|
| `/api/v1/ops/cost-trend` | GET | `cost_tracking` | `days` 7 (1~90), `project` | `ArtifactChart.tsx:139` |
| `/api/v1/ops/project-stats` | GET | `pipeline_jobs` ∪ `pipeline_jobs_archive` | `days` 30 (1~365) | `ArtifactChart.tsx:140` |
| `/api/v1/ops/status` | GET | 없음 — `health_check()` + `circuit_breaker_state` 재사용 | 없음 | `ArtifactDashboard.tsx:100` |
| `/api/v1/ops/pipeline-history` | GET | `pipeline_jobs` ∪ `pipeline_jobs_archive` | `limit` 10 (1~100), `project` | `ArtifactDashboard.tsx:101` |
| `/api/v1/ops/qa-results` | GET | `code_reviews` | `limit` 20 (1~200), `project` | `app/ops/page.tsx:541` ← `lib/api.ts:475` |
| `/api/v1/ops/design-reviews` | GET | `design_reviews` (+`pipeline_jobs`/archive LEFT JOIN 으로 project 보강) | `limit` 10 (1~100) | `app/ops/page.tsx:542` ← `lib/api.ts:476` |

6개 모두 `{"items": [...], ...}` 형태다 — 호출부의
`Array.isArray(d) ? d : d.items || []` (ArtifactChart.tsx:144) 에 맞췄다.
인증은 기존 관례대로 라우트 단 `Depends` 없이 전역 미들웨어에 맡긴다.

### 뺀 엔드포인트: 없음 (6/6 구현)

### 출처 테이블을 지시서와 다르게 잡은 2건 — 확인한 스키마 근거

**(1) qa-results: `design_qa_scores` → `code_reviews`**

`design_qa_scores` 실제 스키마(2026-09-16 운영 DB 직접 조회):

```
request_id uuid | scoring_version | total_score | request_match_score
| context_retention_score | visual_completeness_score | responsive_stability_score
| accessibility_score | technical_stability_score | score_details jsonb
| token_compliance jsonb | evidence jsonb | created_at | updated_at      → 0행
```

화면 계약(`app/ops/page.tsx:88` `QaResultItem`)은
`task_id / project / verdict / retry_count / detail / created_at` 인데
`design_qa_scores` 에는 **task_id·project·verdict 어느 것도 없다**. 키는
`request_id`(디자인 수정요청 uuid)이고 행도 0건이다.
`app/services/design_qa_scorer.py:726` 이 유일한 기록자로, 디자인 수정요청
점수표이지 파이프라인 QA 판정이 아니다.

계약이 실제로 맞는 테이블은 `code_reviews` (migrations/023_ai_feedback.sql, 16,501행):
`job_id / project / verdict / score / feedback jsonb / review_cycle / needs_retry
/ flag_category / created_at`. verdict 실측 분포는
`APPROVE 12,287 · FLAG 2,521 · REQUEST_CHANGES 1,694` 이고, 화면은
`item.verdict === "PASS"` 만 통과로 그리므로 API 에서 `PASS`/`FAIL` 로 정규화하고
원본을 `raw_verdict` 로 함께 보낸다. `retry_count = max(review_cycle-1, 0)`.

**(2) project-stats / pipeline-history: `pipeline_jobs` → `pipeline_jobs` ∪ `pipeline_jobs_archive`**

`pipeline_jobs` 는 큐 테이블이고 현재 10행(2026-09-16 11:47~15:34, 상태는
error 6 / cancelled 3 / running 1 — **done 0건**)뿐이다. 끝난 작업은
`pipeline_jobs_archive`(739행, 2026-04-03~) 로 옮겨진다.
`migrations/20260915_pipeline_jobs_archive.sql` 머리말이 그 이유를 직접 적고 있다:

> "프로젝트별 성공률·소요시간·비용을 pipeline_jobs 로 산출할 방법이 없다"

큐만 세면 `completed` 가 **구조적으로 0** 이 되어 완료율 차트가 항상 0% 를
가리킨다. 두 테이블을 `job_id` 로 중복 제거(`DISTINCT ON (job_id)`, 큐 우선)해
합쳐 본다. 아카이브는 46컬럼 드리프트를 피하려고 `row_data jsonb` 를 쓰므로
`instruction`/`phase`/`completed_at` 은 `row_data->>'...'` 로 꺼낸다
(값이 ISO8601 문자열임을 실측 확인, `NULLIF(...,'')::timestamptz` 캐스트 성공).

두 테이블 모두 이미 다른 코드가 참조 중이라 `TABLE_NO_MODEL` 이 늘지 않는다
(스캔 후 44 불변 확인). `code_reviews`/`design_reviews` 는 각각
`migrations/023_ai_feedback.sql`, `migrations/019_design_reviews.sql` 에
`CREATE TABLE` 이 있어 역시 늘지 않는다.

SQL 3종(project-stats / pipeline-history / design-reviews 조인)은 운영 DB에
직접 실행해 결과를 확인했다. cost-trend 경계식도 `days=200` 으로 3월 데이터가
나오는 것까지 확인했다.

## 2. `scan_aads.py --check-baseline` 실행 출력 전문

**적용 전**
```
[AAG] root=/tmp/aads-wt-runner-24de409a
[AAG] 노드 757 · 엣지 1148 · 앱 파이썬 381개 · 라우터 모듈 81개 · 프런트 232개
[AAG] 마운트 라우트 874 · 네임스페이스 82 · 프런트 호출(해석) 337 · 테이블 218
[AAG]   DUP_MODULE          1
[AAG]   DOUBLE_MOUNT        9
[AAG]   ORPHAN_ROUTER       1
[AAG]   TABLE_NO_MODEL     44
[AAG]   PATH_DRIFT          1
[AAG]   ROUTE_MISSING      12
[AAG]   STALE_BACKUP       34
[AAG] 결함 합계 102 · UNRESOLVED 110 (결함 아님)
[AAG] 고정선 대비 증가 없음
```

**적용 후**
```
[AAG] root=/tmp/aads-wt-runner-24de409a
[AAG] 노드 758 · 엣지 1151 · 앱 파이썬 381개 · 라우터 모듈 81개 · 프런트 232개
[AAG] 마운트 라우트 880 · 네임스페이스 82 · 프런트 호출(해석) 337 · 테이블 219
[AAG]   DUP_MODULE          1
[AAG]   DOUBLE_MOUNT        9
[AAG]   ORPHAN_ROUTER       1
[AAG]   TABLE_NO_MODEL     44
[AAG]   PATH_DRIFT          1
[AAG]   ROUTE_MISSING       6
[AAG]   STALE_BACKUP       34
[AAG] 결함 합계 96 · UNRESOLVED 110 (결함 아님)
[AAG] 고정선 대비 증가 없음
```
종료코드 0.

**ROUTE_MISSING 12 → 6.** 마운트 라우트 874 → 880 (+6, 정확히 신규분).
나머지 규칙은 전부 불변 — DOUBLE_MOUNT 9 유지(마운트 미추가 확인),
TABLE_NO_MODEL 44 유지(없는 테이블 SELECT 없음 확인).

남은 6건은 전부 kakaobot 계열로, 이번 범위 밖이다:

```
- [P0] .../app/kakaobot/history/page.tsx:50  GET  /api/v1/kakao-bot/history
- [P0] .../app/kakaobot/history/page.tsx:51  GET  /api/v1/kakao-bot/history/stats
- [P0] .../app/kakaobot/page.tsx:39          GET  /api/v1/kakao-bot/stats
- [P0] .../app/kakaobot/scheduled/page.tsx:51 POST /api/v1/kakao-bot/scheduled/{}/cancel
- [P0] .../app/kakaobot/settings/page.tsx:67 GET  /api/v1/kakao-bot/settings
- [P0] .../app/kakaobot/settings/page.tsx:81 PUT  /api/v1/kakao-bot/settings
```

## 3. 단위테스트

```
$ bash scripts/run_unit_tests.sh tests/unit/test_ops_dashboard_endpoints.py
...................................... [100%]
38 passed in 1.41s          (종료코드 0)
```

회귀 확인 — 관련 기존 테스트 동반 실행:
```
$ bash scripts/run_unit_tests.sh tests/unit/test_ops_dashboard_endpoints.py \
    tests/unit/test_ops_codex_usage.py tests/unit/test_api_health.py \
    tests/unit/test_aag_scan_aads.py tests/unit/test_aag_behavior_check.py \
    tests/unit/test_dup_guard.py
154 passed, 5 warnings in 20.78s   (종료코드 0)
```
`test_api_health.py` 는 TestClient 로 앱 전체를 올리므로 라우트 등록·import 도
함께 검증된다.

테스트 38건은 전부 DB 없이 도는 순수 함수 대상이다 —
클램프(`_clamp_int`, 가비지 입력 포함 11케이스), 직렬화
(`_cost_trend_items` 빈 날 0 채움 / `_project_stat_items` 상태 버킷 /
`_pipeline_title` 한 줄·줄바꿈 두 형태 / `_qa_result_items` verdict 정규화 /
`_design_review_items` / `_ops_status_payload`·`_worst_circuit`), 그리고
6경로가 정확히 한 번씩 등록됐는지 보는 라우트 계약 테스트 1건.

## 4. baseline before/after

`tools/aag/baseline_aads.json`

| 키 | before | after |
|---|---|---|
| `findings_by_rule.ROUTE_MISSING` | **12** | **6** |
| `findings_by_rule.STALE_BACKUP` | 37 | 34 |
| `findings_total` | 105 | 96 |
| `scope.mounted_routes` | 874 | 880 |
| `scope.sql_tables_referenced` | 218 | 219 |
| `scope.graph_nodes` / `graph_edges` | 757 / 1148 | 758 / 1151 |
| `generated_at` | 2026-09-16T14:16:46+09:00 | 2026-09-16T15:44:33+09:00 |
| DUP_MODULE / DOUBLE_MOUNT / ORPHAN_ROUTER / TABLE_NO_MODEL / PATH_DRIFT | 1 / 9 / 1 / 44 / 1 | 변동 없음 |

STALE_BACKUP 37→34 는 이번 작업과 무관하다. 고정선 파일이 14:16 기준으로
3건 뒤처져 있었고(적용 전 스캔도 이미 34), 줄어든 쪽이므로 함께 잠갔다.

## 변경 파일

```
 M app/api/ops.py                          (+627행 / -0행, 기존 3,008행 미수정)
 M tools/aag/baseline_aads.json            (--write-baseline 산출)
 M reports/aag/aads-findings.md            (동상)
 M reports/aag/aads-graph.json             (동상)
 M reports/aag/aads-arch.mmd               (동상)
?? tests/unit/test_ops_dashboard_endpoints.py  (신규, 38건)
```

## 별건 — 이번 작업이 고치지 않았고, 화면에 그대로 드러날 것

세 가지를 조사 중 확인했다. 추측이 아니라 실측이고, 이번 범위 밖이라 손대지 않았다.

1. **`cost_tracking` 은 2026-03-11 이후 기록이 없다** (220행, 마지막
   `recorded_at` 2026-03-11 15:11 KST. `task_cost_log` 는 2026-03-05 에서 멈춤).
   `/ops/cost-trend` 는 SQL·경계식이 맞는데도 최근 7일이 전부 0 으로 나온다.
   기록자는 `app/api/ops.py:995`(POST `/ops/cost`)와
   `app/services/tenant_usage_limits.py:485` 두 곳이다. 기존
   `/ops/cost/summary` 도 같은 테이블을 보므로 /ops 비용 패널이 이미 같은
   상태였다 — 이번에 새로 생긴 문제가 아니다. **원인 미확인이라 오류 사전에
   넣지 않았다**(R-ERRBOOK 1: 추측은 넣지 않는다).

2. **`design_reviews` 는 0행이고 저장소 전체에 기록자가 없다.**
   `grep -rn design_reviews --include=*.py` 결과가 마이그레이션
   (`019_design_reviews.sql`) 뿐이다. 엔드포인트는 계약대로 동작하지만 화면은
   "데이터 없음" 으로 뜬다. 조용히 빈 화면(현 상태)보다는 낫다고 보고 구현했다.
   `after_path` 가 파일시스템 경로면 `screenshot_url` **키 자체를 빼서** 내보낸다
   — 호출부가 truthy 검사로 `<img>` 를 그리므로 넣으면 깨진 이미지가 뜬다.

3. **AAG 게이트가 러너 워크트리에서 그냥은 못 돈다.**
   `tools/aag/rules_aads.yml` 의 `frontend.roots: ../aads-dashboard/src` 는
   저장소 루트 상대 경로다. 워크트리가 `/tmp/aads-wt-runner-*` 에 있으면
   `/tmp/aads-dashboard` 를 찾다 실패하고 스캐너가 **종료코드 2("점검 못함")** 를
   낸다. `scripts/hooks/pre-commit:431` 이 rc≠0 을 커밋 차단으로 읽으므로,
   `app/api/` 를 건드린 워크트리 커밋은 이 상태에서 전부 막힌다.
   이번에는 `/tmp/aads-dashboard → /root/aads/aads-dashboard` 심링크를 만들어
   돌렸고, **커밋이 통과하도록 그대로 남겨 뒀다**. 저장소 밖 읽기 전용 심링크다.
   근본 해결은 규칙 파일이 대시보드 경로를 절대경로나 환경변수로 받게 하는
   것인데, 7항이 `tools/aag/**` 미접촉을 요구하므로 손대지 않았다.
