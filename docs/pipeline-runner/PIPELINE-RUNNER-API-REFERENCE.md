# Pipeline Runner API 레퍼런스

**버전**: v1.0  
**최종 수정일**: 2026-04-09  
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
Internal Header: `x-monitor-key: internal-pipeline-call`

### 1.1 단건 작업 제출

```
POST /pipeline/jobs
```

**Request Body** (`JobSubmitRequest`):

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `project` | string | ✅ | — | `AADS`, `KIS`, `GO100`, `SF`, `NTV2` |
| `instruction` | string | ✅ | — | 작업 지시 (max 50,000자) |
| `session_id` | string | ✅ | — | UUID 형식 채팅 세션 ID |
| `max_cycles` | integer | — | 3 | 검수 반복 횟수 (1~10) |
| `size` | string | — | "M" | XS/S/M/L/XL (worker_model 지정 시 무시) |
| `worker_model` | string | — | "" | 모델 직접 지정 |
| `parallel_group` | string | — | "" | 병렬 그룹 ID |
| `depends_on` | string | — | "" | 선행 작업 job_id |

**Response** (`JobSubmitResponse`):

```json
{
  "job_id": "runner-a1b2c3d4",
  "status": "queued",
  "message": "작업이 대기열에 추가되었습니다"
}
```

**에러 응답**:

| 상태코드 | 조건 |
|---------|------|
| 400 | 유효하지 않은 project, 잘못된 session_id 형식 |
| 409 (논리적) | 중복 작업 — `status: "duplicate"`, `job_id: "기존 job_id"` |
| 500 | 서버 에러 |

---

### 1.2 배치 작업 제출

```
POST /pipeline/jobs/batch
```

**Request Body** (`BatchSubmitRequest`):

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `project` | string | ✅ | — | 프로젝트명 |
| `session_id` | string | ✅ | — | UUID 세션 ID |
| `jobs` | array | ✅ | — | 작업 배열 (1~20개) |
| `parallel_group` | string | — | `batch-{uuid[:8]}` | 병렬 그룹 (미지정 시 자동 생성) |
| `max_cycles` | integer | — | 3 | 검수 반복 (1~10) |

**`jobs` 배열 아이템** (`BatchJobItem`):

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|------|--------|------|
| `key` | string | ✅ | — | 작업 식별 키 (배치 내 고유) |
| `instruction` | string | ✅ | — | 작업 지시 (max 50,000자) |
| `size` | string | — | "M" | XS/S/M/L/XL |
| `worker_model` | string | — | "" | 모델 직접 지정 |
| `depends_on_key` | string | — | "" | 배치 내 선행 작업 key |

**Response**:

```json
{
  "parallel_group": "batch-a1b2c3d4",
  "jobs": [
    {"key": "task1", "job_id": "runner-x1y2z3", "status": "queued"},
    {"key": "task2", "job_id": "runner-a4b5c6", "status": "queued", "depends_on": "runner-x1y2z3"}
  ]
}
```

---

### 1.3 작업 목록 조회

```
GET /pipeline/jobs
```

**Query Parameters**:

| 파라미터 | 타입 | 기본값 | 설명 |
|---------|------|--------|------|
| `status` | string | — | 상태 필터 (max 30자) |
| `project` | string | — | 프로젝트 필터 (max 10자) |
| `session_id` | string | — | 세션 필터 (max 36자) |
| `limit` | integer | 20 | 결과 수 (1~100) |

**Response**: 작업 배열 (최신순)

---

### 1.4 단건 작업 조회

```
GET /pipeline/jobs/{job_id}
```

**Response**: `pipeline_jobs` 전체 컬럼

**에러**: 404 (작업 없음)

---

### 1.5 승인/거부

```
POST /pipeline/jobs/{job_id}/approve
```

**Request Body** (`JobApproveRequest`):

| 필드 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `action` | string | ✅ | `"approve"` 또는 `"reject"` |
| `feedback` | string | — | 피드백 (max 2,000자, 거부 시 활용) |

**에러**:
- 400: `status`가 `awaiting_approval`이 아닌 경우, 또는 잘못된 action
- 404: 작업 없음

**부수 효과**: `autonomy_gate.record_task_result` 호출 (approve=pass, reject=fail)

---

### 1.6 완료 알림 (Runner → API)

```
POST /pipeline/jobs/{job_id}/notify
```

Runner가 작업 완료/에러 시 호출. 상태에 따라:
- `awaiting_approval`: 승인 요청 메시지 채팅 게시
- `done`: 5단계 검증 체크리스트 게시
- `error`: 에러 상세 + error_detail 게시

---

### 1.7 Lock 상태 조회

```
GET /pipeline/lock-status
```

프로젝트별 동시실행 Lock 현황 반환.

---

### 1.8 활성 작업 (Task Monitor)

```
GET /tasks/active?session_id={id}
```

`pipeline_jobs` + `directive_lifecycle` 통합 조회. 응답 필드: `task_id`, `project`, `title`, `pipeline`, `phase`, `status`, `elapsed_sec`, `created_at`

---

### 1.9 작업 로그 조회

```
GET /tasks/{task_id}/logs?last_n=50&since=&log_type=
```

| 파라미터 | 타입 | 기본값 | 범위 |
|---------|------|--------|------|
| `last_n` | integer | 50 | 1~200 |
| `since` | string | "" | ISO timestamp |
| `log_type` | string | "" | 로그 유형 필터 |

---

### 1.10 실시간 로그 스트림 (SSE)

```
GET /tasks/{task_id}/stream
```

- Media type: `text/event-stream`
- Keepalive: 20초
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`

---

## 2. 채팅 AI 도구 파라미터

### 2.1 pipeline_runner_submit

채팅 AI가 코드 수정 작업을 러너에 제출하는 도구.

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---------|------|------|--------|------|
| `project` | string (enum) | ✅ | — | KIS, GO100, SF, NTV2, AADS |
| `instruction` | string | ✅ | — | Claude Code 작업 지시 |
| `max_cycles` | integer | — | 3 | 검수 반복 횟수 |
| `session_id` | string | — | (자동 감지) | 채팅 세션 ID. 미지정 시 `current_chat_session_id` ContextVar → DB 최근 세션 조회 |
| `size` | string (enum) | — | "M" | XS/S/M/L/XL. worker_model 지정 시 무시 |
| `worker_model` | string | — | — | 모델 직접 지정. Claude: `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`. LiteLLM: `litellm:gemini-2.5-flash`, `litellm:deepseek-chat`, `litellm:qwen3-235b` |
| `parallel_group` | string | — | — | 병렬 그룹 — 같은 그룹의 작업은 프로젝트 Lock 우회 |
| `depends_on` | string | — | — | 선행 작업 job_id (done 완료 후에만 실행) |

**서버 매핑**: AADS→68서버, KIS/GO100→211서버, SF/NTV2→114서버

**Executor 내부**: `POST http://localhost:8080/api/v1/pipeline/jobs` (httpx timeout 10초)

---

### 2.2 pipeline_runner_submit_batch

여러 작업을 의존성 그래프로 동시 제출하는 도구.

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---------|------|------|--------|------|
| `project` | string (enum) | ✅ | — | 프로젝트명 |
| `jobs` | array | ✅ | — | 작업 배열 (1~20) |
| `parallel_group` | string | — | `batch-{uuid}` | 병렬 그룹 |
| `max_cycles` | integer | — | 3 | 검수 반복 |
| `session_id` | string | — | (자동 감지) | 채팅 세션 ID |

**jobs 아이템**: `key` (필수), `instruction` (필수), `size`, `worker_model`, `depends_on_key`

**Executor 내부**: `POST http://localhost:8080/api/v1/pipeline/jobs/batch` (httpx timeout 15초)

---

### 2.3 pipeline_runner_status

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `job_id` | string | — | 특정 작업 ID (미지정 시 전체 목록) |
| `status` | string | — | 상태 필터: `queued`, `running`, `awaiting_approval`, `done`, `error` |

**error_detail 값**: `timeout`, `claude_code_crash`, `git_conflict`, `build_fail`, `disk_full`, `rate_limit`, `process_died`

---

### 2.4 pipeline_runner_approve

| 파라미터 | 타입 | 필수 | 설명 |
|---------|------|------|------|
| `job_id` | string | ✅ | 작업 ID |
| `action` | string | ✅ | `approve` 또는 `reject` |
| `feedback` | string | — | 거부 시 피드백 (수정 지시) |

---

### 2.5 레거시 리다이렉트

| 레거시 도구명 | → 실제 도구 |
|--------------|-------------|
| `pipeline_c_start` | `pipeline_runner_submit` |
| `pipeline_c_status` | `pipeline_runner_status` |
| `pipeline_c_approve` | `pipeline_runner_approve` |
| `pipeline_c_reject` | `pipeline_runner_approve` (action=reject) |

---

## 3. DB 스키마

### 3.1 pipeline_jobs 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `job_id` | VARCHAR (PK) | `runner-{uuid4.hex[:8]}` 형식 |
| `project` | VARCHAR | AADS/KIS/GO100/SF/NTV2 |
| `instruction` | TEXT | 작업 지시 원문 (max 50,000자) |
| `instruction_hash` | VARCHAR(16) | SHA-256 앞 16자 (중복 검사용) |
| `chat_session_id` | UUID | 채팅 세션 FK |
| `status` | VARCHAR | 상태 (아래 enum 참조) |
| `phase` | VARCHAR | 세부 단계 |
| `cycle` | INTEGER | 현재 검수 사이클 |
| `max_cycles` | INTEGER | 최대 검수 사이클 (1~10, 기본 3) |
| `model` | VARCHAR | 실행 모델 ID |
| `worker_model` | VARCHAR | 사용자 지정 모델 |
| `parallel_group` | VARCHAR | 병렬 그룹 ID |
| `depends_on` | VARCHAR | 선행 작업 job_id |
| `runner_pid` | INTEGER | 원격 프로세스 PID |
| `result_output` | TEXT | Claude Code 출력 (max 6,000자) |
| `git_diff` | TEXT | git diff 전문 (max 50,000자) |
| `review_feedback` | TEXT | AI 검수 피드백 |
| `error_detail` | VARCHAR | 에러 유형 분류 |
| `created_at` | TIMESTAMP | 생성 시각 |
| `started_at` | TIMESTAMP | 실행 시작 시각 |
| `updated_at` | TIMESTAMP | 최종 갱신 시각 |

### 3.2 status 값 (상태 전이)

| status | 설명 | 다음 상태 |
|--------|------|----------|
| `queued` | 대기 중 | → claimed |
| `claimed` | 러너가 클레임 | → running |
| `running` | Claude Code 실행 중 | → awaiting_approval, error |
| `awaiting_approval` | CEO 승인 대기 | → approved, rejected |
| `approved` | 승인됨 | → deploying |
| `deploying` | 배포 중 | → done, error |
| `done` | 완료 | (최종) |
| `rejected` | 거부됨 | → rolling_back |
| `rolling_back` | 원복 중 | → rejected_done |
| `rejected_done` | 원복 완료 | (최종) |
| `error` | 에러 | (최종) |

### 3.3 phase 값

`queued`, `parallel_start`, `claude_code_work`, `claude_code_detached`, `claude_code_done`, `ai_review`, `review_pass`, `review_delegated`, `revision`, `max_cycles`, `awaiting_approval`, `aads_pre_restart`, `restarting`, `deploying`, `push_done`, `verifying`, `done`, `error`, `stall_detected`, `cancelled`

### 3.4 approval_queue 테이블 (Watchdog 전용)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | SERIAL (PK) | — |
| `error_log_id` | INTEGER | 에러 로그 FK |
| `title` | VARCHAR | 승인 요청 제목 |
| `description` | TEXT | 장애 설명 |
| `suggested_action` | TEXT | 권장 조치 |
| `action_type` | VARCHAR | `auto_command`, `claude_code`, `manual` |
| `action_command` | TEXT | 실행 명령 |
| `target_server` | VARCHAR | `68`, `211`, `114`, `NAS` |
| `severity` | VARCHAR | `critical`(0), `high`(1), `medium`(2), `low`(3) |
| `status` | VARCHAR | `pending`, `approved`, `rejected`, `executed`, `failed` |
| `telegram_message_id` | INTEGER | Telegram 메시지 ID |
| `requested_at` | TIMESTAMP | 요청 시각 |
| `responded_at` | TIMESTAMP | 응답 시각 |
| `executed_at` | TIMESTAMP | 실행 시각 |
| `execution_result` | TEXT | 실행 결과 (max 2,000자) |

### 3.5 task_logs 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | SERIAL (PK) | — |
| `task_id` | VARCHAR | 작업 ID |
| `log_type` | VARCHAR | 로그 유형 |
| `content` | TEXT | 로그 내용 |
| `phase` | VARCHAR | 작업 단계 |
| `metadata` | JSONB | 추가 메타데이터 |
| `created_at` | TIMESTAMP | 생성 시각 |

---

## 4. 환경변수

### 4.1 Shell Runner 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `PGHOST` | `localhost` | PostgreSQL 호스트 |
| `PGPORT` | `5433` | PostgreSQL 포트 |
| `PGUSER` | `aads` | PostgreSQL 유저 |
| `PGDATABASE` | `aads` | PostgreSQL DB명 |
| `POLL_INTERVAL` | `5` (초) | DB 폴링 주기 |
| `AADS_API_URL` | `http://127.0.0.1:8100` | aads-server API |
| `MAX_RUNTIME` | `7200` (초) | Claude 프로세스 최대 실행 시간 |
| `MAX_JOB_RUNTIME` | `3600` (초) | 단일 작업 최대 시간 |
| `MAX_RETRIES` | `2` | Claude 실패 시 재시도 |
| `MAX_CONCURRENT_PER_PROJECT` | `3` | 프로젝트당 최대 동시 실행 |
| `MAX_CONCURRENT_GLOBAL` | `10` | 전역 최대 동시 작업 |
| `APPROVAL_TIMEOUT_HOURS` | `24` | 승인 대기 타임아웃 |
| `ARTIFACT_MAX_AGE_HOURS` | `24` | 임시파일 보존 시간 |
| `WATCHDOG_INTERVAL` | `300` (초) | 프로세스 생존 확인 주기 |
| `STUCK_CHECK_INTERVAL` | `300` (초) | 좀비/stuck 감지 주기 |
| `MIN_DISK_GB` | `1` | 최소 디스크 공간 |
| `DB_MODE` | `auto` | `docker` 또는 `psql` |
| `PG_CONTAINER` | `aads-postgres` | Docker 컨테이너명 |
| `RUNNER_PROJECTS` | (전체) | 처리할 프로젝트 목록 (쉼표 구분) |
| `RUNNER_HOSTNAME` | `$(hostname -s)` | 호스트명 |

### 4.2 인증 환경변수

| 변수 | 용도 |
|------|------|
| `ANTHROPIC_AUTH_TOKEN` | 1계정 (Naver) OAuth 토큰 |
| `ANTHROPIC_AUTH_TOKEN_2` | 2계정 (Gmail) OAuth 토큰 |
| `ANTHROPIC_API_KEY` | 런타임 주입 — OAuth 토큰 복사 (CLI 호환) |
| `TELEGRAM_BOT_TOKEN` | Telegram 알림 봇 토큰 |
| `TELEGRAM_CHAT_ID` | CEO Telegram 채팅 ID |

### 4.3 Python Orchestrator 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LITELLM_BASE_URL` | `http://aads-litellm:4000` | LiteLLM 프록시 |
| `LITELLM_MASTER_KEY` | `sk-litellm` | LiteLLM API 키 |
| `AADS_API_BASE` | `http://localhost:8080` | 내부 API 베이스 |

### 4.4 systemd EnvironmentFile

`/root/.config/aads-runner.env` — 민감 정보(토큰 등) 별도 관리

---

## 5. 사용 예시

### 5.1 단건 작업 제출 (채팅 AI 도구)

```
pipeline_runner_submit(
    project="AADS",
    instruction="app/api/health.py에서 응답에 uptime 필드를 추가해주세요",
    size="S"
)
```

→ 결과: `{ "job_id": "runner-a1b2c3d4", "status": "queued" }`

### 5.2 모델 직접 지정

```
pipeline_runner_submit(
    project="KIS",
    instruction="전체 백테스트 로직을 리팩토링하고 성능 최적화",
    worker_model="claude-opus-4-6"
)
```

### 5.3 LiteLLM 모델 사용

```
pipeline_runner_submit(
    project="GO100",
    instruction="README.md 업데이트",
    worker_model="litellm:gemini-2.5-flash"
)
```

### 5.4 배치 병렬 실행

```
pipeline_runner_submit_batch(
    project="AADS",
    jobs=[
        { "key": "backend", "instruction": "API 엔드포인트 추가", "size": "M" },
        { "key": "frontend", "instruction": "대시보드 UI 수정", "size": "M" },
        { "key": "test", "instruction": "통합 테스트 작성", "size": "S", "depends_on_key": "backend" }
    ]
)
```

→ backend + frontend 병렬 실행, test는 backend 완료 후 실행

### 5.5 의존성 체이닝

```
# 1단계: 스키마 마이그레이션
pipeline_runner_submit(project="AADS", instruction="DB 마이그레이션 작성", size="M")
→ job_id: "runner-step1"

# 2단계: API 구현 (1단계 완료 후)
pipeline_runner_submit(project="AADS", instruction="새 API 구현", depends_on="runner-step1")
```

### 5.6 상태 조회

```
pipeline_runner_status()                          # 전체 목록 (최근 10건)
pipeline_runner_status(job_id="runner-a1b2c3d4")  # 특정 작업
pipeline_runner_status(status="running")          # running 필터
```

### 5.7 승인/거부

```
pipeline_runner_approve(job_id="runner-a1b2c3d4", action="approve")
pipeline_runner_approve(job_id="runner-a1b2c3d4", action="reject", feedback="에러 처리 추가 필요")
```

### 5.8 REST API 직접 호출

```bash
# 작업 제출
curl -X POST http://localhost:8080/api/v1/pipeline/jobs \
  -H "Content-Type: application/json" \
  -H "x-monitor-key: internal-pipeline-call" \
  -d '{"project":"AADS","instruction":"버그 수정","session_id":"uuid-here","size":"S"}'

# 상태 조회
curl http://localhost:8080/api/v1/pipeline/jobs?status=running&limit=5

# 승인
curl -X POST http://localhost:8080/api/v1/pipeline/jobs/runner-abc123/approve \
  -H "Content-Type: application/json" \
  -d '{"action":"approve"}'
```

---

## 6. 트러블슈팅 가이드

### 6.1 에러 유형별 원인/조치

| error_detail | 원인 | 조치 |
|-------------|------|------|
| `timeout` | MAX_RUNTIME(7,200초) 초과 | instruction 분할 또는 size 축소 |
| `claude_code_crash` | Claude CLI 비정상 종료 | 로그 확인 (`/tmp/aads_pipeline_artifacts/{job_id}.err`), 재시도 |
| `git_conflict` | merge conflict 발생 | 수동 conflict 해결 후 재제출 |
| `build_fail` | 빌드/컴파일 에러 | 코드 수정 후 재제출 |
| `disk_full` | 디스크 공간 부족 | `du -sh /tmp/aads*`로 확인, artifact 정리 |
| `rate_limit` | API 레이트 리밋 | 대기 후 자동 재시도 (6단계 폴백) |
| `process_died` | 프로세스 unexpectedly 종료 | systemd 재시작 확인: `systemctl status aads-pipeline-runner` |
| `auth_error` | OAuth 토큰 만료/무효 | `~/.claude/api_keys.env` 확인 |
| `oom_killed` | 메모리 초과 (kill -9) | 서버 메모리 확인, instruction 축소 |
| `stale_recovered` | 러너 크래시 복구 시 정리된 작업 | 자동 — 필요 시 재제출 |

### 6.2 작업이 queued에서 진행되지 않을 때

1. **러너 프로세스 확인**: `systemctl status aads-pipeline-runner`
2. **프로젝트 Lock 확인**: `GET /pipeline/lock-status` — 다른 작업이 running 중이면 대기
3. **depends_on 확인**: 선행 작업이 아직 done이 아니면 대기
4. **RUNNER_PROJECTS 확인**: systemd 환경변수에 해당 프로젝트가 포함되어 있는지

### 6.3 awaiting_approval 작업이 보이지 않을 때

1. **NOTIFY_AI 버그 확인**: job_id가 `runner-` 패턴인지 (AADS-232 수정 적용 확인)
2. **Telegram 봇 확인**: `ps aux | grep tg_approval_bot`
3. **수동 확인**: `pipeline_runner_status(status="awaiting_approval")`

### 6.4 배포 실패 시

1. **Health check 로그**: `/var/log/aads-pipeline/runner.log`에서 health URL 응답 확인
2. **자동 롤백 확인**: `git log -3` — revert 커밋 존재 여부
3. **수동 롤백**: 서비스별 재시작 명령 실행

### 6.5 좀비 프로세스 확인/정리

```bash
# 러너 프로세스 확인
ps aux | grep pipeline-runner.sh

# 좀비 Claude 프로세스
ps aux | grep "claude.*-p"

# Lock 파일 확인
ls -la /tmp/pipeline-runner.lock /tmp/pipeline-deploy-*.lock 2>/dev/null

# systemd 재시작 (좀비 자동 정리)
systemctl restart aads-pipeline-runner
```

### 6.6 주요 DB 진단 쿼리

```sql
-- 상태별 작업 수
SELECT status, COUNT(*) FROM pipeline_jobs GROUP BY status ORDER BY COUNT(*) DESC;

-- 최근 1시간 에러
SELECT job_id, project, error_detail, review_feedback, updated_at
FROM pipeline_jobs WHERE status = 'error' AND updated_at > NOW() - interval '1 hour'
ORDER BY updated_at DESC;

-- 프로젝트별 평균 실행 시간
SELECT project,
       AVG(EXTRACT(EPOCH FROM (updated_at - started_at)))::int AS avg_seconds,
       COUNT(*) AS total
FROM pipeline_jobs WHERE status = 'done' AND started_at IS NOT NULL
GROUP BY project;

-- 모델별 성공/실패율
SELECT model,
       COUNT(*) FILTER (WHERE status = 'done') AS success,
       COUNT(*) FILTER (WHERE status = 'error') AS errors,
       COUNT(*) AS total
FROM pipeline_jobs WHERE model IS NOT NULL
GROUP BY model ORDER BY total DESC;
```

---

## 부록: 타임아웃 상수 종합

| 상수 | 값 | 출처 |
|------|-----|------|
| Claude CLI MAX_RUNTIME | 7,200초 (2h) | pipeline-runner.sh |
| 사이즈별 XS | 600초 (10m) | pipeline_runner_service.py |
| 사이즈별 S | 1,200초 (20m) | pipeline_runner_service.py |
| 사이즈별 M | 3,600초 (60m) | pipeline_runner_service.py |
| 사이즈별 L | 5,400초 (90m) | pipeline_runner_service.py |
| 사이즈별 XL | 7,200초 (120m) | pipeline_runner_service.py |
| AI Review API 타임아웃 | 30초 | pipeline-runner.sh |
| SSH 재시도 base delay | 2초 (exp backoff) | pipeline_runner_service.py |
| SSH 최대 재시도 | 3회 | pipeline_runner_service.py |
| 승인 대기 타임아웃 | 24시간 | pipeline-runner.sh |
| Stall 감지 임계값 | 1,800초 (30m) | pipeline_runner_service.py |
| Watchdog 루프 | 120초 | pipeline_runner_service.py |
| AADS restart debounce | 30초 | pipeline_runner_service.py |
| Tool executor HTTP 타임아웃 | 10초 (단건), 15초 (배치) | tool_executor.py |
| 배포 flock 대기 | 300초 | pipeline-runner.sh |
