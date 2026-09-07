# LangGraph/LangSmith LLMOps 배포시스템 1~4단계 승인 패키지

- 작성 시각: 2026-09-07 18:49 KST
- 범위: 1. 설계, 2. PRD, 3. 화면 목업, 4. Tool 셋업 및 연동
- 승인 정책: 본 문서는 구축개선 전 승인용 산출물이다. 런타임 코드 변경, DB 변경, 배포는 승인 후 별도 진행한다.
- 관련 기존 근거: `docs/reports/20260907_ohvis_harness_langgraph_langchain_langsmith_llmwiki_plan.md`, `app/services/ohvis_harness.py`, `app/api/ohvis_harness.py`, `migrations/158_ohvis_harness_skill_wiki_foundation.sql`, `migrations/161_ops_deploy_request_queue.sql`

## 요약

1~4단계 산출물 기준으로 권장 구조는 "LangGraph 실행 하네스 + LangChain 도구 계층 + LangSmith-compatible 내부 trace + Ops DB 배포 원장 + 채팅 아티팩트 관제"이다.

현재 AADS에는 기반이 이미 일부 있다. `deploy_runs` 128건, `deploy_phase_events` 495건, `ops_skill_library` 9건, `ohvis_harness_traces` 26건, `ohvis_wiki_pages` 1,693건이 운영 DB에 존재한다. API `/health`는 200이고, `/api/v1/ohvis/harness/status`, `/api/v1/ops/deploy/status`는 인증 보호로 401을 반환한다.

## 1. 설계

### 목표

코드 수정, 커밋, 푸시, 배포, 검증을 채팅 응답 생명주기에서 분리하고 Ops DB 원장으로 관리한다. 채팅은 "요청 접수 + job id + 상태 확인 경로"를 빠르게 반환하고, 실제 장시간 작업은 Runner/Ops Worker가 이어받는다.

### 기술 스택

| 계층 | 선택 | 역할 | 현재 반영 |
|---|---|---|---|
| API | FastAPI 0.115 | 배포 요청/상태/하네스 API | 반영 |
| UI | Next.js 16 | 채팅 아티팩트 탭, Ops 카드 | 반영 중 |
| DB | PostgreSQL 15 + pgvector | deploy/task/trace/wiki 원장 | 반영 |
| Agent runtime | LangGraph | durable state, checkpoint, interrupt/resume | 부분 반영 |
| Tool layer | LangChain Core + MCP adapters | 모델/도구 schema, tool policy | 부분 반영 |
| Observability | Langfuse + LangSmith-compatible schema | trace, latency, cost, eval 후보 | 부분 반영 |
| Release | Docker Compose blue/green | clean SHA, same-digest, health gate | 반영 중 |

### 목표 흐름

```text
CEO Chat
  -> 코드 수정 요청
  -> Runner job 생성
  -> diff/test/commit/push 완료
  -> Ops deploy request 등록
  -> 채팅은 즉시 deploy_run_id 반환
  -> Ops Worker가 queue 직렬 처리
  -> blue/green deploy.sh 실행
  -> deploy_phase_events 기록
  -> health/same-digest/5분 모니터링
  -> Push/채팅 아티팩트/ops 카드에 완료 알림
```

### 동시 작업 안전 원칙

| 문제 | 설계 원칙 | 완료 기준 |
|---|---|---|
| 여러 세션 동시 배포 | 프로젝트별 deploy lock + DB queue 직렬화 | active deploy 1건, queued N건 |
| 수정 코드 누락 | clean release SHA만 배포 | image label SHA == git commit SHA |
| dirty 섞임 | 대상 파일 선별 commit, release worktree build | uncommitted file build 금지 |
| B/G 동기화 중 추가 배포 | 실패 처리 대신 queued_for_deploy 보류 | 최신 SHA 1건으로 supersede |
| 채팅 응답 지연 | 배포 완료 대기 금지, deploy_run_id 즉시 반환 | 채팅 응답 10초 이내 접수 |

## 2. PRD

### 제품명

AADS Ops Managed Release + LLMOps Harness v1

### 사용자

| 사용자 | 핵심 요구 |
|---|---|
| CEO | 채팅에서 오래 기다리지 않고 진행/완료/실패를 확인 |
| PM/CTO AI | 작업 상태, 승인 필요, 누락 위험을 DB 원장 기준으로 판단 |
| Runner | 코드 수정 후 commit/push/deploy request까지 자동 연결 |
| DevOps Worker | 배포 큐를 직렬 처리하고 phase별 검증을 기록 |

### 기능 요구사항

| ID | 요구사항 | 우선순위 | 승인 전 판정 |
|---|---|---:|---|
| FR-001 | 코드 수정 완료 후 commit/push 상태를 Ops DB에 기록 | P0 | 설계 확정 필요 |
| FR-002 | `/ops/deploy/requests`로 배포 요청을 큐 등록하고 즉시 반환 | P0 | 기본 API 존재 |
| FR-003 | 배포 진행 phase, 경과시간, 예상잔여시간을 `/ops/deploy/status`에서 제공 | P0 | 기본 API 존재 |
| FR-004 | 채팅 아티팩트 배포 탭에서 진행/대기/최근 반영 내역 표시 | P0 | 기본 UI 존재 |
| FR-005 | 동시 배포 요청은 실행 중 run 뒤에 queued_for_deploy로 보류 | P0 | 보강 필요 |
| FR-006 | 같은 프로젝트 최신 SHA 외 과거 큐는 superseded 처리 | P0 | 보강 필요 |
| FR-007 | LangGraph run_id와 runner/deploy/task id를 연결 | P1 | 보강 필요 |
| FR-008 | LangSmith-compatible trace/eval row를 내부 DB에 남김 | P1 | 기반 존재 |
| FR-009 | LLM Wiki/Error Book에 반복 장애와 개선책 자동 적재 | P1 | 기반 존재, 자동화 미흡 |
| FR-010 | 승인 필요 작업은 interrupt 상태로 저장 후 resume | P1 | 보강 필요 |

### 비기능 요구사항

| 항목 | 기준 |
|---|---|
| 안정성 | active API 직접 restart 금지, blue/green only |
| 추적성 | 모든 배포는 release_sha, runner_job_id, deploy_run_id 연결 |
| 응답성 | 채팅은 장시간 배포 완료를 동기 대기하지 않음 |
| 보안 | DROP/TRUNCATE/force push/시크릿 출력/무단 deploy 차단 |
| 롤백 | cutover health 실패 시 이전 active 라우팅 즉시 복구 |
| 검증 | candidate health, routed health, same-digest standby, 5분 P0/P1 monitor |

### 성공 지표

| 지표 | 목표 |
|---|---:|
| 배포 요청 등록 응답 | 10초 이내 |
| 동시 배포 시 중복 실행 | 0건 |
| dirty 파일 포함 배포 | 0건 |
| release SHA와 이미지 SHA 불일치 | 0건 |
| 배포 완료 후 상태 미표시 | 0건 |

## 3. 화면 목업

### 목업 파일

- `app/static/reports/20260907_langgraph_llmops_release_control_mockup.html`

### 화면 구성

| 영역 | 표시 정보 | 목적 |
|---|---|---|
| 상단 상태 바 | active/queued/blocked/last certified | CEO가 현재 배포 가능 여부 즉시 판단 |
| 배포 카드 | 프로젝트, phase, 경과, 예상잔여, SHA | 동시 배포 상황 파악 |
| 변경 요약 | 기능명, 수정 파일, 검증 결과 | 무엇이 운영 반영됐는지 확인 |
| LLMOps Trace | graph_run_id, trace_id, tool_calls, cost | 오류 개선과 재현 근거 확보 |
| 승인 큐 | 승인 필요 tool/deploy/DB 작업 | 위험 작업의 명시 승인 |

### 첫 진입 경로

채팅 화면 오른쪽 아티팩트 탭에서 `배포`를 선택하면 `/ops/deploy/status` 기반 카드가 보인다. 상세 조사가 필요하면 카드의 deploy_run_id를 눌러 Ops 화면의 phase timeline으로 이동한다.

## 4. Tool 셋업 및 연동

### 현재 확인된 도구/연동

| 도구/모듈 | 확인 결과 | 근거 |
|---|---|---|
| LangGraph | import 가능 | 컨테이너 Python import |
| LangChain Core | import 가능 | 컨테이너 Python import |
| LangChain OpenAI/Anthropic/Google | import 가능 | 컨테이너 Python import |
| LangSmith | import 가능 | 컨테이너 Python import |
| Langfuse | import 가능 | 컨테이너 Python import |
| MCP adapter | import 가능 | 컨테이너 Python import |
| `deploy_runs` | 128건 | 운영 DB 조회 |
| `deploy_phase_events` | 495건 | 운영 DB 조회 |
| `ops_skill_library` | 9건 | 운영 DB 조회 |
| `ohvis_harness_traces` | 26건 | 운영 DB 조회 |
| `ohvis_wiki_pages` | 1,693건 | 운영 DB 조회 |

### 연동 설계

| 연동 | 방식 | 승인 전 상태 |
|---|---|---|
| Chat -> Runner | 기존 pipeline runner submit/status 사용 | 유지 |
| Runner -> Ops Deploy Request | commit/push 후 `/ops/deploy/requests` 등록 | 보강 필요 |
| Ops Worker -> deploy.sh | DB queue 1건 claim 후 blue/green 실행 | 보강 필요 |
| deploy.sh -> DB | phase start/complete, PID, heartbeat 기록 | 존재 |
| DB -> Chat Artifact | `/ops/deploy/status` polling | 존재 |
| LangGraph -> Ops IDs | graph_run_id/run metadata에 runner/deploy 연결 | 보강 필요 |
| LangSmith-compatible trace | 내부 `ohvis_harness_traces` 우선, 외부 SaaS opt-in | 기반 존재 |
| LLM Wiki/Error Book | 반복 장애를 wiki/error book으로 승격 | 자동화 보강 필요 |

### 승인 후 구축개선 범위

| 우선순위 | 구축개선 항목 | 예상 변경 |
|---|---|---|
| P0 | Runner 배포 완료 대기 제거 | `pipeline_runner_service`, `tool_executor` |
| P0 | Deploy request worker 자동화 | `deploy_observability`, ops worker 또는 deploy coordinator |
| P0 | queued_for_deploy 최신 SHA 병합 | `deploy_runs` queue claim/supersede 로직 |
| P0 | same-digest/monitoring 완료 전 완료 보고 차단 | `deploy.sh`, deploy status |
| P1 | graph_run_id 전 구간 전파 | chat/runner/ops trace metadata |
| P1 | LangSmith-compatible eval case 생성 | trace/eval service |
| P1 | wiki/error-book 자동 적재 | report compiler, memory promotion |

## 승인 요청

아래 4개 산출물은 승인 전 단계로 완료했다.

| 단계 | 결과물 | 상태 |
|---|---|---|
| 1 설계 | 목표 아키텍처, 동시 작업 안전 흐름 | 완료 |
| 2 PRD | 기능/비기능/성공지표 | 완료 |
| 3 화면 목업 | 채팅 아티팩트 배포/LLMOps 카드 목업 | 완료 |
| 4 Tool 셋업 및 연동 | 현재 연동 실측, 승인 후 보강 범위 | 완료 |

승인 후에는 P0 구축개선부터 진행한다. 첫 구현 단위는 "배포 요청 비동기화 + Ops DB queue worker + 최신 SHA supersede + 채팅 응답 즉시 종료"로 묶는 것이 맞다.
