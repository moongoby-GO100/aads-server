# Pipeline Runner 아키텍처 문서

**버전**: v1.0  
**최종 수정일**: 2026-04-09  
**작성자**: AADS AI (소스코드 실측 기반)  
**관련 소스**: 10개 파일, 8,000+ 줄

---

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [전체 아키텍처 다이어그램](#2-전체-아키텍처-다이어그램)
3. [실행 흐름 (7단계 상세)](#3-실행-흐름)
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

Pipeline Runner는 **CEO/채팅 AI가 제출한 코드 수정 작업을 자율적으로 실행·검수·배포하는 CI/CD 파이프라인**이다. Claude Code CLI를 원격 SSH로 호출하여 코드를 수정하고, AI가 자동 검수한 뒤, CEO 승인 후 push + 배포까지 완전 자동화한다.

### 설계 원칙

- **코드 수정만 → 승인 → 커밋 → 푸시 → 빌드 → 배포** — Claude Code CLI는 코드 수정만 수행, 커밋/푸시/빌드/배포는 CEO 승인 후 Runner가 처리 (H7 가드)
- **6단계 모델+계정 폴백** — 실패 시 모델과 OAuth 계정을 번갈아 재시도
- **원자적 Job 클레임** — `FOR UPDATE SKIP LOCKED` (C4)으로 동시 러너 간 중복 방지
- **크래시 복구** — 시작 시 stuck 작업 자동 정리 (C3)
- **SQL 인젝션 방지** — dollar-quoting + UUID 포맷 검증 (C1)

### 2계층 구조

| 계층 | 파일 | 역할 |
|------|------|------|
| **Python API** (DB 기반) | `pipeline_runner.py`, `pipeline_runner_service.py` | REST API, 오케스트레이터, 상태 관리 |
| **Shell Runner** (파일시스템 기반) | `pipeline-runner.sh` (systemd) | DB 폴링, Claude CLI 실행, git 조작, 배포 |

두 계층이 **동일한 `pipeline_jobs` DB 테이블**을 공유하며, 현재는 Shell Runner가 주된 실행기로 동작한다.

---

## 2. 전체 아키텍처 다이어그램

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      CEO / 채팅 AI                                      │
│              pipeline_runner_submit()                                    │
│              pipeline_runner_submit_batch()                              │
└──────────────────┬──────────────────────────────────────────────────────┘
                   │ HTTP POST /api/v1/pipeline/jobs
                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  API Layer — app/api/pipeline_runner.py (612줄)                          │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────────┐  │
│  │ submit_job   │ │ list_jobs    │ │ approve_or_  │ │ submit_batch   │  │
│  │ POST /jobs   │ │ GET /jobs    │ │ reject       │ │ POST /jobs/    │  │
│  │              │ │              │ │ POST /jobs/  │ │ batch          │  │
│  │ 중복검사     │ │ 필터/페이징  │ │ {id}/approve │ │ 의존성그래프   │  │
│  │ 크기추정     │ │              │ │              │ │ auto parallel  │  │
│  │ pg_notify    │ │              │ │ autonomy_gate│ │                │  │
│  └──────┬───────┘ └──────────────┘ └──────────────┘ └────────────────┘  │
│         │ INSERT pipeline_jobs (status=queued)                           │
│         │ SELECT pg_notify('pipeline_new_job', $job_id)                  │
└─────────┼───────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Orchestrator — app/services/pipeline_runner_service.py (2,216줄)        │
│  ┌─────────────────────────────────────────────────────────────────┐     │
│  │ PipelineCJob 클래스                                             │     │
│  │  _run_inner()  → Claude Code SSH 실행                           │     │
│  │  _ai_review()  → 독립 LLM 검수 (sonnet-4-6)                    │     │
│  │  approve()     → git push + restart + verify                    │     │
│  │  reject()      → git checkout 원복                              │     │
│  └─────────────────────────────────────────────────────────────────┘     │
│  Watchdog: 120초 주기, stall 30분 감지, awaiting_approval 24시간 만료   │
└─────────┬───────────────────────────────────────────────────────────────┘
          │ (Python API 경로) 또는
          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Shell Runner — scripts/pipeline-runner.sh (1,570줄)                     │
│  systemd: aads-pipeline-runner.service (Restart=always, RestartSec=10)   │
│  ┌─────────────────────────────────────────────────────────────────┐     │
│  │ main() 루프                                                     │     │
│  │  1. DB 폴링 (5초) — queued/approved/rejected 감지               │     │
│  │  2. claim_queued_job() — FOR UPDATE SKIP LOCKED                  │     │
│  │  3. run_job() — 6단계 모델+계정 폴백, H7 가드 주입              │     │
│  │  4. AI Review — POST /api/v1/review/code-diff                   │     │
│  │  5. deploy_job() — flock, worktree 머지, push, 재시작           │     │
│  │  6. reject_job() — worktree 삭제 / git stash                    │     │
│  └──────────────────────────────────────┬──────────────────────────┘     │
│  Lock: flock /tmp/pipeline-runner.lock  │                                │
│  Log: /var/log/aads-pipeline/runner.log │                                │
└─────────────────────────────────────────┼───────────────────────────────┘
                                          │ SSH + claude CLI
                                          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Claude Code CLI                                                         │
│  claude --model {model} -p --output-format text "{instruction}"          │
│  또는 LiteLLM: python3 scripts/litellm_runner.py --model {model} ...    │
│  출력: /tmp/aads_pipeline_artifacts/{job_id}.out/.err                    │
└──────────────────────────────────────────────────────────────────────────┘
          │ 완료
          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AI Code Review → Telegram 승인 → CEO 승인/거부                         │
│  POST /api/v1/review/code-diff (30초 타임아웃)                           │
│  tg_approval_bot.py — 인라인 버튼으로 승인/거부                         │
└──────────────────────────────────────────────────────────────────────────┘
          │ 승인
          ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Deploy                                                                  │
│  AADS: deploy.sh bluegreen + hot-reload                                  │
│  KIS:  systemctl restart kis-v41-api                                     │
│  GO100: systemctl restart go100 + frontend build/swap                    │
│  SF:   docker restart shortflow-worker/dashboard                         │
│  NTV2: php artisan optimize + frontend build + docker restart reverb     │
│  Health check: 10초 간격, 3회 재시도, 실패 시 git revert 자동 롤백       │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 실행 흐름

### 상태 전이도

```
queued → claimed → running → awaiting_approval → approved → deploying → done
                           ↘ error                        ↘ rejected → rolling_back → rejected_done
```

### 7단계 상세

#### 1단계: SUBMIT (queued)

- **트리거**: `POST /api/v1/pipeline/jobs` 또는 `POST /api/v1/pipeline/jobs/batch`
- **중복 검사**: `SHA-256("{project}:{instruction}")` 앞 16자 해시 → 10분 내 done이면 차단, 30분 내면 경고
- **크기 추정** (`_estimate_size`): 키워드·파일참조·길이 휴리스틱으로 XS/S/M/L/XL 자동 분류
- **DB 저장**: `pipeline_jobs` INSERT (status=queued)
- **알림**: `SELECT pg_notify('pipeline_new_job', $job_id)`

#### 2단계: CLAIM (claimed → running)

- **Shell Runner**: `claim_queued_job()` — `UPDATE ... SET status='claimed' ... FOR UPDATE SKIP LOCKED ... RETURNING`
- **프로젝트 Lock 확인**: `parallel_group` 없으면 프로젝트당 최대 1개 동시 실행
- **의존성 확인**: `depends_on` 잡이 `done`이 아니면 스킵
- **사전 검증** (`pre_validate`): workdir 존재, 디스크 공간 ≥ 1GB, git dirty → stash

#### 3단계: RUNNING (Claude Code 실행)

- **H7 가드 주입**: instruction 앞에 빌드/배포 금지 규칙 삽입
- **Claude CLI 호출**:
  ```bash
  timeout $MAX_RUNTIME claude --model $model -p --output-format text "$instruction"
  ```
- **LiteLLM 모델 시**: `python3 scripts/litellm_runner.py --model $model ...`
- **6단계 폴백**: Sonnet(계정1) → Sonnet(계정2) → Opus(계정1) → Opus(계정2) → Haiku(계정1) → Haiku(계정2)
- **타임아웃**: 기본 7,200초 (2시간), 사이즈별 XS=600s ~ XL=7,200s
- **PID 추적**: `pipeline_jobs.runner_pid`에 기록 → watchdog이 프로세스 생존 확인

#### 4단계: AI_REVIEW (AI 검수)

- **Shell Runner**: `git diff HEAD` 생성 후 git diff 형식 사전검사, 유효 시 `POST /api/v1/review/code-diff` (30초 타임아웃)
- **Reviewer 모델**: `runner_model_config.size='AI_REVIEW'` 우선순위 조회, 미설정 시 `qwen-turbo`
- **사전분류**: 인증 실패, 실행 오류, `git diff` 수집 실패, 비정상 입력을 LLM 호출 전 `FLAG` 카테고리로 분리
- **판정값**: `APPROVE` / `REQUEST_CHANGES` / `FLAG` / `SKIP`
- **FLAG 메타데이터**: `flag_category`, `failure_stage`, `needs_retry`

#### 5단계: AWAITING_APPROVAL (CEO 승인 대기)

- diff 전문을 채팅방에 게시
- Telegram 인라인 버튼으로 승인 요청
- **타임아웃**: 24시간 후 자동 만료 (error 처리)
- 채팅 도구: `pipeline_runner_approve(job_id, action='approve'|'reject')`

#### 6단계: DEPLOYING (승인 후 배포)

- **Shell Runner**: `claim_approved_job()` → flock 배포 잠금 → worktree 머지 → git add/commit/push
- **프로젝트별 재시작** (상세: [10. 서버 매핑](#10-서버-매핑))
- **Health check**: 10초 간격, 최대 3회 재시도
- **실패 시**: `git revert HEAD` 자동 롤백 후 재배포
- **자율 평가**: `autonomy_gate.record_task_result` — 승인=pass, 거부=fail

#### 7단계: DONE / REJECTED

- **DONE**: 채팅방에 5단계 검증 체크리스트 게시, 임시파일 정리
- **REJECTED**: git checkout 원복, worktree 삭제, 피드백과 함께 채팅방 알림
- **rejected_done**: 롤백 완료 최종 상태

---

## 4. 컴포넌트별 상세

### 4.1 API Layer — `app/api/pipeline_runner.py` (612줄)

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/pipeline/jobs` | POST | 단건 작업 제출 |
| `/pipeline/jobs` | GET | 작업 목록 (status/project/session_id 필터, limit 1~100) |
| `/pipeline/jobs/{job_id}` | GET | 단건 상세 조회 |
| `/pipeline/jobs/{job_id}/notify` | POST | 러너 완료 시 AI 반응 트리거 |
| `/pipeline/jobs/{job_id}/approve` | POST | 승인 또는 거부 |
| `/pipeline/jobs/batch` | POST | 배치 작업 제출 (1~20건, 의존성 그래프) |
| `/pipeline/lock-status` | GET | 프로젝트 동시실행 Lock 현황 |

### 4.2 Orchestrator — `app/services/pipeline_runner_service.py` (2,216줄)

- **PipelineCJob 클래스**: 작업 생명주기 전체를 관리하는 메인 클래스
- **in-memory 상태**: `_active_jobs` dict, `_project_locks`, `_job_approve_locks`
- **Watchdog**: 120초 주기 백그라운드 태스크
  - stall 감지: 30분 무 로그 → 채팅 알림 (최대 3회) → auto-kill
  - `awaiting_approval` 24시간 만료
  - orphan 결과 수집: `.done` 파일 확인 → 채팅에 결과 게시
- **AADS 자기수정 특수처리**: 30초 debounce, pre-restart DB 저장, 30초 health 폴링

### 4.3 Shell Runner — `scripts/pipeline-runner.sh` (1,570줄)

- **systemd 서비스**: `Restart=always`, `RestartSec=10`, `WorkingDirectory=/root/aads/aads-server`
- **메인 루프**: DB 폴링 (POLL_INTERVAL=5초) → queued/approved/rejected 순차 처리
- **적응형 폴링**: 작업 발견 시 1초 대기, 유휴 시 5초 대기
- **함수 42개**: claim, run, deploy, reject, watchdog, cleanup 등

### 4.4 Telegram 봇 — `scripts/tg_approval_bot.py` (293줄)

- Long polling 방식 (30초 timeout)
- 인라인 버튼: approve/ignore
- `ALLOWED_CHAT_ID`로 CEO만 응답 가능
- `/status`, `/test_alert` 명령어

### 4.5 Auto Trigger — `scripts/auto_trigger.sh` (961줄)

- **레거시 파일시스템 기반** 지시서(.md) 감지 → `claude_exec.sh` 실행
- **5단계 우선순위**: P0 파일명 → P0 내용 → P1 파일명 → P1 내용 → P2 (impact/effort 정렬)
- Pipeline Runner의 DB 기반 방식과 병행 운영

### 4.6 Task Monitor — `app/api/task_monitor.py` (169줄)

- **SSE 스트리밍**: `GET /tasks/{task_id}/stream` (20초 keepalive)
- **로그 조회**: `GET /tasks/{task_id}/logs` (last_n, since, log_type 필터)
- **활성 작업**: pipeline_jobs + directive_lifecycle 통합 조회

---

## 5. 병렬 실행

### parallel_group

- **같은 `parallel_group`** 내 작업 = 프로젝트 Lock 우회, 동시 실행
- **다른 그룹** 또는 그룹 없음 = 기존 프로젝트 Lock으로 직렬화
- **최대 동시 실행**: 프로젝트당 `MAX_CONCURRENT_PER_PROJECT=3`, 전역 `MAX_CONCURRENT_GLOBAL=10`

### depends_on (의존성 체이닝)

```
작업 A (runner-001) ──done──→ 작업 B (depends_on=runner-001) ──done──→ 작업 C (depends_on=runner-002)
```

- `depends_on` 잡이 `done`이 아니면 `claim_queued_job`에서 스킵
- 선행 작업 완료 시 `promote_next_queued()`가 후속 작업 승격

### 배치 API

- `POST /api/v1/pipeline/jobs/batch` — 1~20개 작업 동시 제출
- 자동 `parallel_group`: 미지정 시 `batch-{uuid[:8]}` 생성
- 작업 간 `depends_on_key` → 실제 `job_id`로 자동 변환
- 채팅 도구: `pipeline_runner_submit_batch`

### Git Worktree (병렬 실행 시)

- 조건: `MAX_CONCURRENT_PER_PROJECT > 1` 이고 `/tmp` 여유 5GB 이상
- 경로: `/tmp/aads-wt-{job_id}`
- 브랜치: `worktree/{group_id}/{task_id}_{PID}`
- 배포 시: 3-way merge로 main workdir에 통합 후 삭제

---

## 6. 모델 라우팅

### 사이즈→모델 자동 매핑

| Size | Model | 타임아웃 |
|------|-------|---------|
| XS | `claude-haiku-4-5-20251001` | 600초 (10분) |
| S | `claude-haiku-4-5-20251001` | 1,200초 (20분) |
| M | `claude-sonnet-4-6` | 3,600초 (60분) |
| L | `claude-sonnet-4-6` | 5,400초 (90분) |
| XL | `claude-opus-4-6` | 7,200초 (120분) |

### 모델 선택 우선순위

1. `worker_model` 직접 지정 (최우선)
2. 명시적 `size` 파라미터
3. instruction 텍스트에서 `_parse_size_from_instruction` 파싱
4. `_estimate_size` 휴리스틱 자동 분류

### 6단계 모델+계정 폴백 (Shell Runner)

```
시도1: claude-sonnet-4-6 + 계정1(Naver)
시도2: claude-sonnet-4-6 + 계정2(Gmail)
시도3: claude-opus-4-6   + 계정1(Naver)
시도4: claude-opus-4-6   + 계정2(Gmail)
시도5: claude-haiku-4-5  + 계정1(Naver)
시도6: claude-haiku-4-5  + 계정2(Gmail)
```

재시도 대기: `3 + attempt × 2`초 (최대 ~15초)

### LiteLLM Runner

`worker_model`이 `litellm:` 접두사인 경우:
- `python3 scripts/litellm_runner.py --model {model_name} --instruction {instruction} --workdir {workdir}`
- 폴백 없이 1회만 시도

### _estimate_size 휴리스틱 (AADS-229)

- **S**: simple 키워드 2개+ 또는 (길이 < 200 + complex 키워드 0 + 파일참조 ≤ 1)
- **XL**: complex 키워드 3개+ 또는 파일참조 10개+ 또는 길이 > 5,000
- **L**: complex 키워드 2개+ 또는 파일참조 5개+ 또는 길이 > 3,000
- **M**: 나머지

Complex 키워드: 리팩토링, 마이그레이션, 아키텍처, 전체, refactor, migration, architecture, all files, 전수, 대규모  
Simple 키워드: 오타, typo, 주석, comment, 버전, version, 설정 변경, config, 로그, 1줄, 한 줄

---

## 7. Lock 메커니즘

### 3단계 Lock

| 레벨 | 방식 | 대상 | 파일/키 |
|------|------|------|---------|
| **프로세스** | flock | 러너 중복 실행 방지 | `/tmp/pipeline-runner.lock` |
| **작업** | Redis HTTP API | 프로젝트 단위 작업 잠금 | `POST /api/v1/ops/locks/work/acquire?project=&session_id=` |
| **배포** | flock + Redis | 동일 프로젝트 동시 배포 방지 | `/tmp/pipeline-deploy-{project}.lock` (300초 대기) |

### DB 원자적 클레임 (C4)

```sql
UPDATE pipeline_jobs
SET status = 'claimed', started_at = NOW()
WHERE job_id = (
    SELECT job_id FROM pipeline_jobs
    WHERE status = 'queued'
    FOR UPDATE SKIP LOCKED
    LIMIT 1
) RETURNING job_id, project, instruction, ...
```

### Lock 해제 조건

- 작업 완료 (done/error) → 자동 해제
- `promote_next_queued()` → 다음 queued 작업 승격
- stuck 복구: running 60분, deploying 10분, awaiting_approval 24시간 초과 시 강제 해제

---

## 8. AI 코드 검수

### Shell Runner 검수 흐름

1. Claude Code 성공 후 `git diff HEAD` 캡처 (최대 50,000자)
2. `POST /api/v1/review/code-diff` 전송 (30초 타임아웃)
3. 응답 verdict: `APPROVE` / `REQUEST_CHANGES` / `FLAG`

### Python Orchestrator 검수 흐름

1. `call_background_llm` (model: `claude-sonnet-4-6`)
2. **5단계 JSON 파싱 폴백**:
   1. 코드 펜스 제거
   2. 직접 `json.loads`
   3. Regex `{...}` 블록 추출
   4. 줄별 JSON 스캔
   5. 키워드 기반 verdict 추출 (PASS/통과/정상 → PASS)
3. 파싱 실패 시 → `DELEGATED` (채팅 AI에 위임)

### 검수 기준

- 코드 변경이 instruction과 일치하는지
- 보안 취약점 여부
- git diff가 비어있지 않은지

---

## 9. 승인 흐름

### Pipeline Runner 승인

1. AI 검수 완료 → `awaiting_approval` 전환
2. diff 요약 + 전문을 채팅방에 게시
3. Telegram 인라인 버튼 전송 (tg_approval_bot.py)
4. CEO 응답:
   - **승인** (`pipeline_runner_approve(job_id, action='approve')`) → 배포 시작
   - **거부** (`pipeline_runner_approve(job_id, action='reject', feedback='...')`) → 원복

### 승인 경합 방지 (H-11)

- `_job_approve_locks`: 작업별 asyncio.Lock
- 동일 job_id에 대한 동시 approve/reject 호출 직렬화

### Watchdog 승인

- `approval_queue` 테이블 기반 (별도 시스템)
- 장애 감지 → Telegram 버튼 → CEO 클릭 → 복구 명령 실행
- Safe prefixes: `docker restart`, `systemctl restart`, `supervisorctl restart`, `curl`, `npm run build`, `pm2 restart`

---

## 10. 서버 매핑

### 프로젝트→서버 매핑

| 프로젝트 | 서버 | IP | workdir |
|----------|------|-----|---------|
| AADS | 서버68 | 68.183.183.11 | `/root/aads/aads-server` |
| KIS | 서버211 | 211.188.51.113 | `/root/webapp` |
| GO100 | 서버211 | 211.188.51.113 | `/root/kis-autotrade-v4` |
| SF | 서버114 | 116.120.58.155 | `/data/shortflow` |
| NTV2 | 서버114 | 116.120.58.155 | `/srv/newtalk-v2` |

### 프로젝트별 배포 방식 및 Health Check

| 프로젝트 | 배포 명령 | Health URL | 롤백 |
|----------|----------|------------|------|
| AADS | `bash deploy.sh bluegreen` + hot-reload API | `http://localhost:8100/api/v1/health` | git revert + 재배포 |
| AADS dashboard | `docker compose build/up aads-dashboard` + Visual QA | — | — |
| KIS | `systemctl restart kis-v41-api` (+ webapp 변경 시 `kis-webapp-api`) | `http://localhost:8003/health` | git revert + 재배포 |
| GO100 | `systemctl restart go100` + frontend npm build/swap | `http://localhost:8002/health` | git revert + 재배포 |
| SF | `docker restart shortflow-worker shortflow-dashboard` + saas-dashboard build/swap | `http://localhost:8000/health` | git revert + 재배포 |
| NTV2 | `php artisan optimize` + frontend build/swap + `docker restart newtalk-v2-reverb` | `http://localhost:8080` | git revert + 재배포 |

Health check: **10초 간격, 최대 3회 재시도**. 실패 시 `git revert HEAD` 자동 롤백 후 재배포.

---

## 11. 에러 처리 및 재시도

### 에러 분류 (`classify_error`)

| 에러 유형 | 조건 |
|----------|------|
| `timeout` | exit_code=124 또는 MAX_RUNTIME 초과 |
| `git_conflict` | stderr에 merge conflict 패턴 |
| `oom_killed` | exit_code=137 또는 Killed 패턴 |
| `auth_error` | 인증 관련 에러 메시지 |
| `rate_limit` | Rate limit / 429 패턴 |
| `disk_full` | No space left on device |
| `code_syntax_error` | SyntaxError / IndentationError |
| `build_fail` | Build failed / compile error |
| `permission_denied` | Permission denied |
| `network_error` | Connection refused / timeout |
| `unknown` | 기타 |

### 재시도 전략

- Shell Runner: `MAX_RETRIES=2` + 6단계 모델/계정 폴백 = 최대 6회 시도
- Python Orchestrator: SSH 최대 3회 재시도 (exponential backoff: 2s, 4s, 8s)
- 재시도 대기: `3 + attempt × 2`초

### Stuck 작업 복구 (`_recover_stuck_jobs`)

| 상태 | 타임아웃 | 조치 |
|------|---------|------|
| running/claimed | 60분 (MAX_JOB_RUNTIME=3,600초) | error 전환 + zombie kill |
| deploying | 10분 | error 전환 |
| awaiting_approval | 24시간 | error 전환 (타임아웃) |

### 실행 시간 알림

- 60분 초과: 텔레그램 경고 (중복 방지: `/tmp/runner_alert_{job_id}_60`)
- 120분 초과: 텔레그램 긴급 경고 (중복 방지: `/tmp/runner_alert_{job_id}_120`)

---

## 12. 운영 모니터링

### 로그 경로

| 로그 | 경로 |
|------|------|
| 메인 러너 로그 | `/var/log/aads-pipeline/runner.log` |
| 작업 stdout | `/tmp/aads_pipeline_artifacts/{job_id}.out` |
| 작업 stderr | `/tmp/aads_pipeline_artifacts/{job_id}.err` |
| auto_trigger 우선순위 | `/var/log/aads/auto_trigger_priority.log` |
| 투입 결정 | `/var/log/aads/trigger_decisions.log` |
| systemd 저널 | `journalctl -u aads-pipeline-runner` |

### SSE 모니터링

- 실시간 로그: `GET /api/v1/tasks/{task_id}/stream`
- 활성 작업 목록: `GET /api/v1/tasks/active?session_id={id}`

### DB 쿼리 (운영 확인용)

```sql
-- 전체 작업 현황
SELECT status, COUNT(*) FROM pipeline_jobs GROUP BY status;

-- 실행 중 작업
SELECT job_id, project, phase, started_at FROM pipeline_jobs WHERE status = 'running';

-- 최근 에러
SELECT job_id, project, error_detail, updated_at FROM pipeline_jobs
WHERE status = 'error' ORDER BY updated_at DESC LIMIT 10;

-- 승인 대기
SELECT job_id, project, created_at FROM pipeline_jobs WHERE status = 'awaiting_approval';
```

### 핵심 상수 요약

| 상수 | 값 | 출처 |
|------|-----|------|
| `POLL_INTERVAL` | 5초 | pipeline-runner.sh |
| `MAX_RUNTIME` | 7,200초 (2시간) | pipeline-runner.sh |
| `MAX_JOB_RUNTIME` | 3,600초 (1시간) | pipeline-runner.sh |
| `MAX_CONCURRENT_PER_PROJECT` | 3 | pipeline-runner.sh |
| `MAX_CONCURRENT_GLOBAL` | 10 | pipeline-runner.sh |
| `APPROVAL_TIMEOUT_HOURS` | 24 | pipeline-runner.sh |
| `_STALL_THRESHOLD_SEC` | 1,800초 (30분) | pipeline_runner_service.py |
| `_REVIEW_MODEL` | claude-sonnet-4-6 | pipeline_runner_service.py |
| `WATCHDOG_INTERVAL` | 300초 (5분) | pipeline-runner.sh |
| Watchdog loop | 120초 | pipeline_runner_service.py |

---

## 부록: 소스 파일 인벤토리

| 파일 | 줄 수 | 역할 |
|------|------|------|
| `app/api/pipeline_runner.py` | 612 | REST API |
| `app/services/pipeline_runner_service.py` | 2,216 | 오케스트레이터 |
| `scripts/pipeline-runner.sh` | 1,570 | Shell 실행기 |
| `scripts/auto_trigger.sh` | 961 | 레거시 자동 트리거 |
| `app/api/approval.py` | 325 | Watchdog 승인 큐 |
| `scripts/tg_approval_bot.py` | 293 | 텔레그램 봇 |
| `app/api/task_monitor.py` | 169 | SSE 모니터 |
| `app/api/ceo_chat_tools.py` | 3,489 | 채팅 도구 정의 |
| `app/services/tool_executor.py` | 2,644 | 도구 실행기 |
| `scripts/aads-pipeline-runner.service` | 31 | systemd 서비스 |
