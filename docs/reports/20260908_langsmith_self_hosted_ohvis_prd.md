# LangSmith 자체 구축 및 OHVIS 내부 LLMOps PRD

- 작성 시각: 2026-09-08 11:23 KST
- 대상: AADS/OHVIS, LangSmith-compatible trace/eval, Langfuse, LangGraph, Skill Find, LLM Wiki
- 요청: LangSmith를 자체적으로 구축할 수 있는지 확인한 뒤 다음 단계 진행
- 결론: 공식 LangSmith self-hosted 제품은 Enterprise 라이선스와 별도 인프라가 필요하므로 즉시 설치형 구현 대상이 아니다. AADS는 먼저 내부 `LangSmith-compatible` 관측/평가 레이어를 구축하고, 공식 self-hosted LangSmith는 라이선스/클러스터 승인 후 선택 적용한다.

## 1. 지시 파악

CEO 요청은 단순 설명이 아니라 다음 단계 실행이다. 이번 산출물은 다음 세 가지를 확정한다.

1. 공식 LangSmith self-hosted를 AADS가 자체 운영할 수 있는 조건.
2. 현재 OHVIS/AADS에 이미 반영된 관측/하네스 기반.
3. 즉시 구현 가능한 내부 LLMOps PRD와 향후 공식 LangSmith 전환 게이트.

## 2. 현행 실측

| 항목 | 실측값 | 판정 | 출처 |
|---|---:|---|---|
| 기준 시각 | 2026-09-08 11:23 KST | 최신 측정 | DB `now() AT TIME ZONE 'Asia/Seoul'` |
| OHVIS foundation tables | 8개 존재 | 기반 반영 | DB `information_schema.tables` |
| `ops_skill_library` | 9건 | Skill Find seed 존재 | DB 조회 |
| `ohvis_wiki_pages` | 1,693건 | LLM Wiki page 기반 존재 | DB 조회 |
| `ohvis_harness_traces` | 102건 | 내부 trace 기반 존재 | DB 조회 |
| `ohvis_tasks.done` | 254건 | task 원장 누적 | DB 조회 |
| `ohvis_tasks.running` | 44건 | stale/runner 정합성 감사 필요 | DB 조회 |
| `ohvis_tasks.stale` | 21건 | cleanup 정책 필요 | DB 조회 |
| LangGraph 의존성 | `langgraph>=1.1.6` | 반영 | `pyproject.toml` |
| LangSmith 패키지 | lock에 `langsmith==0.12.2` | 설치 기반 존재 | `requirements.runtime.lock` |
| Langfuse 의존성 | `langfuse>=3.0.0` | 운영 관측 우선축 | `pyproject.toml` |
| OHVIS Harness API | `/ohvis/harness/status`, `/skill-find`, `/wiki/search`, `/hermes/recommend` | 일부 구현 | `app/api/ohvis_harness.py` |
| 공식 LangSmith 서버 | 운영 구성/라이선스/클러스터 없음 | 미구축 | 코드/설정 검색 |

## 3. 공식 LangSmith self-hosted 요건

공식 문서 기준 LangSmith self-hosted는 다음 조건을 가진다.

| 요건 | 내용 | AADS 현재 판정 |
|---|---|---|
| 계약 | Enterprise plan add-on, trial/license key 필요 | 미보유로 추정, 확인 필요 |
| 제품 구성 | frontend UI, backend API, platform backend, playground, queue, ACE backend | AADS 내부에 동일 제품 없음 |
| 저장소 | ClickHouse, PostgreSQL, Redis/Valkey, Blob storage 권장 | AADS는 PostgreSQL/Redis는 있으나 ClickHouse/Blob 운영 설계 없음 |
| 설치 방식 | Kubernetes/Helm 및 cloud provider guide 중심 | 현재 AADS blue-green Docker Compose 운영과 별도 |
| Deployment self-hosted | Agent Server, control plane, data plane, DB를 자체 인프라에 운영 | 별도 클러스터/운영 책임 필요 |
| Engine | 별도 entitlement, Sandboxes 필요, 일부 LangChain-managed Intelligence 사용 | 완전 내부형으로 보기 어려움 |

공식 근거:

- LangSmith Self-hosted: https://docs.langchain.com/langsmith/self-hosted
- Deploy to self-hosted: https://docs.langchain.com/langsmith/deploy-to-self-hosted-overview
- Enable additional self-hosted features: https://docs.langchain.com/langsmith/deploy-self-hosted-full-platform
- LangSmith pricing: https://www.langchain.com/pricing
- LangSmith Observability: https://docs.langchain.com/langsmith/observability
- LangSmith Evaluation: https://docs.langchain.com/langsmith/evaluation

## 4. 구축 전략

### 4.1 판정

| 선택지 | 설명 | 장점 | 리스크 | 권장 |
|---|---|---|---|---|
| A. 공식 LangSmith self-hosted 즉시 설치 | Enterprise license + K8s/ClickHouse/Redis/Postgres/Blob 구성 | 제품 UI/평가/운영 기능 완성도 높음 | 라이선스, 인프라, 운영복잡도, 비용 미확정 | 보류 |
| B. LangSmith SaaS opt-in | 기존 앱 trace를 외부 LangSmith로 전송 | 빠른 검증 | 데이터 반출, 비용, 보안 승인 필요 | P2 옵션 |
| C. AADS 내부 LangSmith-compatible LLMOps | 내부 DB에 trace/eval/dataset/feedback schema 구현 후 Langfuse와 병행 | 즉시 구현 가능, 데이터 내부 보존, 기존 OHVIS와 결합 | LangSmith UI/Engine 수준은 자체 구현 필요 | P0 권장 |
| D. Langfuse만 유지 | 현행 trace만 보강 | 변경량 작음 | dataset/eval/failure-to-directive가 약함 | 단기 유지 |

권장안은 C다. 이유는 AADS가 이미 `ohvis_harness_traces`, `ohvis_wiki_pages`, `ops_skill_library`, LangGraph, Langfuse 기반을 갖고 있기 때문이다. 공식 LangSmith self-hosted는 구매/인프라 결정 이후 붙이는 외부 제품 옵션으로 둔다.

### 4.2 목표 아키텍처

```text
Chat / Runner / Ops Worker / Dashboard
        |
        v
OHVIS Harness Kernel
        |
        +-- LangGraph run
        |      - graph_run_id
        |      - checkpoint/thread_id
        |      - interrupt/resume
        |
        +-- Tool and Skill Find
        |      - skill_slug/version/hash
        |      - risk_tier
        |      - approval policy
        |
        +-- Internal LangSmith-compatible LLMOps
        |      - trace
        |      - span
        |      - tool call
        |      - dataset
        |      - evaluation
        |      - feedback
        |
        +-- Langfuse adapter
        |      - current trace callback
        |      - optional export
        |
        +-- Official LangSmith adapter
               - disabled by default
               - enabled only with license/key/masking approval
```

## 5. PRD

### 5.1 제품명

OHVIS Internal LLMOps and LangSmith-compatible Evaluation Layer v1

### 5.2 사용자

| 사용자 | 필요 기능 |
|---|---|
| CEO | 응답/러너/배포가 왜 성공·실패했는지 trace와 평가 결과로 확인 |
| CTO/PM Agent | 반복 실패를 dataset/eval로 승격하고 개선 지시서 생성 |
| Runner | 코드 수정·테스트·배포 결과를 같은 `graph_run_id`에 기록 |
| QA Agent | 과거 실패 케이스를 재실행해 회귀 여부 판단 |
| Ops | 비용, latency, tool error, approval wait, stale task를 감시 |

### 5.3 기능 요구사항

| ID | 요구사항 | 우선순위 | 완료 기준 |
|---|---|---:|---|
| FR-001 | `ohvis_harness_traces`를 trace/span/run 계층으로 확장 | P0 | trace, span, tool_call, latency, cost, error 저장 |
| FR-002 | `llmops_datasets`, `llmops_examples`, `llmops_experiments`, `llmops_scores` 추가 | P0 | 실패 응답 1건을 dataset example로 승격 |
| FR-003 | chat/runner/deploy/task에 `graph_run_id`와 `trace_id` 연결 | P0 | 완료보고에서 run provenance 표시 |
| FR-004 | `quality_score < 0.4` 또는 반복 error pattern을 eval 후보로 생성 | P0 | 최근 실패 패턴 top-k dataset 자동 후보 |
| FR-005 | `/api/v1/ohvis/llmops/traces` 조회 API | P0 | 프로젝트/세션/run별 trace 조회 |
| FR-006 | `/api/v1/ohvis/llmops/evals` 평가 API | P0 | dataset 선택 후 offline eval 실행 |
| FR-007 | `/ops/traces` 대시보드 | P1 | trace timeline, tool call, cost, error 표시 |
| FR-008 | `/ops/evals` 대시보드 | P1 | dataset/experiment/score/회귀 상태 표시 |
| FR-009 | Langfuse export adapter 유지 | P1 | Langfuse callback 실패해도 내부 trace는 남음 |
| FR-010 | 공식 LangSmith export adapter opt-in | P2 | env off 기본값, masking 테스트, endpoint/key 존재 시만 전송 |
| FR-011 | official self-hosted readiness checklist | P2 | license, K8s, ClickHouse, Redis, Blob, backup, SSO 체크 |

### 5.4 비기능 요구사항

| 항목 | 기준 |
|---|---|
| 보안 | prompt/input/output 원문 저장은 민감정보 마스킹 후 저장 |
| 데이터 주권 | 기본 trace/eval은 AADS PostgreSQL 내부 저장 |
| 비용 | LLM-as-judge는 sampling/rate limit 적용, 기본 rule evaluator 우선 |
| 안정성 | trace 저장 실패가 chat/runner 실행을 중단하지 않음 |
| 감사 | 모든 eval score는 evaluator version과 source trace를 보존 |
| 배포 | API 변경은 blue-green, clean SHA, same digest, 5분 P0/P1 monitor 준수 |

### 5.5 데이터 모델 초안

| 테이블 | 용도 | 주요 컬럼 |
|---|---|---|
| `llmops_traces` | 상위 trace/run 원장 | `id`, `graph_run_id`, `project`, `session_id`, `task_id`, `status`, `cost_usd`, `latency_ms` |
| `llmops_spans` | 모델/tool/chain span | `trace_id`, `parent_span_id`, `span_type`, `name`, `started_at`, `ended_at`, `error` |
| `llmops_tool_calls` | 도구 호출 세부 | `span_id`, `tool_name`, `risk_tier`, `approval_state`, `input_summary`, `output_summary` |
| `llmops_datasets` | 평가 데이터셋 | `slug`, `project`, `purpose`, `source_filter`, `enabled` |
| `llmops_examples` | 평가 예제 | `dataset_id`, `source_trace_id`, `input`, `expected`, `rubric`, `metadata` |
| `llmops_experiments` | 평가 실행 | `dataset_id`, `candidate_sha`, `model_id`, `status`, `started_at`, `completed_at` |
| `llmops_scores` | 평가 점수 | `experiment_id`, `example_id`, `evaluator`, `score`, `comment`, `evidence` |
| `llmops_feedback` | CEO/사용자 피드백 | `trace_id`, `rating`, `label`, `comment`, `created_by` |

기존 `ohvis_harness_traces`는 바로 폐기하지 않고 v1 compatibility view 또는 adapter로 연결한다.

### 5.6 API 초안

| Method | Route | 용도 |
|---|---|---|
| GET | `/api/v1/ohvis/llmops/status` | LLMOps foundation 상태 |
| GET | `/api/v1/ohvis/llmops/traces` | trace 검색 |
| GET | `/api/v1/ohvis/llmops/traces/{trace_id}` | trace detail/span/tool call 조회 |
| POST | `/api/v1/ohvis/llmops/datasets/from-trace` | trace를 eval example로 승격 |
| POST | `/api/v1/ohvis/llmops/evals/run` | offline eval 실행 요청 |
| GET | `/api/v1/ohvis/llmops/evals/{experiment_id}` | eval 결과 조회 |
| POST | `/api/v1/ohvis/llmops/export/langsmith` | 승인된 trace만 LangSmith로 export |

### 5.7 화면 초안

| 화면 | 기능 | 완료 기준 |
|---|---|---|
| `/ops/traces` | 프로젝트별 trace list, cost, latency, error, tool calls | 최근 24시간 trace 검색 가능 |
| `/ops/traces/[id]` | span tree, prompt/input/output summary, tool approval trail | 실패 원인과 재현 입력 확인 가능 |
| `/ops/evals` | dataset, experiment, score trend | candidate SHA별 회귀 비교 |
| `/ops/llmops/settings` | Langfuse/LangSmith export, masking, sampling 정책 | 외부 전송 기본 off 확인 |

## 6. 구현 단계

| 단계 | 작업 | 범위 | 완료 기준 |
|---|---|---|---|
| P0-1 | DB migration 작성 | additive schema only | migration dry-run, SELECT 검증 |
| P0-2 | 내부 trace writer | `app/services/llmops_*` | trace write/read unit test |
| P0-3 | 기존 `ohvis_harness_traces` adapter | compatibility | 기존 API 깨짐 없음 |
| P0-4 | chat/runner/deploy provenance 연결 | 최소 `trace_id`, `graph_run_id` | 완료보고 trace 링크 가능 |
| P0-5 | dataset 승격 API | failed/low-quality trace | trace 1건 -> example 1건 |
| P1-1 | rule evaluator | format/source/tool-policy checks | LLM 비용 없이 기본 평가 |
| P1-2 | LLM-as-judge evaluator | opt-in/sampling | 비용 상한과 score 저장 |
| P1-3 | dashboard trace/eval UI | AADS dashboard | 화면 캡처 또는 API 폴백 |
| P2-1 | LangSmith SaaS/export adapter | env opt-in | masking + endpoint/key 테스트 |
| P2-2 | official self-hosted readiness | infra checklist | license/K8s/ClickHouse/backup 승인 |

## 7. 공식 LangSmith self-hosted 전환 게이트

공식 LangSmith self-hosted는 아래가 모두 충족될 때만 진행한다.

| 게이트 | 필요 조건 | 미충족 시 대안 |
|---|---|---|
| License | Enterprise plan/license key 확보 | 내부 LLMOps 유지 |
| Infra | Kubernetes 또는 공식 지원 Docker/K8s topology 확정 | AADS Compose에 섞지 않음 |
| Storage | ClickHouse, PostgreSQL, Redis/Valkey, Blob storage 준비 | PostgreSQL 내부 schema로 대체 |
| Security | SSO/RBAC/ABAC, TLS, secret management | AADS 기존 auth/role gate 유지 |
| Data policy | trace 원문/민감정보/고객 데이터 반출 승인 | masking + internal only |
| Ops | backup, upgrade, capacity, monitoring 책임자 지정 | Langfuse/internal dashboard 유지 |
| Cost | license, infra, LCU/Engine 비용 승인 | rule evaluator 우선 |

## 8. 실행 지시서 초안

```text
>>>DIRECTIVE_START
TASK_ID: AADS-LANGSMITH-INTERNAL-LLMOPS-P0
TITLE: OHVIS 내부 LangSmith-compatible Trace/Eval LLMOps 기반 구현
PRIORITY: P1-HIGH
SIZE: L
MODEL: gpt-5.6-sol
DESCRIPTION:
1. 공식 LangSmith self-hosted 제품 설치는 Enterprise license/Kubernetes/ClickHouse/Redis/Blob 승인 전까지 진행하지 않는다.
2. AADS 내부 PostgreSQL에 `llmops_traces`, `llmops_spans`, `llmops_tool_calls`, `llmops_datasets`, `llmops_examples`, `llmops_experiments`, `llmops_scores`, `llmops_feedback` additive migration을 추가한다.
3. 기존 `ohvis_harness_traces`와 호환되는 adapter를 구현해 기존 `/api/v1/ohvis/harness/status` 및 trace 기반 기능을 깨지 않게 한다.
4. chat, pipeline runner, deploy run, tool execution에 `graph_run_id`와 `trace_id`를 연결한다.
5. 실패/저품질 trace를 dataset example로 승격하는 API와 rule evaluator를 우선 구현한다. LLM-as-judge는 sampling과 비용 상한을 둔 opt-in으로 둔다.
6. Langfuse는 현행 관측으로 유지하고, LangSmith external export는 `LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, `LANGSMITH_API_KEY`가 모두 있고 masking 정책이 통과될 때만 활성화한다. 기본값은 off다.
7. Dashboard `/ops/traces`, `/ops/evals`, `/ops/llmops/settings` 초안을 구현한다.
8. unrelated dirty 파일은 건드리지 말고 isolated worktree 또는 Runner 의존성 그래프로 진행한다.
9. 검증: migration dry-run, unit tests, API route import, API smoke, dashboard screenshot 또는 API fallback, blue-green 배포 시 5분 P0/P1 모니터링.
>>>DIRECTIVE_END
```

## 9. 리스크

| 리스크 | 영향 | 통제 |
|---|---|---|
| 공식 LangSmith를 바로 self-host하려다 인프라가 AADS 배포 경로와 충돌 | 운영 장애/복잡도 증가 | 별도 클러스터/namespace, 승인 전 설치 금지 |
| trace 원문에 개인정보/시크릿 포함 | 보안 사고 | summary 저장, masking, export opt-in |
| LLM-as-judge 비용 증가 | 비용 급증 | rule evaluator 우선, sampling, budget gate |
| 기존 Langfuse와 중복 저장 | 저장 비용/혼선 | 내부 canonical trace, Langfuse/export는 adapter |
| 기존 dirty worktree와 구현 커밋 섞임 | 배포 preflight 차단 | isolated worktree 또는 Runner 사용 |
| running/stale task 불일치 | trace/eval 근거 왜곡 | P0 구현 전 task ledger audit 병행 |

## 10. 완료 기준

1. DB additive migration 적용 전후 SELECT가 통과한다.
2. chat 요청 1건, runner 작업 1건, deploy run 1건이 같은 `graph_run_id` 또는 연결 가능한 provenance를 남긴다.
3. 실패 trace 1건이 dataset example로 승격된다.
4. rule evaluator가 최소 5개 기준을 점수화한다: source presence, tool policy, final response persistence, cost present, error classification.
5. `/ops/traces`와 `/ops/evals`에서 조회 가능하다.
6. 외부 LangSmith 전송은 기본 off이며, masking 승인 전에는 어떤 원문도 외부로 나가지 않는다.

## 11. 최종 권장

즉시 구현은 공식 LangSmith self-hosted 설치가 아니라 `AADS-LANGSMITH-INTERNAL-LLMOPS-P0`가 맞다. 현재 AADS는 이미 1,693개 wiki page와 102개 harness trace를 갖고 있으므로, 이 데이터를 내부 trace/eval/dataset 구조로 승격하면 비용과 보안 리스크를 낮추면서 LangSmith식 운영 효과를 먼저 얻을 수 있다.
