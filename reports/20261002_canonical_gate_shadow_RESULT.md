# 정본 강제 게이트 그림자 모드 — RESULT (AADS-CANONICAL-GATE-SHADOW-20261002 / R2 교정: AADS-CANONICAL-GATE-SHADOW-PRESERVE-R2-20261003)

> R2 교정 요약(원 runner-58991158 이 PRESERVATION_HARD_GATE 로 반려된 건): 아래 "R2 — 보존 검수 지적 교정" 절 참조.
> 코드 변경은 보존 커밋 `989e7360` 을 최신 origin/main(`a7e3d1f8`)에 선별 복구하고 3가지만 고쳤다 — ① `@router.post` 데코레이터 줄 원문 복원 ② 300ms 를 판정+기록 합산 상한으로 ③ 배치 게이트 동시 실행.

차단은 어디에도 없다. 세 진입점에서 감지·경고·기록만 하고 항상 통과한다(fail-open). 차단 전환은 1주 실측 후 CEO 별도 승인.

## STEP 0 — 기존 구현 분류

| 대상 | 분류 | 내용 |
|---|---|---|
| `app/routers/goals.py` `add_goal_document` | 수정 | 트랜잭션 **밖**에서 게이트 호출, 경고가 있을 때만 응답에 `canonical_gate` 추가. INSERT·락·응답 기존 필드 불변 |
| `app/api/pipeline_runner.py` `JobSubmitResponse` | 수정 | 선택 필드 `canonical_gate` 추가 + `@model_serializer(mode="wrap")` — 경고가 없으면 키 자체를 응답에서 뺀다(기존 응답 shape 불변). **`submit_job` 데코레이터는 원문 그대로 둔다**(R2) |
| `app/api/pipeline_runner.py` `submit_job` | 수정 | 목표 연결이 확정된(`goal_link`) 제출만 판정. 예외 경로에서 `goal_link=None` 로 정리 |
| `app/api/pipeline_runner.py` `submit_batch` | 수정 | 목표 연결이 확정된 잡을 모아 **커밋 후** 판정, 경고는 잡별 `results[*].canonical_gate` |
| `scripts/hooks/pre-commit` | 수정 | 기존 Step·차단 순서 불변. 서명 기록 바로 앞(모든 차단 게이트 통과 후)에 `|| true` 블록 1개 추가 |
| `app/services/canonical_gate.py` | 신규 | 공통 판정·기록 모듈 |
| `scripts/canonical_gate_commit.py` | 신규 | hook 진입점(stdlib만, DB 비접근, 항상 exit 0) |
| `migrations/20261002_canonical_gate_events.sql` + rollback | 신규 | 적용 안 함 |
| `tests/unit/test_canonical_gate_shadow.py` | 신규 | 아래 검증 참조 |
| 삭제 | 없음 | — |

지시서에 없는 파일 변경: `scripts/canonical_gate_commit.py` 신규 — hook 은 bash 라 판정·로그 형식을 테스트하려면 별도 스크립트가 필요했다(dup_guard.py 와 같은 패턴). 인증 핵심 파일은 건드리지 않았다.

## 판정 규칙

| 진입점 | ref | missing_canonical 조건 | ok 조건 |
|---|---|---|---|
| `goal_api` | goal_documents.id | 같은 테넌트·프로젝트에 `document_key` 일치 head 도, `source_path = doc_path` revision 도 없음 | 둘 중 하나 있음. `kind=prototype` 은 head CHECK 에 없는 종류라 ok(`kind_not_canonical_eligible`) |
| `runner_submit` | job_id | 목표에 연결된 승인 정본 0건 **그리고** 프로젝트 승인 정본 0건 | 하나라도 있음 |
| `commit` | 경로 | hook 은 DB 를 안 보므로 **항상 `unknown`** (`unverified`). 집계에서 로그 경로를 `source_path` 와 대조해 판정 | — |

- `unknown`: 타임아웃(기본 300ms, `CANONICAL_GATE_TIMEOUT_MS`)·예외·목표 미존재. 기록 후 통과. **300ms 는 판정+기록 합산 상한**이다(판정 70%, 기록은 남은 예산) — R2 이전에는 각각 300ms 라 최악 600ms 였다. 배치 제출은 잡마다 순차가 아니라 동시에 판정해 배치 전체 추가 지연이 잡 수와 무관하다.
- 경고는 `missing_canonical` 일 때만 응답에 붙는다(`unknown`·`ok` 는 조용히 기록).
- `CANONICAL_GATE_MODE`: `off`(쿼리 0회, pool 획득도 안 함) / `shadow`(기본, 알 수 없는 값도 shadow) / `enforce`(**미구현** — shadow 와 동일, 기록 detail 에 `enforce_not_implemented`, 코드에 TODO).
- 알려진 거짓 양성 원천: ① `goal_api` 의 자동 `document_key`(`kind:해시`)는 정본의 사람이 정한 key 와 거의 안 맞으므로 사실상 `source_path` 일치가 판정을 좌우한다. ② 정본 등록이 문서 등록보다 늦으면 그 순간엔 missing. ③ `project_document_legacy_links` 는 판정에 쓰지 않는다(링크는 등록 후에 생김). 아래 집계 SQL 이 ②·③을 표본으로 가려낸다.

## commit 로그 형식 (`<git-dir>/canonical_gate.log`, 탭 9칸)

```
CGv1 \t ts(UTC ISO8601) \t commit \t project \t path \t verdict \t mode \t status(A|M) \t blob_sha
```
project 기본 `AADS`(`CANONICAL_GATE_PROJECT` 로 변경). 경고는 stderr 노란색, 종료코드 불변.

## 운영 반영 절차 (머지 후 별도 승인 단계)

1. 마이그레이션 적용(이 작업에서는 적용하지 않았다): `psql -v ON_ERROR_STOP=1 -f migrations/20261002_canonical_gate_events.sql`
   - 적용 전에도 게이트는 로그(`canonical_gate.record_skipped`)만 남기고 통과한다.
2. hook 설치: `cp scripts/hooks/pre-commit .git/hooks/` (설치본이 다르면 `tests/unit/test_dup_guard.py::test_installed_hooks_stay_synced_with_repo_copies` 가 실패한다 — 복사 후 통과)
3. API 반영: **clean release SHA 의 격리 워크트리에서 `bash /root/aads/aads-server/deploy.sh bluegreen`** — `/root/aads/AGENTS.md` 11개 조항 전체가 적용된다(릴리스 SHA 당 이미지 1회 빌드, `--no-build` 슬롯 기동, 후보 헬스 통과 뒤에만 nginx 락, 동일 digest 스탠바이, 라우팅 헬스 실패 시 롤백). 활성 API 의 직접 재시작·reload 는 하지 않는다.
4. 배포 완료 ≠ 릴리스 인증. **5분 P0/P1 모니터링**(신규 오류 0)을 통과해야 완료로 보고한다. 모니터링 중 `canonical_gate.failed_open`·`canonical_gate.record_skipped` 로그와 `canonical_gate_events` 증가량을 함께 본다(마이그레이션 적용 전이면 `record_skipped` 는 정상).
5. 환경변수는 기본값(shadow)이면 설정 불필요.

## 1주 집계 SQL

```sql
-- 1) 진입점·판정별 건수
SELECT entrypoint, verdict, count(*) FROM canonical_gate_events
 WHERE occurred_at >= now() - interval '7 days' GROUP BY 1,2 ORDER BY 1,2;

-- 2) goal_api missing 표본 + 사후 등록 여부(거짓 양성 후보: 이미 정본/레거시 링크가 생긴 건)
SELECT e.occurred_at, e.project, d.id AS goal_doc_id, d.kind, d.doc_path,
       EXISTS (SELECT 1 FROM project_document_revisions r
                WHERE r.project_key = e.project AND r.source_path = d.doc_path) AS registered_later,
       EXISTS (SELECT 1 FROM project_document_legacy_links l
                WHERE l.goal_document_id = d.id) AS legacy_linked
  FROM canonical_gate_events e JOIN goal_documents d ON d.id::text = e.ref
 WHERE e.entrypoint = 'goal_api' AND e.verdict = 'missing_canonical'
   AND e.occurred_at >= now() - interval '7 days'
 ORDER BY random() LIMIT 50;

-- 3) runner_submit missing 표본(목표 승인 정본이 지금은 생겼는가)
SELECT e.occurred_at, e.project, e.ref AS job_id, e.detail->>'goal_id' AS goal_id,
       (SELECT count(*) FROM project_document_heads h
         WHERE h.project_key = e.project AND h.approved_revision_id IS NOT NULL) AS project_approved_now
  FROM canonical_gate_events e
 WHERE e.entrypoint = 'runner_submit' AND e.verdict = 'missing_canonical'
   AND e.occurred_at >= now() - interval '7 days'
 ORDER BY random() LIMIT 50;

-- 4) commit 로그 집계: 각 서버의 .git/canonical_gate.log 를 임시 테이블에 적재 후 정본 대조
CREATE TEMP TABLE cg_commit_log (tag text, ts timestamptz, ep text, project text, path text,
                                 verdict text, mode text, status text, blob text);
-- \copy cg_commit_log FROM '/path/.git/canonical_gate.log' (FORMAT text)   -- 탭 구분 그대로
SELECT count(*) FILTER (WHERE r.id IS NULL) AS missing_canonical,
       count(*) FILTER (WHERE r.id IS NOT NULL) AS registered,
       count(*) AS total
  FROM (SELECT DISTINCT path FROM cg_commit_log) c
  LEFT JOIN project_document_revisions r ON r.source_path = c.path;

-- 5) unknown 비율(타임아웃·예외)
SELECT entrypoint, detail->>'reason' AS reason, count(*) FROM canonical_gate_events
 WHERE verdict = 'unknown' AND occurred_at >= now() - interval '7 days' GROUP BY 1,2 ORDER BY 3 DESC;
```

## 차단 전환 판정 기준 (초안)

진입점별로 모두 충족해야 CEO 에게 enforce 전환을 올린다.

1. **거짓 양성률 < 5%** — missing_canonical 표본(진입점당 최대 50건, 전수가 50 미만이면 전수)을 사람이 분류해 "정본이 실제로 있었는데 missing 으로 판정" 비율을 잰다. 위 2·3번의 `registered_later`/`legacy_linked` 가 1차 후보.
2. **unknown 비율 < 2%** — 타임아웃·예외로 판정을 못 한 비율(5번). 높으면 차단 시 fail-open/fail-closed 정책을 따로 정해야 한다.
3. 표본 규모: 진입점당 missing_canonical 이 최소 30건 이상 쌓일 것(적으면 기간 연장).
4. 차단 시 우회 경로(환경변수·예외 목록)와 롤백을 먼저 설계할 것 — 우회가 습관이 되면 게이트가 무력화된다(R-PUSH 교훈).

## 롤백

- 즉시: `CANONICAL_GATE_MODE=off` — 쿼리 0회, hook 도 무동작. 모드는 호출 때마다 환경변수를 읽지만 실행 중인 API 프로세스의 환경은 바꿀 수 없으므로, API 에는 위 3번의 clean release 절차로 반영한다(compose 환경변수 수정은 CEO 승인 후 점검 창구). hook 은 환경변수를 주면 즉시 꺼진다.
- 완전: 승인된 revert 를 같은 clean release 절차로 배포한다. 기록 테이블(`canonical_gate_events`)은 게이트가 꺼져도 남겨 둬도 무해하다 — **DROP 은 실행하지 않는다**. down 스크립트는 격리 PG 에서만 검증했고 운영에서는 별도 승인 없이 실행하지 않는다.

## R2 — 보존 검수 지적 교정

**지적.** runner-58991158 이 `PRESERVATION_HARD_GATE: 삭제된 public 함수/클래스/API 라우터가 감지되었습니다: @router.post` (deletions=4) 로 반려됐다.

**판정: 실제 삭제가 아니라 데코레이터 재작성 거짓 양성.** 원 커밋은 `submit_job` 의 `@router.post("/pipeline/jobs", response_model=JobSubmitResponse, tags=["pipeline-runner"])` 줄을 `response_model_exclude_none=True` 가 붙은 줄로 고쳐 썼다. `code_reviewer._precheck_preservation_gate` 의 `_DELETED_SYMBOL_RE` 는 diff 의 `-@router.*` 줄을 삭제로 세고, 라우트 경로가 심볼에 없어 "같은 경로로 다시 추가됨" 면제가 `@router.*` 에는 적용되지 않는다. 실제 게이트 함수로 원 diff=FLAG, 신 diff=None 을 재현했다. 라우트는 사라지지 않았다 — 전후 라우트 목록 동일(아래).

**교정(최소·추가형).** 검수 시스템은 바꾸거나 우회하지 않았다.
1. 데코레이터 줄을 원문 그대로 복원하고, `JobSubmitResponse` 에 `@model_serializer(mode="wrap")` 를 추가해 `canonical_gate` 가 None 이면 응답 키를 뺀다(`response_model_exclude_none` 대체, 기존 응답 shape 불변).
2. 300ms 상한: 원본은 판정·기록이 각각 300ms 라 최악 약 600ms 였다. 합산 300ms(판정 70%, 기록은 남은 시간)로 고쳤다. 락 걸린 격리 PG 에서 229ms 실측.
3. 배치 제출: 잡마다 순차 판정이라 N×300ms 였다. `asyncio.gather` 로 동시 판정 — 멈춘 게이트 10건 배치가 0.9초 미만(테스트).

## 두 번째 확인 — prototype 면제와 목표/프로젝트 판정 범위

- 이전 승인 지시문 원문은 조회하지 못했다(승인된 정본 문서 없음/조회 불가). **따라서 "승인 지시문과 일치" 는 확인하지 못했다.**
- prototype 면제는 지시문이 아니라 스키마에서 도출했다: `project_document_heads.kind` CHECK 가 `plan, prd, spec, design, architecture, contract, tasks, report, reference` 만 허용하고 prototype 은 허용하지 않는다. 즉 prototype 은 정본 head 로 등록될 수 없으므로 "미등록" 경고 대상이 될 수 없다. **CEO 확인 필요 항목.**
- 판정 범위: goal 문서 API 는 `document_key` 일치 head 또는 `source_path` 일치 revision 이 있으면 ok, 러너 제출은 프로젝트의 승인(approved) 정본 수가 0 이면 missing. goal 이 다른 테넌트면 unknown(경고 없음).

## 검증

실제로 실행한 명령과 결과(2026-10-03, 기준 HEAD `a7e3d1f8`).

| 명령 | 결과 |
|---|---|
| `bash scripts/run_unit_tests.sh tests/unit/test_canonical_gate_shadow.py tests/unit/test_canonical_gate_entrypoints.py tests/unit/test_dup_guard.py` | 86 passed |
| 영향 범위 65개 파일 `bash scripts/run_unit_tests.sh $(cat impact_files.txt)` | 1019 passed, 1 skipped, **1 failed** (아래) |
| `ruff check --select F821,F811` (변경 파일 6개) | All checks passed |
| `python3 -m py_compile` 변경 파이썬 6개, `bash -n scripts/hooks/pre-commit` | 통과 |
| 라우트·OpenAPI 전후 비교(HEAD 트리 대 작업 트리) | pipeline_runner 14/36 라우트 동일, OpenAPI 43 paths, 전체 1012 라우트 동일 |
| 보존 게이트 `_precheck_preservation_gate` — 원 diff / 신 전체 diff(HANDOVER·보고서 포함) | FLAG / None |
| 격리 PG(postgres:15 일회용 컨테이너 + 내부 네트워크) 마이그레이션 2회 적용 + e2e 스크립트 | 멱등, 17/17 통과 |

**실제 경로 장애 주입(`test_canonical_gate_entrypoints.py`, 34건 통과).** 실제 `add_goal_document` 핸들러, `submit_job`/`submit_batch` 핸들러(HTTP TestClient 포함), pre-commit 블록 실행에 판정 예외·기록 예외·타임아웃·pool 부재를 주입해 raise 없이 기존 응답 shape 로 통과함을 확인했다. mode=off 는 `pool.acquire` 0회·이벤트 행 증가 0(격리 PG 에서도 확인). 소스 문자열 검사만으로 갈음하지 않았다.

**영향 테스트의 실패 1건 — 이번 변경과 무관한 기존 실패.** `tests/unit/test_output_validator.py::test_tool_backed_status_progress_tail_is_not_progress_only_blocked`. 이 테스트와 `output_validator.py` 는 변경하지 않았다. 원본 HEAD 를 `git archive` 로 풀어 같은 명령을 돌리면 동일하게 실패한다(1 failed, 6 passed). 삭제·수정하지 않았고 별도 조사 대상으로 남긴다.

**격리 PG 에서의 DROP 고지.** 격리 컨테이너에서만 down 스크립트를 검증하며 DROP 이 실행됐다. 운영에서는 실행하지 않았다.

## 실행하지 않은 검증

- 운영 DB 마이그레이션 적용(금지 사항) — 운영에 `canonical_gate_events` 가 없으면 게이트는 로그만 남기고 통과한다.
- 실제 전체 pre-commit 실행(커밋 금지), 설치본 `.git/hooks` 동기화 확인.
- clean release SHA 의 `deploy.sh bluegreen` 및 배포 후 5분 P0/P1 모니터링 — 아직 배포 전이다.
- 실제 운영 트래픽에서의 shadow 집계(1주 관측).

## 반영 상태

| 항목 | 상태 |
|---|---|
| diff 지문 | 변경 9개 파일의 `git hash-object` 목록 sha256 = `1a298fa410b6ed7639a86878cf7d0ece1c038a07c1e064c3fab3f3e19f4b6d40` (이 보고서·HANDOVER.md 제외, 목록은 /tmp/cg_r2/fingerprint.txt) |
| commit / push | 수행하지 않음(Runner 가 CEO 승인 후) |
| 운영 DB 변경 | 없음 |
| 배포 | 없음. enforce 전환 결정 없음 |
| HANDOVER.md | 최상단에 R2 항목 추가 |
| AADS 정본(handover_write / `POST /api/v1/handovers`) 기록 | **수행하지 못함.** 이 세션에 AADS API 토큰·DB 접속 정보가 없고(무인증 GET → 401), 운영 DB 변경 금지다. Runner 단계에서 인증된 호출로 기록해야 한다. |
| TODO e724f20a-1bbd-4839-b8d5-42509195cd5c 갱신 | 수행하지 못함(같은 사유, 이 세션에 TODO 갱신 수단 없음) |
| 비용 | 측정하지 않음 |
