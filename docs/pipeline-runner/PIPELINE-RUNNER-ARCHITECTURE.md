# Pipeline Runner 아키텍처 문서

**버전**: v2.0  
**최종 수정일**: 2026-04-15  
**작성자**: AADS AI (소스코드 실측 기반)  
**관련 소스**: `app/api/pipeline_runner.py`, `app/services/pipeline_runner_service.py`, `app/services/code_reviewer.py`, `scripts/pipeline-runner.sh`, `app/services/model_selector.py`

---

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [전체 아키텍처 다이어그램](#2-전체-아키텍처-다이어그램)
3. [실행 흐름 (7단계 상세)](#3-실행-흐름-7단계-상세)
4. [컴포넌트별 상세](#4-컴포넌트별-상세)
5. [병렬 실행](#5-병렬-실행)
6. [모델 라우팅](#6-모델-라우팅)
7. [Lock 메커니즘](#7-lock-메커니즘)
8. [AI 코드 검수](#8-ai-코드-검수)
9. [승인 흐름](#9-승인-흐름)
10. [서버 매핑](#10-서버-매핑)
11. [에러 처리 및 재시도](#11-에러-처리-및-재시도)
12. [운영 모니터링](#12-운영-모니터링)

---

## 1. 시스템 개요

### 목적

Pipeline Runner는 CEO/채팅 AI가 제출한 코드 수정 작업을 DB 큐 기반으로 실행하고, AI 리뷰와 CEO 승인을 거쳐 후속 배포 단계로 넘기는 파이프라인이다. 현재 주 실행기는 `scripts/pipeline-runner.sh`이며, FastAPI 레이어는 제출·조회·승인·설정 변경을 담당한다.

### 설계 원칙

- **코드 수정만 실행**: 러너 프롬프트에 H7 가드를 주입해 빌드/배포/프로세스 종료를 금지한다.
- **DB 중심 모델 선택**: 사이즈별 실행 모델과 AI 리뷰 모델을 `runner_model_config`에서 읽는다.
- **멀티 엔진 실행**: Claude CLI, Codex CLI, LiteLLM Runner를 `worker_model` 접두사로 분기한다.
- **원자적 Job 클레임**: `FOR UPDATE SKIP LOCKED` 기반으로 중복 실행을 방지한다.
- **실패 복구**: stuck 작업 정리, 의존 작업 고아 정리, 모델/토큰 폴백을 기본 내장한다.
- **R-AUTH 준수**: AADS 계열 LLM 호출은 OAuth 토큰 우선이며 외부 LLM은 LiteLLM 프록시를 사용한다.

### 2계층 구조

| 계층 | 파일 | 역할 |
|------|------|------|
| Python API | `app/api/pipeline_runner.py` | 작업 제출/조회/승인, 모델 설정 API, DB 모델 조회 |
| Shell Runner | `scripts/pipeline-runner.sh` | DB 폴링, 모델 사이클 구성, Claude/Codex/LiteLLM 실행, AI 리뷰 요청 |

`pipeline_jobs`와 `runner_model_config`를 두 계층이 함께 사용한다.

---

## 2. 전체 아키텍처 다이어그램

```text
CEO / 채팅 AI
   │
   │ POST /api/v1/pipeline/jobs
   │ POST /api/v1/pipeline/jobs/batch
   ▼
app/api/pipeline_runner.py
   ├─ 입력 검증 (project / session_id / size)
   ├─ size 추정 + _get_model_for_size(conn, size)
   ├─ pipeline_jobs INSERT / 중복 재사용 / pg_notify
   ├─ GET/PUT /pipeline/settings/runner-models
   └─ POST /pipeline/jobs/{job_id}/approve
   │
   ▼
PostgreSQL
   ├─ pipeline_jobs
   ├─ runner_model_config
   └─ code_reviews
   │
   ▼
scripts/pipeline-runner.sh (1865줄)
   ├─ claim_queued_job()
   ├─ get_db_model_cycle(size)
   ├─ MODEL_CYCLE + TOKEN_CYCLE 구성
   ├─ Claude CLI / Codex CLI / LiteLLM Runner 실행
   ├─ git diff HEAD 수집
   ├─ POST /api/v1/review/code-diff
   └─ awaiting_approval 전환 + notify
   │
   ▼
app/services/code_reviewer.py
   ├─ _get_review_models() → runner_model_config(size='AI_REVIEW')
   ├─ 입력 사전검사 (diff 형식, 러너 에러 텍스트 차단)
   └─ call_llm_with_fallback() 기반 리뷰
```

---

## 3. 실행 흐름 (7단계 상세)

### 상태 전이도

```text
queued → claimed → running → awaiting_approval → approved → deploying → done
                           ↘ cancelled / error        ↘ rejected → rolling_back → rejected_done
```

### 1단계: SUBMIT (queued)

- 엔드포인트: `POST /api/v1/pipeline/jobs`, `POST /api/v1/pipeline/jobs/batch`
- 입력 검증: 프로젝트 화이트리스트, UUID 세션 ID, size 패턴 검사
- 중복 처리:
  - 동일 `instruction_hash` + 활성 상태면 기존 job 재사용
  - 동일 `instruction_hash` + 최근 2시간 내 `error`면 `queued`로 리셋 후 재시도
- 모델 결정:
  - `worker_model`이 있으면 그대로 사용
  - 없으면 `_parse_size_from_instruction()` 또는 `_estimate_size()` 후 `_get_model_for_size()` 호출
- DB 저장 후 `pg_notify('pipeline_new_job', job_id)` 전송

### 2단계: CLAIM (claimed → running)

- `claim_queued_job()`가 `FOR UPDATE SKIP LOCKED`로 1건 클레임
- `depends_on`이 `done`이 아니면 클레임 대상에서 제외
- 원격 프로젝트의 `litellm:` 작업은 bash 러너가 직접 클레임하지 않도록 필터링
- `pre_validate()`에서 workdir, 디스크, git dirty 상태를 검사한다

### 3단계: RUNNING (실행 엔진 분기)

- H7 프롬프트 가드 삽입
- `worker_model` 접두사로 실행 엔진 분기:
  - `codex:*` → Codex CLI
  - `litellm:*` → LiteLLM Runner
  - 그 외 → Claude CLI
- 실행 전 `MODEL_CYCLE`, `TOKEN_CYCLE`을 만들어 모델/계정 폴백 순서를 확정한다
- 러너 PID를 `pipeline_jobs.runner_pid`에 기록해 watchdog이 생존 여부를 추적한다

### 4단계: AI_REVIEW

- 실행 성공 후 `git diff HEAD`를 최대 50,000자까지 수집한다
- diff가 실제 git diff 형식이면 `POST /api/v1/review/code-diff`를 30초 타임아웃으로 호출한다
- 리뷰 응답 verdict:
  - `APPROVE`
  - `REQUEST_CHANGES`
  - `FLAG`
  - `SKIP`
- 리뷰 입력이 diff가 아니면 LLM 호출 전 `FLAG` 또는 `SKIP`으로 차단한다

### 5단계: AWAITING_APPROVAL

- `pipeline_jobs.status/phase`를 `awaiting_approval`로 전환
- 채팅방에 diff 요약과 승인 도구 호출 지시를 게시
- `notify_completion()`이 CEO 검수 메시지와 후속 작업 승격을 담당한다

### 6단계: APPROVE / DEPLOYING

- `POST /pipeline/jobs/{job_id}/approve`에서 `approved` 또는 `rejected`로 상태 전환
- 승인 시 `autonomy_gate.record_task_result(... judge_verdict='pass')`
- 거부 시 `judge_verdict='fail'`
- 실제 배포/롤백 단계는 러너 및 프로젝트별 후속 처리로 이어진다

### 7단계: DONE / REJECTED / ERROR

- `done`: 작업 종료 및 다음 queued 작업 승격
- `rejected_done`: 거부 후 롤백 완료 상태
- `cancelled`/`error`: 타임아웃, 인증 실패, 프로세스 사망, 중복 supersede 등 비정상 종료

---

## 4. 컴포넌트별 상세

### 4.1 API Layer — `app/api/pipeline_runner.py` (829줄)

주요 역할:

- 작업 제출/배치 제출/조회/승인
- 프로젝트 Lock 상태 조회
- `runner_model_config` 조회/UPSERT API 제공
- `_get_model_for_size(conn, size)`를 통한 DB 기반 1순위 모델 선택

주요 엔드포인트:

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/pipeline/jobs` | POST | 단건 작업 제출 |
| `/pipeline/jobs` | GET | 작업 목록 조회 |
| `/pipeline/jobs/{job_id}` | GET | 작업 상세 조회 |
| `/pipeline/jobs/{job_id}/notify` | POST | 채팅 AI 후속 반응 트리거 |
| `/pipeline/jobs/{job_id}/approve` | POST | 승인/거부 |
| `/pipeline/jobs/batch` | POST | 의존성 그래프 기반 배치 제출 |
| `/pipeline/lock-status` | GET | 프로젝트 Lock 상태 |
| `/pipeline/settings/runner-models` | GET/PUT | 사이즈별 모델 설정 조회/업데이트 |

### 4.2 Orchestrator — `app/services/pipeline_runner_service.py`

- 인메모리 기반 대안 실행기
- Codex 가용 모델 검증: `default`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`
- size별 타임아웃 계산과 SSH 재시도 백오프를 가진다
- 문서 기준 주 실행기는 Shell Runner지만, 일부 모델/원격 경로 로직의 기준 구현이 이 서비스에도 남아 있다

### 4.3 Shell Runner — `scripts/pipeline-runner.sh` (1865줄)

핵심 기능:

- DB 폴링 + 원자적 클레임
- `get_db_model_cycle()`로 DB 모델 우선순위 조회
- `codex:`, `litellm:` 접두사 기반 실행기 분기
- `MODEL_CYCLE` / `TOKEN_CYCLE` 조합으로 모델+계정 폴백
- git diff 수집 및 리뷰 API 호출
- artifact/worktree/lock 정리

### 4.4 AI Reviewer — `app/services/code_reviewer.py` (431줄)

핵심 기능:

- `_get_review_models()`로 `runner_model_config(size='AI_REVIEW')` 모델 리스트 조회
- diff 형식 사전검사
- `call_llm_with_fallback()`로 리뷰 모델 호출
- `code_reviews` 테이블 저장

### 4.5 모델 레지스트리 — `app/services/model_selector.py`

- Codex CLI, Groq, DeepSeek, OpenRouter, Kimi, MiniMax, Qwen, Gemini 계열 식별자 정의
- 대시보드와 운영 UI가 사용할 수 있는 모델 ID 집합의 기준 역할을 한다

---

## 5. 병렬 실행

### parallel_group

- 같은 `parallel_group`이면 동일 프로젝트 내에서도 동시 실행 허용
- 다른 그룹 또는 그룹 없음이면 프로젝트 단위 동시 실행 제한 적용
- 현재 프로젝트당 동시 실행 상한: `MAX_CONCURRENT_PER_PROJECT=3`

### depends_on

- `depends_on`이 설정된 작업은 선행 job이 `done`일 때만 실행 가능
- 선행 job이 `error`, `rejected`, `rejected_done`이면 후속 queued 작업은 자동 고아 정리된다

### Git Worktree

- 조건: `MAX_CONCURRENT_PER_PROJECT > 1` 및 `/tmp` 여유 5GB 이상
- 경로: `/tmp/aads-wt-{job_id}`
- 실패 시 메인 workdir로 폴백

---

## 6. 모델 라우팅

### API 모델 선택: `_get_model_for_size(conn, size)`

현재 API 계층은 하드코딩 딕셔너리 대신 `runner_model_config`를 우선 조회한다.

```sql
SELECT models FROM runner_model_config WHERE size = $1
```

- `models`가 JSON 배열이면 첫 번째 항목을 1순위 실행 모델로 사용
- DB 조회 실패 또는 빈 값이면 하드코딩 폴백:

| Size | API 폴백 모델 |
|------|---------------|
| XS | `claude-haiku-4-5-20251001` |
| S | `claude-haiku-4-5-20251001` |
| M | `claude-sonnet-4-6` |
| L | `claude-opus-4-6` |
| XL | `claude-opus-4-6` |

### Shell Runner 모델 사이클

Shell Runner는 `get_db_model_cycle(size)`로 DB의 `models` 배열을 순서대로 꺼낸 뒤 각 모델을 2회씩 확장한다.

예시:

```text
DB models = [claude-sonnet-4-6, codex:gpt-5.4]
MODEL_CYCLE = [claude-sonnet-4-6, claude-sonnet-4-6, codex:gpt-5.4, codex:gpt-5.4]
TOKEN_CYCLE = [1, 2, 1, 2]
```

DB 조회 실패 시 Shell Runner 폴백:

- `XS`/`S` → `claude-haiku-4-5-20251001`
- `M`/`L` → `claude-sonnet-4-6`
- `XL` → `claude-opus-4-6`
- 앞단에는 `litellm:minimax-m2.7` 2회를 먼저 배치

### `worker_model` 우선순위

1. `worker_model` 직접 지정
2. 요청 `size`
3. instruction 내 `SIZE`/`규모` 힌트
4. `_estimate_size()` 휴리스틱

### Codex CLI 지원

`worker_model`이 `codex:` 접두사면 Codex CLI 경로로 실행한다.

지원 모델:

- `codex:default`
- `codex:gpt-5.4`
- `codex:gpt-5.4-mini`
- `codex:gpt-5.3-codex`

동작 규칙:

- 미지원 Codex 모델명은 `gpt-5.4`로 보정
- 연결 오류는 5초 간격 최대 12회 재시도
- rate limit, quota, auth 오류는 재시도 없이 즉시 다음 모델로 폴백
- Codex 실패 후 폴백 대상은 size별 Claude primary/secondary 모델 조합이다

### LiteLLM Runner 지원

`worker_model`이 `litellm:` 접두사면 Docker 내부 `litellm_runner.py`를 실행한다.

- instruction은 임시 파일로 전달
- 모델명은 접두사 제거 후 `--model`로 전달
- 외부 LLM 직접 REST 호출은 사용하지 않고 LiteLLM 프록시 경로만 사용한다

### `_estimate_size()` 휴리스틱

- 단순 작업은 `S`
- 복잡 키워드/파일 참조 수/본문 길이가 증가할수록 `L` 또는 `XL`
- 기본값은 `M`

---

## 7. Lock 메커니즘

### 3단계 Lock

| 레벨 | 방식 | 대상 |
|------|------|------|
| 프로세스 | `flock` | 러너 중복 실행 방지 (`/tmp/pipeline-runner.lock`) |
| 작업 | Redis HTTP API | 프로젝트별 work lock |
| 배포 | `flock` + Redis | 프로젝트별 deploy lock |

### DB 원자적 클레임

```sql
UPDATE pipeline_jobs
SET status='claimed', updated_at=NOW()
WHERE job_id = (
  SELECT p.job_id
  FROM pipeline_jobs p
  WHERE p.status='queued'
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING ...
```

### Lock 해제 조건

- 성공 종료
- 실패/취소
- 승인 대기 전환 후 work lock 해제
- watchdog에 의한 dead process 정리

---

## 8. AI 코드 검수

### Shell Runner 검수 흐름

1. `git diff HEAD` 수집
2. 변경 파일 목록 추출
3. `/api/v1/review/code-diff` 호출
4. verdict에 따라 채팅방 메시지 보강

### Reviewer 모델 선택

`app/services/code_reviewer.py`는 다음 순서로 모델을 결정한다.

1. `runner_model_config WHERE size = 'AI_REVIEW'`
2. DB 실패 시 `_REVIEW_MODEL_FALLBACK = 'qwen-turbo'`
3. 실제 호출은 `call_llm_with_fallback()`가 수행하므로 중앙 토큰/프록시 정책을 따른다

### 사전 검수 차단

다음 입력은 LLM 리뷰 전에 차단된다.

- 빈 diff → `SKIP`
- 러너 인증 실패 텍스트 → `FLAG(RUNNER_AUTH_FAILURE)`
- 실행 오류 텍스트 → `FLAG(RUNNER_EXECUTION_FAILURE)`
- `git diff` 수집 실패 텍스트 → `FLAG(GIT_DIFF_FAILURE)`
- 그 외 비정상 입력 → `FLAG(INVALID_REVIEW_INPUT)`

---

## 9. 승인 흐름

### Pipeline Runner 승인

1. AI 리뷰 후 `awaiting_approval`
2. CEO/채팅 AI가 `pipeline_runner_approve()` 또는 REST API 호출
3. API가 `approved` 또는 `rejected`로 상태 전환
4. 결과를 `review_feedback`와 autonomy 통계에 반영

### 승인 경합 방지

- API는 DB의 `WHERE status='awaiting_approval'` 조건으로 한 번만 상태 전환한다
- 메모리 기반 오케스트레이터는 `_job_approve_locks`를 별도로 사용한다

---

## 10. 서버 매핑

### 프로젝트 → workdir

| 프로젝트 | 서버 | workdir |
|----------|------|---------|
| AADS | 68 | `/root/aads/aads-server` |
| KIS | 211 | `/root/webapp` |
| GO100 | 211 | `/root/kis-autotrade-v4` |
| SF | 114 | `/data/shortflow` |
| NTV2 | 114 | `/srv/newtalk-v2` |

### LiteLLM Runner 경로

| 프로젝트 | runner 경로 |
|----------|-------------|
| AADS | `/app/scripts/litellm_runner.py` |
| KIS | `/root/kis-autotrade-v4/litellm_runner.py` |
| GO100 | `/root/kis-autotrade-v4/litellm_runner.py` |
| SF | `/root/scripts/litellm_runner.py` |
| NTV2 | `/root/scripts/litellm_runner.py` |

---

## 11. 에러 처리 및 재시도

### `classify_error()` 분류

| error_detail | 조건 예시 |
|-------------|-----------|
| `timeout` | exit 124, timed out |
| `git_conflict` | merge conflict |
| `oom_killed` | exit 137/139, Killed |
| `auth_error` | authentication, unauthorized |
| `rate_limit` | 429, quota exceeded |
| `disk_full` | ENOSPC, disk full |
| `code_syntax_error` | SyntaxError |
| `build_fail` | compilation error, ModuleNotFoundError |
| `permission_denied` | EACCES |
| `network_error` | connection refused, ETIMEDOUT |
| `unknown` | 기타 |

### 재시도 전략

- Claude/LiteLLM/Codex는 `MODEL_CYCLE` 기준으로 순차 폴백
- `TOKEN_CYCLE`로 계정 1/2를 번갈아 사용
- Codex 연결 계열 오류는 추가 12회 재시도
- 의존 작업 실패 시 후속 queued 작업은 자동 정리

### Watchdog/복구

- `MAX_JOB_RUNTIME=3600`
- `WATCHDOG_INTERVAL=300`
- dead PID 감지 시 `cancelled`
- 승인 대기 타임아웃: `APPROVAL_TIMEOUT_HOURS=24`

---

## 12. 운영 모니터링

### 로그 경로

| 로그 | 경로 |
|------|------|
| 메인 로그 | `/var/log/aads-pipeline/runner.log` |
| stdout | `/tmp/aads_pipeline_artifacts/{job_id}.out` |
| stderr | `/tmp/aads_pipeline_artifacts/{job_id}.err` |

### 핵심 운영 API

- `GET /api/v1/pipeline/jobs`
- `GET /api/v1/pipeline/jobs/{job_id}`
- `GET /api/v1/pipeline/lock-status?project=AADS`
- `GET /api/v1/pipeline/settings/runner-models`

### 설정 대시보드 반영 포인트

- 대시보드는 `runner_model_config`를 통해 XS/S/M/L/XL/AI_REVIEW 6개 카테고리를 관리한다
- 운영 UI는 모델 우선순위 카드를 2열로 배치하는 구성을 전제로 한다
- 모델 선택군은 운영상 다음 그룹을 사용한다: Claude, Codex, MiniMax, Groq, Gemini, Qwen, DeepSeek, Kimi, OpenRouter

### 핵심 상수 요약

| 상수 | 값 | 출처 |
|------|-----|------|
| `POLL_INTERVAL` | 5초 | `pipeline-runner.sh` |
| `MAX_RUNTIME` | 7200초 | `pipeline-runner.sh` |
| `MAX_JOB_RUNTIME` | 3600초 | `pipeline-runner.sh` |
| `MAX_CONCURRENT_PER_PROJECT` | 3 | `pipeline-runner.sh` |
| `APPROVAL_TIMEOUT_HOURS` | 24 | `pipeline-runner.sh` |
| `WATCHDOG_INTERVAL` | 300초 | `pipeline-runner.sh` |
| `_SSH_MAX_RETRIES` | 3 | `pipeline_runner_service.py` |
| `_SSH_RETRY_BASE_DELAY` | 2초 | `pipeline_runner_service.py` |

---

## 부록: 소스 파일 인벤토리

| 파일 | 줄 수 | 역할 |
|------|------|------|
| `app/api/pipeline_runner.py` | 829 | Pipeline Runner API + 설정 API |
| `app/services/code_reviewer.py` | 431 | AI 리뷰 모델 조회/판정/저장 |
| `scripts/pipeline-runner.sh` | 1865 | 주 실행기 |
| `app/services/pipeline_runner_service.py` | 2430+ | 대안 오케스트레이터/SSH 실행 |
| `app/services/model_selector.py` | 1300+ | 모델 식별자/라우팅 기준 |
