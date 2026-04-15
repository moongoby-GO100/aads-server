# Pipeline Runner API 레퍼런스

**버전**: v2.0  
**최종 수정일**: 2026-04-15  
**작성자**: AADS AI (소스코드 실측 기반)

---

## 목차

1. [REST API 엔드포인트](#1-rest-api-엔드포인트)
2. [채팅 AI 도구 파라미터](#2-채팅-ai-도구-파라미터)
3. [DB 스키마](#3-db-스키마)
4. [환경변수](#4-환경변수)
5. [사용 예시](#5-사용-예시)
6. [트러블슈팅 가이드](#6-트러블슈팅-가이드)

---

## 1. REST API 엔드포인트

Base URL: `http://localhost:8080/api/v1`

### 1.1 단건 작업 제출

```http
POST /pipeline/jobs
```

Request Body: `JobSubmitRequest`

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `project` | string | ✅ | — | `AADS`, `KIS`, `GO100`, `SF`, `NTV2` |
| `instruction` | string | ✅ | — | 최대 50,000자 |
| `session_id` | string | ✅ | — | UUID 형식 |
| `max_cycles` | integer | — | `3` | 1~10 |
| `size` | string | — | `"M"` | `XS`, `S`, `M`, `L`, `XL` |
| `worker_model` | string | — | `""` | 직접 실행 모델 지정 |
| `parallel_group` | string | — | `""` | 동일 그룹 병렬 실행 |
| `depends_on` | string | — | `""` | 선행 `job_id` |

모델 선택 규칙:

1. `worker_model`이 있으면 그대로 사용
2. 없으면 `size` 사용
3. `size == "M"`이면 instruction에서 size 힌트 파싱 후 `_estimate_size()` 적용 가능
4. 최종 size를 `_get_model_for_size(conn, size)`로 DB 조회

중복 처리:

- 동일 `instruction_hash` + 활성 작업이면 기존 job 재사용
- 동일 `instruction_hash` + 최근 2시간 내 `error`면 `queued`로 리셋 후 재시도

성공 응답 예시:

```json
{
  "job_id": "runner-a1b2c3d4",
  "status": "queued",
  "message": "작업이 대기열에 추가되었습니다. Runner가 곧 실행합니다."
}
```

활성 작업 재사용 예시:

```json
{
  "job_id": "runner-e5f6g7h8",
  "status": "active_exists",
  "message": "이미 진행 중인 작업이 있습니다: runner-e5f6g7h8 (현재 claude_code_work). 해당 작업을 계속 진행합니다."
}
```

### 1.2 배치 작업 제출

```http
POST /pipeline/jobs/batch
```

Request Body: `BatchSubmitRequest`

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `project` | string | ✅ | — | 프로젝트 코드 |
| `session_id` | string | ✅ | — | UUID |
| `jobs` | array | ✅ | — | 1~20개 |
| `parallel_group` | string | — | `batch-{uuid[:8]}` | 배치 병렬 그룹 |
| `max_cycles` | integer | — | `3` | 1~10 |

`jobs` 배열 항목: `BatchJobItem`

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `key` | string | ✅ | — | 배치 내 식별자 |
| `instruction` | string | ✅ | — | 작업 지시 |
| `size` | string | — | `"M"` | 작업 크기 |
| `worker_model` | string | — | `""` | 직접 실행 모델 |
| `depends_on_key` | string | — | `""` | 선행 key |

응답 예시:

```json
{
  "parallel_group": "batch-a1b2c3d4",
  "jobs": [
    {
      "key": "schema",
      "job_id": "runner-1111aaaa",
      "model": "claude-sonnet-4-6",
      "depends_on": null
    },
    {
      "key": "api",
      "job_id": "runner-2222bbbb",
      "model": "codex:gpt-5.4",
      "depends_on": "runner-1111aaaa"
    }
  ],
  "message": "2개 작업이 제출되었습니다. 의존성에 따라 순차/병렬 실행됩니다."
}
```

배치도 단건 제출과 동일하게 활성 작업 재사용 및 최근 실패 재시도를 지원한다.

### 1.3 작업 목록 조회

```http
GET /pipeline/jobs
```

Query Parameters:

| 파라미터 | 타입 | 기본값 | 설명 |
|---------|------|--------|------|
| `status` | string | — | 상태 필터 |
| `project` | string | — | 프로젝트 필터 |
| `session_id` | string | — | 채팅 세션 필터 |
| `limit` | integer | `20` | 1~100 |

응답 필드:

- `job_id`
- `project`
- `instruction` (앞 200자)
- `status`
- `phase`
- `cycle`
- `error_detail`
- `created_at`
- `updated_at`
- `started_at`
- `depends_on`
- `model`
- `worker_model`
- `actual_model`
- `size`

### 1.4 단건 작업 조회

```http
GET /pipeline/jobs/{job_id}
```

응답 필드:

- `job_id`
- `project`
- `instruction`
- `status`
- `phase`
- `cycle`
- `max_cycles`
- `result_output`
- `git_diff` (최대 5000자)
- `review_feedback`
- `error_detail`
- `started_at`
- `created_at`
- `updated_at`

에러:

- `400`: 잘못된 `job_id` 형식
- `404`: 작업 없음

### 1.5 완료 알림

```http
POST /pipeline/jobs/{job_id}/notify
```

Runner가 작업 종료 후 채팅 AI 후속 반응을 트리거할 때 호출한다.

동작:

- terminal 상태(`done`, `rejected_done`, `error`, `cancelled`)면 중복 처리 방지
- `awaiting_approval`면 검수/승인 요청 메시지 생성
- `done`이면 검증 체크리스트 메시지 생성
- `error`면 에러 상세 메시지 생성
- 작업 종료 시 동일 프로젝트의 다음 queued 작업 승격 시도

### 1.6 승인/거부

```http
POST /pipeline/jobs/{job_id}/approve
```

Request Body: `JobApproveRequest`

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `action` | string | ✅ | `approve` 또는 `reject` |
| `feedback` | string | — | 최대 2000자 |

성공 응답 예시:

```json
{
  "job_id": "runner-a1b2c3d4",
  "action": "approve",
  "message": "작업이 승인됨"
}
```

부수 효과:

- `review_feedback`에 `[CEO] ...` 추가
- `approve`면 autonomy 결과를 `pass`
- `reject`면 autonomy 결과를 `fail`

### 1.7 Lock 상태 조회

```http
GET /pipeline/lock-status?project=AADS
```

응답:

```json
{
  "project": "AADS",
  "locked": false,
  "queued_count": 3
}
```

### 1.8 Runner 모델 설정 조회

```http
GET /pipeline/settings/runner-models
```

설명:

- `runner_model_config` 전체 조회
- `models`는 JSON 배열로 반환
- `updated_at`, `updated_by` 포함

응답 예시:

```json
{
  "configs": [
    {
      "size": "M",
      "models": ["claude-sonnet-4-6", "codex:gpt-5.4"],
      "updated_at": "2026-04-15T09:10:11+09:00",
      "updated_by": "CEO"
    },
    {
      "size": "AI_REVIEW",
      "models": ["qwen-turbo", "claude-haiku-4-5-20251001"],
      "updated_at": "2026-04-15T09:10:11+09:00",
      "updated_by": "CEO"
    }
  ]
}
```

### 1.9 Runner 모델 설정 업데이트

```http
PUT /pipeline/settings/runner-models
```

Request Body: `_RunnerModelConfigUpdate`

```json
{
  "configs": [
    {
      "size": "XS",
      "models": ["claude-haiku-4-5-20251001", "litellm:minimax-m2.7"]
    },
    {
      "size": "AI_REVIEW",
      "models": ["qwen-turbo", "claude-haiku-4-5-20251001", "litellm:gemini-2.5-flash"]
    }
  ]
}
```

검증 규칙:

- `size` 정규식: `^(XS|S|M|L|XL|AI_REVIEW)$`
- `models`는 최소 1개 이상
- 저장은 `ON CONFLICT (size) DO UPDATE`
- `updated_by`는 현재 코드에서 `"CEO"` 고정

응답 예시:

```json
{
  "status": "ok",
  "message": "2개 size 모델 설정 업데이트 완료"
}
```

### 1.10 활성 작업 조회

```http
GET /tasks/active?session_id={id}
```

Task Monitor API. `pipeline_jobs`와 다른 작업 소스를 통합한 활성 작업 목록을 반환한다.

### 1.11 작업 로그 조회

```http
GET /tasks/{task_id}/logs?last_n=50&since=&log_type=
```

### 1.12 실시간 로그 스트림

```http
GET /tasks/{task_id}/stream
```

SSE 스트림:

- `text/event-stream`
- keepalive 20초

---

## 2. 채팅 AI 도구 파라미터

### 2.1 `pipeline_runner_submit`

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---------|------|------|--------|------|
| `project` | enum | ✅ | — | `AADS`, `KIS`, `GO100`, `SF`, `NTV2` |
| `instruction` | string | ✅ | — | 작업 지시 |
| `session_id` | string | — | 자동 감지 가능 | 채팅 세션 UUID |
| `max_cycles` | integer | — | `3` | 검수 반복 횟수 |
| `size` | enum | — | `"M"` | `XS`, `S`, `M`, `L`, `XL` |
| `worker_model` | string | — | `""` | 실행 모델 직접 지정 |
| `parallel_group` | string | — | `""` | 병렬 그룹 |
| `depends_on` | string | — | `""` | 선행 작업 ID |

`worker_model` 예시:

- Claude: `claude-opus-4-6`
- Codex: `codex:default`, `codex:gpt-5.4`, `codex:gpt-5.4-mini`, `codex:gpt-5.3-codex`
- LiteLLM: `litellm:gemini-2.5-flash`, `litellm:deepseek-chat`, `litellm:minimax-m2.7`

### 2.2 `pipeline_runner_submit_batch`

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---------|------|------|--------|------|
| `project` | string | ✅ | — | 프로젝트명 |
| `jobs` | array | ✅ | — | 1~20개 |
| `parallel_group` | string | — | `batch-{uuid}` | 병렬 그룹 |
| `max_cycles` | integer | — | `3` | 검수 반복 |
| `session_id` | string | — | 자동 감지 가능 | 채팅 세션 UUID |

`jobs` 항목:

- `key`
- `instruction`
- `size`
- `worker_model`
- `depends_on_key`

### 2.3 `pipeline_runner_status`

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `job_id` | string | — | 특정 작업 |
| `status` | string | — | 상태 필터 |

대표 상태:

- `queued`
- `claimed`
- `running`
- `awaiting_approval`
- `approved`
- `deploying`
- `done`
- `rejected`
- `rolling_back`
- `rejected_done`
- `cancelled`
- `error`

### 2.4 `pipeline_runner_approve`

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `job_id` | string | ✅ | 작업 ID |
| `action` | string | ✅ | `approve` 또는 `reject` |
| `feedback` | string | — | 거부/승인 메모 |

### 2.5 레거시 리다이렉트

| 레거시 도구명 | 실제 도구 |
|--------------|-----------|
| `pipeline_c_start` | `pipeline_runner_submit` |
| `pipeline_c_status` | `pipeline_runner_status` |
| `pipeline_c_approve` | `pipeline_runner_approve` |
| `pipeline_c_reject` | `pipeline_runner_approve` (`action="reject"`) |

---

## 3. DB 스키마

### 3.1 `pipeline_jobs`

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `job_id` | varchar | `runner-{uuid[:8]}` |
| `project` | varchar | 프로젝트 코드 |
| `instruction` | text | 작업 지시 |
| `instruction_hash` | varchar(16) | 멱등성/중복 처리 |
| `chat_session_id` | uuid | 채팅 세션 |
| `status` | varchar | 실행 상태 |
| `phase` | varchar | 세부 단계 |
| `cycle` | integer | 현재 사이클 |
| `max_cycles` | integer | 최대 사이클 |
| `model` | varchar | 제출 시 선택된 모델 |
| `worker_model` | varchar | 직접 지정 모델 |
| `actual_model` | varchar | 실제 실행 성공 모델 |
| `size` | varchar | `XS/S/M/L/XL` |
| `parallel_group` | varchar | 병렬 그룹 |
| `depends_on` | varchar | 선행 job |
| `runner_pid` | integer | 현재 실행 PID |
| `result_output` | text | stdout 요약 |
| `git_diff` | text | diff 전문 |
| `review_feedback` | text | 리뷰/CEO 피드백 |
| `error_detail` | varchar | 실패 분류 |
| `created_at` | timestamp | 생성 시각 |
| `started_at` | timestamp | 시작 시각 |
| `updated_at` | timestamp | 갱신 시각 |

### 3.2 `runner_model_config`

현재 모델 설정의 핵심 테이블.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `size` | varchar (PK) | `XS`, `S`, `M`, `L`, `XL`, `AI_REVIEW` |
| `models` | jsonb | 우선순위 모델 배열 |
| `updated_at` | timestamp | 갱신 시각 |
| `updated_by` | varchar | 수정 주체 |

예시:

```json
{
  "size": "L",
  "models": ["codex:gpt-5.4", "claude-opus-4-6", "litellm:minimax-m2.7"]
}
```

### 3.3 `code_reviews`

`app/services/code_reviewer.py` 저장 대상.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `job_id` | varchar | 대상 작업 |
| `project` | varchar | 프로젝트 |
| `verdict` | varchar | `APPROVE`, `REQUEST_CHANGES`, `FLAG`, `SKIP` |
| `score` | numeric | 0.0~1.0 |
| `feedback` | jsonb | 상세 피드백 |
| `diff_size` | integer | diff 길이 |
| `model_used` | varchar | 실제 리뷰 모델 |
| `cost` | numeric | 기록용 추정 비용 |
| `flag_category` | varchar | FLAG 카테고리 |
| `failure_stage` | varchar | 실패 단계 |
| `needs_retry` | boolean | 재시도 필요 여부 |

---

## 4. 환경변수

### 4.1 Shell Runner 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PGHOST` | `localhost` | DB 호스트 |
| `PGPORT` | `5433` | DB 포트 |
| `PGUSER` | `aads` | DB 계정 |
| `PGDATABASE` | `aads` | DB 이름 |
| `PGPASSWORD` | `""` | DB 비밀번호 |
| `POLL_INTERVAL` | `5` | 폴링 주기 |
| `AADS_API_URL` | `http://127.0.0.1:8100` | 내부 API |
| `MAX_RUNTIME` | `7200` | 단일 CLI 최대 실행 시간 |
| `MAX_RETRIES` | `2` | 모델 폴백 외 추가 재시도 제어값 |
| `MAX_CONCURRENT_PER_PROJECT` | `3` | 프로젝트당 동시 작업 수 |
| `APPROVAL_TIMEOUT_HOURS` | `24` | 승인 대기 타임아웃 |
| `ARTIFACT_MAX_AGE_HOURS` | `24` | artifact 보존 시간 |
| `MAX_JOB_RUNTIME` | `3600` | watchdog 상한 |
| `WATCHDOG_INTERVAL` | `300` | watchdog 주기 |
| `STUCK_CHECK_INTERVAL` | `300` | stuck 체크 주기 |
| `MIN_DISK_GB` | `1` | 최소 여유 디스크 |
| `DB_MODE` | `auto` | `docker` 또는 `psql` |
| `PG_CONTAINER` | `aads-postgres` | docker 모드 DB 컨테이너 |

### 4.2 인증 환경변수

| 변수 | 설명 |
|------|------|
| `ANTHROPIC_AUTH_TOKEN` | 1순위 OAuth 토큰 |
| `ANTHROPIC_AUTH_TOKEN_2` | 2순위 OAuth 토큰 |
| `ANTHROPIC_API_KEY` | CLI 호환용 런타임 주입 값 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 알림 |
| `TELEGRAM_CHAT_ID` | CEO 채팅 ID |

주의:

- 코드/문서 정책상 직접 참조 기준 토큰은 `ANTHROPIC_AUTH_TOKEN`
- 외부 LLM은 LiteLLM 프록시 경유
- 중앙 Python 호출은 `anthropic_client.py`의 `call_llm_with_fallback()`를 사용

### 4.3 모델 설정 관련 동작

- 러너는 별도 env로 모델 목록을 받지 않고 DB `runner_model_config`를 직접 조회한다
- Shell Runner 로그에는 `DB_MODEL_CONFIG ...` 태그가 남는다

---

## 5. 사용 예시

### 5.1 단건 작업 제출

```python
pipeline_runner_submit(
    project="AADS",
    instruction="app/api/health.py 응답에 uptime 필드를 추가하세요",
    size="S"
)
```

### 5.2 Codex CLI 지정

```python
pipeline_runner_submit(
    project="AADS",
    instruction="docs/pipeline-runner 문서를 최신 코드 기준으로 정리하세요",
    worker_model="codex:gpt-5.4",
    size="M"
)
```

### 5.3 LiteLLM 모델 지정

```python
pipeline_runner_submit(
    project="GO100",
    instruction="README 문구와 표를 정리하세요",
    worker_model="litellm:gemini-2.5-flash",
    size="S"
)
```

### 5.4 배치 의존성

```python
pipeline_runner_submit_batch(
    project="AADS",
    jobs=[
        {"key": "schema", "instruction": "마이그레이션 작성", "size": "M"},
        {"key": "api", "instruction": "API 반영", "size": "M", "depends_on_key": "schema"}
    ]
)
```

### 5.5 모델 설정 조회

```bash
curl http://localhost:8080/api/v1/pipeline/settings/runner-models
```

### 5.6 모델 설정 업데이트

```bash
curl -X PUT http://localhost:8080/api/v1/pipeline/settings/runner-models \
  -H "Content-Type: application/json" \
  -d '{
    "configs": [
      {"size": "M", "models": ["claude-sonnet-4-6", "codex:gpt-5.4"]},
      {"size": "AI_REVIEW", "models": ["qwen-turbo", "claude-haiku-4-5-20251001"]}
    ]
  }'
```

---

## 6. 트러블슈팅 가이드

### 6.1 작업이 `queued`에서 진행되지 않을 때

확인 항목:

1. 같은 프로젝트의 `running`/`claimed` 작업 수가 상한에 도달했는지
2. `depends_on` 선행 작업이 `done`인지
3. 원격 프로젝트의 `litellm:` 작업을 bash 러너가 건너뛰는 조건에 걸렸는지
4. `runner_model_config`에 size별 모델이 비어 있지 않은지

### 6.2 Codex 작업이 바로 폴백될 때

주요 원인:

- `rate limit`, `quota exceeded`, `billing` 문자열 감지
- `unauthorized`, `forbidden`, `invalid key`, `auth` 감지
- 미지원 `codex:` 모델명 입력

### 6.3 AI 리뷰가 `FLAG`로 끝날 때

우선 확인:

1. `git_diff`가 실제 `diff --git` 형식인지
2. 러너 stderr가 리뷰 입력으로 들어가지 않았는지
3. `runner_model_config(size='AI_REVIEW')` 설정이 유효한지

### 6.4 DB 기반 모델 선택이 기대와 다를 때

확인 쿼리:

```sql
SELECT size, models, updated_at, updated_by
FROM runner_model_config
ORDER BY size;
```

주의:

- API는 배열의 첫 모델만 `model` 컬럼에 저장
- Shell Runner는 배열 전체를 읽어 `MODEL_CYCLE`로 확장한다

### 6.5 주요 진단 쿼리

```sql
SELECT status, COUNT(*) FROM pipeline_jobs GROUP BY status ORDER BY 2 DESC;

SELECT job_id, project, status, phase, actual_model, error_detail, updated_at
FROM pipeline_jobs
ORDER BY updated_at DESC
LIMIT 20;

SELECT size, models, updated_at, updated_by
FROM runner_model_config
ORDER BY size;

SELECT job_id, verdict, score, model_used, flag_category, failure_stage, created_at
FROM code_reviews
ORDER BY created_at DESC
LIMIT 20;
```

---

## 부록: 타임아웃/상수 요약

| 항목 | 값 | 출처 |
|------|-----|------|
| `MAX_RUNTIME` | 7200초 | `scripts/pipeline-runner.sh` |
| Codex 연결 재시도 | 5초 × 12회 | `scripts/pipeline-runner.sh` |
| 리뷰 API 타임아웃 | 30초 | `scripts/pipeline-runner.sh` |
| `MAX_JOB_RUNTIME` | 3600초 | `scripts/pipeline-runner.sh` |
| `WATCHDOG_INTERVAL` | 300초 | `scripts/pipeline-runner.sh` |
| `_SSH_MAX_RETRIES` | 3 | `app/services/pipeline_runner_service.py` |
| `_SSH_RETRY_BASE_DELAY` | 2초 | `app/services/pipeline_runner_service.py` |
