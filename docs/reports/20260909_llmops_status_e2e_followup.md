# OHVIS 내부 LLMOps — 상태 총계 스코프 수정 + 안전한 E2E 검증기

- 날짜: 2026-09-09 (KST)
- 범위: `/api/v1/ohvis/llmops/status` 프로젝트 스코프 집계, `scores`/`feedback` 누락,
  부분 적용 내성, 그리고 저장소 밖 `/tmp/e2e_eval.py`를 대체하는 커밋된 검증 스크립트
- 기준 커밋: `5f5332f3e168ad65889adf44cd57e1a1dee82871` (= `origin/main`)
- 작업 위치: 격리 워크트리 `/tmp/aads-wt-runner-0457960f` (clean 상태에서 시작)
- **배포 상태: 미배포.** 이 러너는 코드 수정만 수행한다. 커밋/푸시/빌드/배포는
  CEO 승인 후 Runner가 수행한다. 배포 SHA·deploy_run_id·5분 P0/P1 인증은
  이 문서에서 **미해결**로 남는다.

---

## 0. 프리플라이트 — 파일 소유권 충돌 해소 (ledger id 27972)

지시서는 `chat_workspace_change_ledger` id 27972가 `app/services/llmops_store.py`를
다른 세션의 dirty 기록으로 들고 있으니, 편집 전에 커밋/파일 증거로 해소하라고 했다.

| 근거 | 값 |
| --- | --- |
| ledger 27972 | `app/services/llmops_store.py`, status=`dirty`, session `7a1b186e-e71f-41c5-bd7b-e5926f41b4d9`, updated_at `2026-09-09 04:52:56+09` |
| 워크트리 `git status` | 해당 파일 없음 (clean) |
| 메인 체크아웃 `git status` | 해당 파일 없음 (clean) |
| 파일 blob 해시 | `git hash-object` = `bb5baba08fb0d86d777d763e52fc5063ebfd83d0` |
| `origin/main:app/services/llmops_store.py` | `bb5baba08fb0d86d777d763e52fc5063ebfd83d0` — **완전히 동일** |
| 세션 7a1b186e의 작업 커밋 | `227e1d9a Chat-Finalize[aads-server]: 3 files (7a1b186e)` — 이 파일 590줄 최초 커밋 |
| 이후 커밋 | `86571dae`, `40e7eb5e` (다른 세션이 같은 파일을 이미 수정·커밋함) |
| 세션 7a1b186e 마지막 ledger 활동 | `2026-09-09 05:27:34+09` (점검 시각 `08:03:41+09` 기준 2시간 36분 유휴) |
| 활성 pipeline_jobs | 7a1b186e 관련 러닝 잡 없음 |

**결정적 반례 — ledger는 커밋 시 정합화되지 않는다:**
같은 세션의 ledger id 28547 `app/services/llmops_chat_hook.py`가 `dirty`(04:58:11+09)로
남아 있으나, 그 파일은 34분 뒤 `582fb94f`(2026-09-08 22:32:01 +0200 = 09-09 05:32:01 KST)로
정상 커밋되었다. 전역적으로도 `dirty` 1,709건이 적체되어 있다.

**결론: 실질적 소유권 충돌 없음.** 27972는 커밋 이후 정합화되지 않은 잔여 기록이며,
디스크 내용은 `origin/main`과 바이트 단위로 동일하므로 덮어쓸 미커밋 작업이 존재하지
않는다. 편집을 진행했다. **ledger 행은 조작하지 않았다.**

### 중복 러너 경고 (미해결)

프리플라이트 중 `pipeline_jobs`에 **동일 지시서를 가진 러너가 2건** 확인되었다.

| job_id | project | status | started_at |
| --- | --- | --- | --- |
| `runner-0457960f` | AADS | running (이 세션) | 2026-09-09 08:02:28+09 |
| `runner-5744f732` | AADS | **queued** | 2026-09-09 08:02:35+09 |

같은 작업 카드에 대한 중복 제출로 보인다. 이 세션은 신규 잡을 제출하지 않았다.
`runner-5744f732`는 실행 전에 취소하는 것을 권한다 (동일 파일 동시 편집 위험).

---

## 1. 확인된 결함과 수정 — `llmops_store.get_status()`

### 1.1 결함

1. **`project` 인자가 SQL에서 완전히 무시되었다.** 응답에 `"project": "AADS"`를
   되돌려주기만 하고 모든 `COUNT(*)`는 전역이었다. 즉 `?project=AADS`가 GO100·CEO·
   `project=NULL` 행까지 포함한 숫자를 AADS의 값인 것처럼 표시했다 (권한 없는
   프로젝트 누출).
2. **`db.scores` / `db.feedback`이 아예 없었다.** 대시보드 `/ops/evals`는 두 지표를
   항상 "미제공"으로 표시할 수밖에 없었다.
3. **`datasets` 테이블 존재가 `examples`/`experiments` 조회를 함께 게이트했다.**
   부분 적용된 DB에서 `llmops_examples`만 없어도 예외가 나면서 `db` 전체가
   `{"available": false}`로 무너져, 셀 수 있었던 집계까지 전부 사라졌다.

### 1.2 수정 (`app/services/llmops_store.py`)

- `build_status_count_specs(tables, project)` — 순수 함수로 (지표 키, 스칼라 서브쿼리)
  목록을 만든다. **필요한 relation이 전부 있는 지표만** 목록에 들어간다.
- 소유 관계를 정본 스키마의 FK로 따라간다 (아래 §1.3).
- **파라미터화**: 프로젝트 이름은 언제나 `$1`. SQL 문자열에 값이 박히지 않는다
  (`test_two_projects_produce_different_scoped_sql_parameters`가 회귀를 막는다).
- **`project=None` 전역 semantics 보존**: `examples`/`experiments`/`scores`/`feedback`는
  예전과 똑같은 무조건 `COUNT(*)`, `traces`/`datasets`는 `($1::text IS NULL OR …)`로
  전부 통과.
- **0과 "셀 수 없음"의 구분**: 계산할 수 없는 지표는 0이 아니라 **응답 키 자체가
  빠진다.** 대시보드는 키 부재를 "미제공"으로 이미 렌더한다.
- **왕복 1회**: 모든 지표를 별칭 붙인 합본 쿼리 하나(`fetchrow`)로 센다. 상태
  엔드포인트는 대시보드가 주기적으로 호출하므로 지표당 왕복은 회귀다.
- **지표 단위 열화**: 합본 쿼리가 깨지면 지표별 `fetchval`로 나눠 세고, 실패한
  지표만 결과에서 뺀다. 하나가 못 세어졌다고 나머지 집계가 죽지 않는다.
- 응답 필드는 전부 보존했고 `db.project_scoped`(bool) 하나만 추가했다.

### 1.3 소유권 조인 (정본 스키마 기준)

운영 DB `information_schema` 실측으로 확인한 FK:

| 원장 | project 소유 경로 |
| --- | --- |
| `llmops_traces` | `project` 컬럼 직접 |
| `ohvis_harness_traces` | `project` 컬럼 직접 |
| `llmops_datasets` | `project` 컬럼 직접 |
| `llmops_examples` | `dataset_id` → `llmops_datasets.project` |
| `llmops_experiments` | `dataset_id` → `llmops_datasets.project` |
| `llmops_scores` | `experiment_id`→experiments→datasets **또는** `example_id`→examples→datasets **또는** `source_trace_id`(TEXT)→`llmops_traces.id::text` — 세 경로 중 하나라도 증명되면 소유 |
| `llmops_feedback` | `trace_id`(TEXT) → `llmops_traces.id::text` → `project` |

소유를 증명할 수 없는 행(예: `trace_id`가 없고 `source_ref`만 있는 feedback)은
프로젝트 총계에서 **제외**한다. 그래야 다른 프로젝트 데이터가 AADS 총계로 새지 않는다.

### 1.4 실측 검증 (읽기 전용, 운영 DB)

수정된 `get_status()`를 운영 DB에 대해 직접 실행한 결과 (호스트 venv, SELECT만):

| 지표 | AADS | GO100 | 전역 | 파티션 검증 |
| --- | --- | --- | --- | --- |
| `traces.total` | 1 | 32 | 120 | AADS+GO100 < 전역 (CEO/NULL project 제외됨 — 정상) |
| `legacy_traces.total` | 9 | 50 | 232 | 동일 |
| `datasets` | 4 | 1 | 5 | 4+1=5 ✅ |
| `examples` | 2 | 1 | 3 | 2+1=3 ✅ |
| `experiments` | 5 | 2 | 7 | 5+2=7 ✅ |
| `scores` | 30 | 6 | 36 | 30+6=36 ✅ |
| `feedback` | 0 | 0 | 0 | 실제 0건 (키 존재 = "정말 0") |

dataset을 통해 소유가 정의되는 4개 원장은 **정확히 분할**된다 — 이중 계상도, 누락도
없다. 배포 전 API(`https://aads.newtalk.kr`)는 같은 시점에 `?project=AADS`로 물어도
전역 숫자(`traces.total=119`, `datasets=4`, `examples=2`, `experiments=6`)를 그대로
돌려주며 `scores`/`feedback`은 아예 없었다 — 이것이 수정 전 증거다.

### 1.5 API 계약

`app/api/ohvis_llmops.py`는 **수정하지 않았다.** 라우트가 이미 `project` 쿼리를
스토어로 넘기고 있어 계약 조정이 불필요했다.

---

## 2. `/tmp/e2e_eval.py` 폐기와 안전한 대체 (`scripts/verify_llmops_e2e.py`)

### 2.1 옛 스크립트 — 사용 금지 (읽기 전용 조사만 수행)

- 경로: `/tmp/e2e_eval.py` (저장소 **밖**), 3,100 bytes,
  sha256 `734c856fa494aad4c2945c615bd8796f5771fd6eb65228fb4e249e29b514b971`
- ledger id `28661`(session 8ad08cc2), `28685`(session 7fb5f50a)에 `dirty`로 등재
- 확인된 동작 (원문 grep, 실행하지 않음):
  - `DELETE FROM llmops_scores WHERE experiment_id IN (SELECT id FROM llmops_experiments WHERE status='running')`
  - `DELETE FROM llmops_experiments WHERE status = 'running'`
    → **실행 중인 모든 실험과 그 점수를 일괄 삭제**한다. 다른 프로젝트 것까지.
  - 하드코딩된 UUID(`did`/`tid`/`xid`)로 대상을 지정
  - rule evaluator를 거치지 않고 `INSERT INTO llmops_scores (…score, passed…)`로
    점수를 직접 지어냄
  - `UPDATE llmops_experiments SET status='completed'…`로 기존 행을 덮어씀

**조치: 실행/커밋/수정/삭제/재사용 모두 하지 않았다.** 파일은 손대지 않고 그대로 두었다.
ledger 28661/28685도 조작하지 않았다. 이 스크립트는 **배포되지도 커밋되지도 않았다** —
그렇게 표기하면 거짓이다.

### 2.2 대체 스크립트 `scripts/verify_llmops_e2e.py`

정본 인증 API만 사용한다. 원칙:

| 원칙 | 구현 |
| --- | --- |
| 기본 읽기 전용 | 인자 없이 실행하면 GET만 한다 |
| 쓰기는 명시적 | `--write` 플래그로만 |
| 격리된 합성 픽스처 | dataset slug `aads-llmops-e2e-verify` 하나에만 추가. 운영 dataset `aads-failed-traces`는 건드리지 않는다 |
| UUID 하드코딩 금지 | 대상 trace는 `/candidates` API가 돌려준 실제 id |
| 정본 rule evaluator | 점수는 `POST /evals/run`이 낸다. SQL INSERT로 지어내지 않는다 |
| 일괄 정리 금지 | DELETE·TRUNCATE 없음, 기존 행 UPDATE 없음. 추가만 |
| 멱등성 검증 | 같은 trace를 두 번 승격해 `example_id`가 같은지 확인 |
| 인증 | 기존 승인 env만: `AADS_MONITOR_KEY`(→ `X-Monitor-Key`) 또는 `AADS_API_TOKEN`(→ Bearer). **값은 출력하지 않는다** |
| 시크릿 미출력 | 인증 실패 시에도 env 이름만 안내 |

사용:

```bash
python3 scripts/verify_llmops_e2e.py                 # 읽기 전용
python3 scripts/verify_llmops_e2e.py --write         # 합성 픽스처 E2E
python3 scripts/verify_llmops_e2e.py --json          # 기계 판독용
```

종료 코드: `0` 통과 / `1` 검증 실패 / `2` 설정·인증 문제.

### 2.3 실행 결과 (운영 API, 배포 전 코드 대상)

`--write` 1회 실행. **API가 실제로 돌려준 id만** 기재한다:

| 항목 | 값 |
| --- | --- |
| 대상 trace (후보 API가 선정) | `992cf936-ada0-45f8-8039-4674ba595a54` |
| 합성 dataset | `aads-llmops-e2e-verify` / `5f7920ab-185c-4422-ba6f-406ad6bf15d0` (project=AADS) |
| 합성 example | `fad92cff-cccd-497f-9761-b9db04bb05b3` |
| 합성 experiment | `aca76086-b44f-4118-8d56-e5413c5db2d2` (status=completed, evaluator=`rule`) |
| 생성된 점수 | 6건 (criteria: cost_present, error_classification, final_response, latency_budget, source_presence, tool_policy) |
| evaluator 요약 | `mean_score=0.7778`, `pass_rate=1.0` |

검증 통과 항목: 승격 생성, **멱등 재승격(2회차 example_id 동일)**, rule evaluator 실행,
결과 조회, 점수 출처가 evaluator임, **additive-only(음수 delta 없음)**.

파괴 없음 확인 (DB 실측): `llmops_experiments` 6 → 7, `llmops_scores` 30 → 36 —
단조 증가. 삭제·수정된 기존 행 없음. GO100 총계는 수정 후 집계에서도 변동 없음
(datasets 1 / examples 1 / experiments 2 / scores 6).

배포 전 API 대상이었으므로 다음 4건은 **예상대로 실패**했고, 이것이 수정 필요성의
증거다:

- `status.project_scoped_flag` — 배포본에 필드 없음
- `status.reports_scores_and_feedback` — `scores`/`feedback` 미제공
- `status.scope_actually_narrows` — 스코프 집계가 전역과 동일 (누출)
- `status.reflects_the_new_fixture` — 상태 응답이 `scores`를 내지 않아 delta 계산 불가

배포 후 같은 명령이 전부 PASS(rc=0)로 바뀌어야 한다.

---

## 3. 테스트

| 파일 | 내용 |
| --- | --- |
| `tests/unit/test_llmops_status_scope.py` (신규, 17건) | AADS vs 다른 프로젝트 스코프, scores/feedback 총계, 0 vs 미제공, 부분 적용, DB 불가, 전역 semantics 보존, 왕복 1회, 지표 단위 열화 |
| `tests/unit/test_ohvis_llmops.py` (수정) | 상태 fake에 `fetchrow` 추가(합본 집계 대응); `CANONICAL_LLMOPS_MODULES`에 `llmops_chat_hook.py` 반영 |

```
$ JWT_SECRET_KEY=test .venv/bin/python -m pytest \
    tests/unit/test_llmops_status_scope.py tests/unit/test_ohvis_llmops.py -q
81 passed in 1.24s
```

`test_ohvis_llmops.py::test_llmops_store_is_the_only_ledger_module`는 이 작업 **이전부터**
HEAD에서 깨져 있었다 (`5f5332f3` 아카이브 사본으로 재현: `1 failed, 63 passed`).
원인은 `582fb94f`가 `app/services/llmops_chat_hook.py`를 추가하면서 정본 모듈 목록을
갱신하지 않은 것. R-QUALITY("기존 테스트가 실패하면 즉시 수정")에 따라 함께 고쳤다.

정적 검사:

- `python -m py_compile` — 3개 파일 통과
- pre-commit ruff 게이트(`--select F821,F811`) — 통과
- 추가 확인: `ISC`/`E9`/`F401` 통과. 잔여 `UP045`(`Optional[X]`) 42→44건은 파일 전반의
  기존 스타일과 동일한 종류이며 커밋 훅 대상이 아니다.
- pre-commit Step 2 재현(`docker run … -v <worktree>:/app:ro -c "import app.services.llmops_store, app.api.ohvis_llmops"`) — `IMPORT OK`

---

## 4. 대시보드

- 운영 릴리스 `b6d7cf4b66d47be2f5853629c5379f1b32bba477` 유지. 이후 `9b933d3`은
  HANDOVER 15줄 인증 문서만 바꾼 것으로 git 확인되어, **문서 전용 차이로 대시보드를
  다시 빌드하지 않는다.**
- 상태 UI는 이미 `db.scores`/`db.feedback`을 표시하거나 없으면 "미제공"으로 렌더한다.
  따라서 API 릴리스만으로 `/ops/evals`에 정확한 AADS Scores/Feedback이 나타난다.
- 기존 증거 `/root/aads/verification/llmops-20260909/`(evals-desktop.png, evals-mobile.png,
  traces-desktop.png, browser-result.log)와 `/tmp/aads-llmops-dashboard-final-release.log`는
  **이전 시점의 산출물**이다. 이를 근거로 새 브라우저 확인을 했다고 주장하지 않는다.
- **브라우저 갭(미해결)**: 이 세션에서 새 인증 브라우저 스크린샷을 찍지 않았다.
  API 릴리스 후 서버 Playwright + 승인된 인증으로 `/ops/evals` 캡처가 필요하다.

---

## 5. 변경 파일

| 파일 | 상태 |
| --- | --- |
| `app/services/llmops_store.py` | 수정 (+180/−21 전체 diffstat 기준) |
| `tests/unit/test_llmops_status_scope.py` | 신규 |
| `tests/unit/test_ohvis_llmops.py` | 수정 (fake `fetchrow`, 정본 모듈 목록) |
| `scripts/verify_llmops_e2e.py` | 신규 |
| `docs/reports/20260909_llmops_status_e2e_followup.md` | 신규 (이 문서) |
| `HANDOVER.md` | 추가 전용 (기존 내용 보존) |

`app/api/ohvis_llmops.py`는 변경 불필요로 판단해 손대지 않았다. 대시보드·배포 스크립트·
finance/runtime dirty 데이터·시크릿·스키마·옛 evaluator 채점 로직은 건드리지 않았다.

## 6. 비용

**측정하지 않음(unmeasured).** 외부 LLM Judge·LangSmith SaaS·유료 호출을 일절
사용하지 않았다. 평가는 전부 내부 rule evaluator다.

## 7. 미해결 항목

1. **커밋/푸시/빌드/배포·GitHub 커밋 URL·배포 SHA·deploy_run_id·5분 P0/P1 인증** —
   이 러너의 권한 밖. CEO 승인 후 Runner가 수행한다. **"배포됨"이라고 보고하지 않았다.**
2. **배포 후 재검증** — `python3 scripts/verify_llmops_e2e.py --project AADS`가 rc=0이
   되어야 한다 (현재 배포본에서는 3건 실패).
3. **브라우저 스크린샷** — 서버 Playwright + 승인 인증으로 `/ops/evals` 신규 캡처 필요.
4. **중복 러너 `runner-5744f732`(queued)** — 같은 카드 중복 제출. 취소 권장.
5. **ledger 적체 1,709 dirty 행** — 커밋 시 정합화되지 않는 구조적 문제. 이번 범위
   밖이라 조작하지 않았다. 별도 과제 필요.
6. **합성 픽스처 정리 정책** — `aads-llmops-e2e-verify` dataset은 의도적으로 남긴다
   (추가만, 삭제 금지 원칙). `--write` 반복 실행은 실험을 계속 누적한다.
