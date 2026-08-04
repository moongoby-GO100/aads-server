# OHVIS 지식·작업물 맥락 운영관리 진화 보고서

> 작성 시각: 2026-07-28 06:03 KST  
> 대상: OHVIS/AADS 지식, 작업물, 채팅 세션, 러너/에이전트 실행 맥락  
> 범위: 최신 기술 동향 확인, 현재 AADS 실측, 목표 아키텍처, 단계별 구현안  
> 결론: OpenClaw 같은 외부 메신저 게이트웨이보다, AADS 내부에 `Context Operating System`을 완성하는 것이 우선이다.

## 1. 요약

OHVIS가 진화하려면 단순히 채팅 내용을 오래 저장하는 수준을 넘어야 한다. 핵심은 **대화, 코드, 파일, 보고서, 러너 작업, 배포, 장애, CEO 결정**을 하나의 연결된 운영 맥락으로 묶고, 그 맥락을 매 턴 필요한 만큼만 정확히 꺼내는 것이다.

현재 AADS는 이미 `memory_facts`, `ai_observations`, `ai_meta_memory`, `chat_turn_executions`, `chat_artifacts`, `ohvis_tasks`, `compiled_prompt_provenance`를 갖고 있어 기반은 강하다. 하지만 `research_archive=0건`, `ohvis_tasks=8건`, 작업물-지시-결과 연결 미완성, 외부 자료 근거 저장 미흡 때문에 "지식이 쌓이지만 운영 판단으로 자동 승격되는 구조"는 아직 약하다.

권장 방향은 다음 6계층이다.

1. **Event Ledger**: 모든 지시, 도구 호출, 파일 변경, 배포, 오류를 append-only 이벤트로 저장
2. **Artifact Registry**: 문서, 코드, 이미지, 보고서, 패치, 배포 산출물을 버전형 객체로 관리
3. **Evidence Store**: 외부/내부 원천근거를 청크+메타데이터+출처로 저장
4. **Temporal Context Graph**: 사람/프로젝트/파일/결정/작업/오류/배포의 시간 관계를 그래프로 연결
5. **Retrieval Router**: SQL exact lookup -> graph -> vector -> web 순서로 맥락 검색
6. **Context Compiler**: 프롬프트 L1~L5에 필요한 근거만 주입하고 provenance로 적용 여부 검증

## 2. 현재 AADS 실측

### 2.1 운영 DB 규모

2026-07-28 06:03 KST 기준 Docker PostgreSQL 직접 조회 결과다.

| 테이블 | 건수 | 의미 | 판정 |
|---|---:|---|---|
| `memory_facts` | 57,479 | 추출·승격된 사실/변경/패턴 | 강점 |
| `chat_messages` | 45,566 | 전체 채팅 메시지 | 강점 |
| `chat_artifacts` | 23,728 | 보고서/표/코드 등 산출물 | 강점 |
| `chat_turn_executions` | 9,686 | 채팅 턴 실행 원장 | 강점 |
| `ai_meta_memory` | 953 | 증류된 운영 규칙/선호 | 보통 |
| `ai_observations` | 456 | 관찰/교정/발견 | 보통 |
| `ohvis_tasks` | 8 | OHVIS 작업 카드 | 초기 |
| `research_archive` | 0 | 리서치 원문/출처/보고서 보관 | 취약 |

### 2.2 실행/스트리밍 상태

| 항목 | 실측값 | 해석 |
|---|---:|---|
| `chat_turn_executions.completed` | 4,850 | 정상 완료 턴 |
| `chat_turn_executions.interrupted` | 4,832 | 중단/재시작/사용자 중단/복구 실패 등으로 닫힌 턴 |
| `chat_turn_executions.running` | 4 | 현재 실행 중 또는 stale 가능 실행 |
| `ohvis_tasks.running` | 6 | 작업 카드가 아직 running으로 남아 있음 |
| `ohvis_tasks.done` | 2 | 완료 작업 카드 |

`interrupted`가 `completed`와 거의 같은 규모인 것은 구조적 신호다. 서버 재시작, 브라우저 끊김, completion contract retry, stale execution 정리 같은 복구 경로가 적극 작동하고 있으나, CEO 체감에는 "응답이 끊김", "왜 다시 이어지지?", "뭘 잘못 수정했나"로 보일 수 있다.

### 2.3 현재 구현된 복구 장치

| 기능 | 파일 경로 | 현재 상태 |
|---|---|---|
| shutdown 전 부분 응답 보존 | `app/services/chat_service.py:4618` `preserve_active_streams_for_shutdown()` | API 종료 전 partial 저장 및 execution interrupted 표시 |
| 단일 실행 자동 재개 | `app/services/chat_service.py:4901` `_resume_single_stream()` | 마지막 user 메시지와 partial 기반 재생성 |
| startup/periodic resume scanner | `app/main.py:1121` 이후 `_resume_pending_executions_once()` | 재시작 후 stale running/retrying claim |
| blue/green resume owner 분리 | `app/main.py:1124` `_is_execution_resume_owner()` | inactive 컨테이너가 실행 복구를 claim하지 않도록 방지 |
| streaming placeholder 저장 | `app/services/chat_service.py`, `app/routers/chat.py` 전반 | 진행 중 partial DB 보존 |
| 작업 카드 API | `app/api/ohvis_tasks.py` | CRUD/SSE/queue API는 존재, 러너·채팅과 완전 연결은 미완 |

판정: "원천적인 자동 이어쓰기"는 이미 일부 구현돼 있다. 다만 완전한 원천 해결은 SSE 레벨이 아니라 **durable execution + event ledger + idempotent resume** 문제다. 즉, LLM 스트림을 메모리 프로세스에 묶어두지 않고, 실행 단계를 이벤트로 쪼개어 언제든 재개 가능한 작업 단위로 만들어야 한다.

## 3. 최신 기술 동향 확인

### 3.1 Context Engineering

2026년 에이전트 시스템의 중심은 "프롬프트 잘 쓰기"가 아니라 **컨텍스트를 어떻게 구성·검색·검증·주입하느냐**로 이동했다. LlamaIndex는 context engineering을 단순 RAG보다 넓은 개념으로 설명하며, 메모리, 도구 선택, 컨텍스트 윈도우 최적화를 함께 다룬다.

OHVIS 적용 의미:
- 모든 대화 전체를 넣는 방식은 비용과 오류를 키운다.
- 매 턴 `task_id`, `session_id`, `artifact_id`, `file_path`, `decision_id` 기준으로 필요한 근거만 꺼내야 한다.
- 프롬프트는 "정적 문장 묶음"이 아니라 `Context Compiler`의 산출물이어야 한다.

### 3.2 OpenAI Agents/Responses

OpenAI 최신 Agents 문서는 에이전트를 "계획, 도구 호출, 전문 에이전트 협업, 다단계 작업 완료에 필요한 상태 유지" 앱으로 정의한다. Agents SDK는 도구, MCP, 상태 저장, guardrail, tracing, resumable approval flow를 지원하고, Responses API는 사용자가 직접 루프와 상태를 관리하는 쪽에 적합하다.

OHVIS 적용 의미:
- AADS는 이미 자체 도구와 실행 원장을 갖고 있으므로 Responses식 "직접 루프 소유"와 Agents식 "전문화된 실행 단위"를 혼합하는 구조가 맞다.
- 고위험 작업은 `approval_required` 상태로 멈추고, 승인 후 같은 execution을 이어가야 한다.
- `previous_response_id` 같은 provider 상태만 믿으면 안 된다. OpenAI 문서도 이전 입력 토큰은 계속 청구된다고 밝히므로, OHVIS는 자체 요약/압축/근거 검색으로 비용을 통제해야 한다.

### 3.3 OpenAI File Search / Vector Store

OpenAI File Search는 vector store 결과 개수 제한, 검색 결과 포함, metadata filtering을 제공한다. 이는 OHVIS가 이미 가진 pgvector를 버리라는 뜻이 아니라, **메타데이터 필터가 없는 벡터 검색은 운영 지식에 위험하다**는 뜻이다.

OHVIS 적용 의미:
- 모든 evidence chunk에 `project`, `workspace`, `source_type`, `sensitivity`, `freshness`, `trust_level`, `valid_from`, `valid_to`가 필요하다.
- 검색 결과는 답변 본문뿐 아니라 "어떤 chunk가 왜 들어왔는지"를 검증용으로 남겨야 한다.

### 3.4 MCP

MCP 2026-07-28 release candidate는 protocol core를 stateless로 전환하고, Extensions, Tasks, MCP Apps, authorization hardening을 강조한다. 중요한 문장은 "protocol은 stateless여도 application은 explicit handle로 상태를 유지할 수 있다"는 방향이다.

OHVIS 적용 의미:
- AADS 도구는 MCP식으로 표준화하되, 상태는 `task_id`, `execution_id`, `artifact_id`, `browser_id`, `file_snapshot_id` 같은 명시 핸들로 넘겨야 한다.
- "로컬 PC LLM 도구 호출"도 PC Agent를 MCP server로 노출하면 채팅 모델이 표준 도구처럼 사용할 수 있다.
- 단, 권한과 승인 범위를 도구 설명이 아니라 DB policy로 통제해야 한다.

### 3.5 A2A

Google의 A2A 가이드는 각 에이전트가 `/.well-known/agent-card.json`에 이름, 기능, endpoint를 공개하고, 런타임에 agent card를 읽어 라우팅하는 구조를 설명한다.

OHVIS 적용 의미:
- KIS/GO100/SF/NTV2/NAS 매니저를 OHVIS 하위 에이전트로만 붙이지 말고, 각 프로젝트를 A2A agent card로 노출할 수 있다.
- CEO가 "KIS 주문 문제 확인"이라고 말하면 OHVIS가 agent card를 보고 KIS Manager에게 위임하고, 결과를 OHVIS 작업 카드로 회수한다.
- A2A는 외부 생태계 연동용이고, 내부 핵심 원장은 여전히 AADS DB가 가져야 한다.

### 3.6 LangGraph / Durable Execution

LangGraph는 durable execution, streaming, human-in-the-loop, persistence를 에이전트 오케스트레이션 핵심 기능으로 둔다.

OHVIS 적용 의미:
- 스트리밍 끊김 문제의 본질은 "SSE가 약함"이 아니라 "실행이 프로세스 메모리에 묶임"이다.
- `chat_turn_executions`와 `ohvis_tasks`를 LangGraph식 checkpoint/run-step 모델로 확장하면 서버 재시작 후에도 정확히 어느 단계부터 이어갈지 알 수 있다.
- 장기 작업은 `step_started`, `tool_called`, `tool_result_saved`, `artifact_written`, `approval_waiting`, `completed` 이벤트로 남겨야 한다.

### 3.7 Temporal Context Graph / Graphiti

Graphiti는 대화, 비즈니스 데이터, 문서를 temporal context graph로 바꾸고, 사실 변경 시 오래된 사실을 무효화하며, vector/full-text/graph traversal을 한 번에 결합한다고 설명한다.

OHVIS 적용 의미:
- "예전에는 맞았지만 지금은 틀린 정보"를 `superseded_by`만으로 관리하기보다 `valid_from`, `valid_to`, `invalidated_by_event_id`로 시간성을 가져야 한다.
- "서버68은 사용 안 한다고 했는데 왜 아직 active인가?" 같은 질문은 벡터 검색보다 시간 그래프가 맞다.
- `memory_facts.related_facts`를 넘어 별도 edge table이 필요하다.

### 3.8 OWASP/NIST 보안·거버넌스

OWASP 2025 LLM Top 10은 prompt injection, sensitive information disclosure, vector/embedding weaknesses, excessive agency, unbounded consumption 등을 생성형 AI 앱 생명주기 리스크로 다룬다. NIST AI RMF Generative AI Profile은 생성형 AI 고유 리스크 식별과 관리 액션을 제시한다.

OHVIS 적용 의미:
- 외부 문서와 업로드 파일은 그대로 프롬프트에 넣으면 안 된다.
- ingestion 단계에서 prompt injection 제거, secret scan, source trust score, 민감도 라벨링이 필요하다.
- 에이전트가 파일/DB/배포/PC를 조작할 수 있으므로 "과도한 자율성"을 막는 승인 게이트가 1급 기능이어야 한다.

## 4. 목표 아키텍처: OHVIS Context Operating System

```
CEO Chat / Mobile / Telegram / PC Agent
        |
        v
Intent Router + Context Compiler
        |
        +--> Exact DB Lookup     (현재 상태, 비용, 작업, 세션)
        +--> Temporal Graph      (결정, 원인, 관계, 시간)
        +--> Vector/Full-text    (문서, 보고서, 대화 근거)
        +--> Web/Official Docs   (최신 외부 자료)
        |
        v
Durable Execution Engine
        |
        +--> Event Ledger
        +--> OHVIS Task Card
        +--> Artifact Registry
        +--> Evidence Store
        +--> Knowledge Promotion
        |
        v
Prompt/Policy/Code/Workflow Evolution
```

### 4.1 Event Ledger

모든 중요한 사건을 append-only로 저장한다.

| 이벤트 | 예시 |
|---|---|
| `user_instruction_created` | CEO 지시 수신 |
| `context_compiled` | 어떤 prompt asset과 memory가 들어갔는지 |
| `tool_call_requested` | 도구명, 입력 해시, 권한 판정 |
| `tool_result_saved` | 결과 요약, 원문 위치 |
| `artifact_created` | 보고서/코드/표/이미지 산출물 |
| `task_status_changed` | pending/running/approval/done/error |
| `deployment_verified` | health, active slot, rollback target |
| `knowledge_promoted` | evidence -> memory_fact/wisdom_rule 승격 |

권장 신규 테이블:

```sql
ohvis_events(
  id uuid primary key,
  event_type text not null,
  project text,
  session_id uuid,
  execution_id uuid,
  task_id uuid,
  artifact_id uuid,
  actor text,
  payload jsonb not null,
  idempotency_key text unique,
  created_at timestamptz default now()
);
```

### 4.2 Artifact Registry

현재 `chat_artifacts`는 23,728건으로 많지만, 작업물 생명주기 관점은 약하다. Artifact Registry는 "파일/보고서/패치/이미지/배포결과"를 하나의 버전형 객체로 본다.

필수 메타데이터:
- `artifact_type`: report, code_patch, table, design, deployment_result, screenshot, uploaded_file
- `source_event_id`
- `project`, `session_id`, `task_id`, `parent_turn_id`
- `storage_uri`, `sha256`, `version`, `supersedes_artifact_id`
- `visibility`: internal, ceo, customer, public
- `verification_status`: unverified, syntax_checked, tested, deployed, approved

### 4.3 Evidence Store

현재 가장 취약한 축이다. `research_archive`가 0건이라, 최신자료 확인 보고가 DB 원장에 남지 않는다.

권장 신규 테이블:

```sql
knowledge_sources(
  id uuid primary key,
  source_uri text not null,
  source_title text,
  publisher text,
  published_at timestamptz,
  accessed_at timestamptz not null,
  trust_level text,
  license text,
  checksum text,
  raw_snapshot_uri text,
  created_at timestamptz default now()
);

evidence_chunks(
  id uuid primary key,
  source_id uuid references knowledge_sources(id),
  project text,
  domain text,
  chunk_text text not null,
  metadata jsonb,
  sensitivity text default 'public',
  freshness_policy text,
  valid_from timestamptz,
  valid_to timestamptz,
  embedding vector,
  created_at timestamptz default now()
);
```

### 4.4 Temporal Context Graph

관계 예시:

| 주체 | 관계 | 대상 |
|---|---|---|
| CEO 지시 | created | task |
| task | modified | file |
| file change | caused | deployment |
| deployment | produced | error |
| error | resolved_by | patch |
| report | cites | evidence_chunk |
| prompt_asset | applied_in | compiled_prompt_provenance |
| memory_fact | superseded_by | newer_fact |

권장 edge table:

```sql
knowledge_edges(
  id uuid primary key,
  src_type text not null,
  src_id text not null,
  relation text not null,
  dst_type text not null,
  dst_id text not null,
  confidence numeric default 1.0,
  valid_from timestamptz default now(),
  valid_to timestamptz,
  evidence_event_id uuid,
  created_at timestamptz default now()
);
```

### 4.5 Retrieval Router

질문 유형별 검색 순서:

| 질문 | 1순위 | 2순위 | 3순위 | 웹 |
|---|---|---|---|---|
| 현재 상태 | DB exact | Docker/health/log | git status | 없음 |
| "왜 그랬나" | event ledger | graph edge | logs/messages | 필요 시 |
| 코드 구조 | `rg` source | semantic code | docs | 공식문서 |
| CEO 선호 | ai_meta_memory | ai_observations | chat history | 없음 |
| 최신 기술 | official docs | research papers | vendor docs | 필수 |
| 작업물 찾기 | artifact registry | chat_artifacts | git/docs | 없음 |

## 5. 스트리밍 끊김에 대한 원천 해결안

CEO 질문: "서버 재시작이면 응답이 자동으로 이어지게 할 수 있는 원천적인 방법은 없나?"

답: 가능하다. 현재도 일부 된다. 하지만 완전한 방식은 SSE 재연결이 아니라 **durable step replay**다.

### 5.1 현재 방식의 한계

| 현재 방식 | 한계 |
|---|---|
| partial을 `streaming_placeholder`에 저장 | 저장 주기 사이 토큰은 유실 가능 |
| 서버 재시작 후 `_resume_single_stream()` 재생성 | 모델이 이전과 완전히 같은 토큰을 이어 쓰지는 못함 |
| `chat_turn_executions.retry_count`로 루프 제한 | 실패 원인별 세밀한 재개점은 부족 |
| blue/green owner 분리 | 배포 안정성은 개선하지만 실행 단계 durable화는 아님 |

### 5.2 원천 구조

LLM 응답을 하나의 긴 스트림으로 보지 말고, 다음 단계로 쪼갠다.

1. `turn_received`
2. `context_compiled`
3. `plan_written`
4. `tool_call_1_started`
5. `tool_call_1_result_saved`
6. `draft_chunk_saved`
7. `final_answer_compiled`
8. `done_event_emitted`

각 단계는 DB에 저장되고 idempotency key를 가진다. 서버가 죽으면 마지막 완료 단계 다음부터 이어간다.

### 5.3 CEO 체감 개선

| 상황 | 현재 체감 | 개선 후 |
|---|---|---|
| 서버 재시작 | "응답 중단/이어쓰기 반복" | "작업 복구 중" 카드 1개, 단계별 재개 |
| 긴 도구 작업 | 채팅창이 멈춘 듯 보임 | 작업 카드가 진행률 표시 |
| 새로고침 | partial/도구카드 누락 가능 | event replay로 동일 화면 재구성 |
| 배포 중 질문 | active stream 보존은 되나 불안 | deploy drain + durable queue로 중단 없음 |

## 6. OHVIS가 할 수 있게 되는 일

### 6.1 지식 연결

- "지난번 GO100 보고서 어디 있지?" -> artifact registry에서 보고서/근거/후속 작업까지 표시
- "왜 이 지시를 했지?" -> CEO 메시지, 러너 작업, 코드 diff, 검증 결과, HANDOVER를 그래프로 연결
- "이 정책 지금도 맞나?" -> `valid_to`, `superseded_by`, 공식문서 accessed_at으로 최신성 판정
- "오비스가 자꾸 같은 실수 하는 이유?" -> error_pattern, quality_score, correction_directive, 실제 적용 provenance 대조

### 6.2 작업물 운영

- 보고서마다 원천 URL, 내부 DB 쿼리, 생성 모델, 비용, 검증 여부 저장
- 코드 수정마다 task, file, test, deploy, rollback target 연결
- 디자인 수정마다 screen_id, allowed_scope, forbidden_scope, screenshot, acceptance criteria 연결
- PC/엑셀/파일함 작업도 file snapshot과 수정 diff를 남김

### 6.3 능동형 CTO

- 오래된 지식 자동 감지: "OpenRouter 모델 가격/제약 최신 재조회 필요"
- 충돌 지식 자동 보고: "서버68 사용 안 함 결정과 현재 active container 운영 사실 충돌"
- 작업 완료 자동 판단: runner done -> 테스트/배포/문서/커밋 여부 확인 -> CEO에게 조건부 승인/반려 보고
- 비용 이상 탐지: 특정 세션 비용/토큰 급증 시 context window 압축 또는 작업 카드 전환

## 7. 구현 로드맵

### P0: 1~2주

| 작업 | 변경 대상 | 완료 기준 |
|---|---|---|
| `research_archive` 저장 활성화 | `app/services`, DB | 리서치 보고서 생성 시 sources/full_report/model/cost 저장 |
| `knowledge_sources`, `evidence_chunks` 추가 | migration | 외부 공식문서 1건이 source/chunk로 저장됨 |
| `ohvis_events` 추가 | migration + service | chat/tool/task/deploy 이벤트 5종 저장 |
| `ohvis_tasks` 실연동 | `pipeline_runner_service.py`, `chat_service.py` | 러너 시작/완료가 작업 카드에 자동 반영 |
| 스트리밍 상태 대시보드 | API + dashboard | running/retrying/interrupted stale 원인 표시 |

### P1: 2~4주

| 작업 | 변경 대상 | 완료 기준 |
|---|---|---|
| Artifact Registry 확장 | `chat_artifacts` 확장 또는 신규 table | task/report/file/deploy 산출물 버전 추적 |
| `knowledge_edges` 그래프 | migration + extractor | 지시->작업->파일->배포 edge 생성 |
| Retrieval Router | `context_builder.py`, `memory_recall.py` | 질문 유형별 exact/graph/vector/web 라우팅 |
| Context Compiler v2 | `compiled_prompt_provenance` 확장 | 어떤 evidence가 프롬프트에 들어갔는지 row로 검증 |
| 평가셋 | `knowledge_evals` | 검색 precision/citation coverage 측정 |

### P2: 1~2개월

| 작업 | 변경 대상 | 완료 기준 |
|---|---|---|
| Temporal invalidation | graph + memory_facts | 오래된 사실 자동 supersede |
| MCP tool 표준화 | AADS MCP server | PC Agent, DB, 파일, 브라우저 도구 표준 스키마 |
| A2A agent card | 프로젝트 매니저 | KIS/GO100/SF/NTV2/NAS agent card 공개 |
| Durable step replay | chat execution engine | 서버 재시작 후 tool step부터 재개 |
| 작업 카드 UI | dashboard | 멀티태스크 큐/진행/결과/판단 표시 |

### P3: 3~6개월

| 작업 | 목표 |
|---|---|
| Wisdom Rule Engine | 반복 검증된 지식을 정책/프롬프트/코드 변경 후보로 자동 제안 |
| Self-Audit CTO | 완료보고 전 커밋/푸시/배포/문서/검증 불일치 자동 차단 |
| Cross-project Context Mesh | 프로젝트별 지식 오염 없이 공통 교훈만 공유 |
| Proactive Briefing | 매일 아침 중요 변경, stale 작업, 충돌 지식, 비용 이상 자동 보고 |

## 8. 우선순위 권장안

| 우선순위 | 권장 | 이유 |
|---|---|---|
| P0-1 | `research_archive` + `knowledge_sources/evidence_chunks` | 최신자료 보고가 현재 DB 원장에 남지 않음 |
| P0-2 | `ohvis_events` append-only 원장 | "왜 그랬나"를 재구성할 근거가 필요 |
| P0-3 | `ohvis_tasks`와 러너/채팅 실행 연결 | 작업 진행/완료/판단을 대화와 분리해야 함 |
| P1-1 | Temporal Context Graph | 변경되는 사실과 결정의 시간 관계를 관리해야 함 |
| P1-2 | Durable step replay | 서버 재시작 중단 체감의 원천 해결 |

## 9. 외부 근거

| 출처 | 확인 내용 | URL |
|---|---|---|
| OpenAI Agents SDK docs, 2026-07-28 확인 | Agents SDK는 도구, MCP, 상태, guardrail, tracing, resumable approval flow를 다룸 | https://developers.openai.com/api/docs/guides/agents |
| OpenAI Conversation State docs, 2026-07-28 확인 | `previous_response_id`/conversation state를 제공하지만 이전 입력 토큰은 계속 과금됨 | https://developers.openai.com/api/docs/guides/conversation-state |
| OpenAI File Search docs, 2026-07-28 확인 | vector store 검색 결과 제한, include results, metadata filtering 지원 | https://developers.openai.com/api/docs/guides/tools-file-search |
| LlamaIndex Context Engineering guide, 2026-07-28 확인 | agent context는 RAG를 넘어 memory, tool selection, context window optimization까지 포함 | https://www.llamaindex.ai/blog/context-engineering-what-it-is-and-techniques-to-consider |
| MCP 2026-07-28 Release Candidate | stateless protocol core, Extensions, Tasks, MCP Apps, authorization hardening | https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/ |
| LangGraph docs, 2026-07-28 확인 | durable execution, streaming, human-in-the-loop, persistence가 핵심 | https://docs.langchain.com/oss/python/langgraph/overview |
| Google Agent Protocols guide, 2026-07-28 확인 | A2A agent card 기반 discovery/routing | https://developers.googleblog.com/developers-guide-to-ai-agent-protocols/ |
| Zep Graphiti, 2026-07-28 확인 | temporal context graph, fact invalidation, vector/full-text/graph retrieval 결합 | https://www.getzep.com/platform/graphiti/ |
| OWASP GenAI Top 10 2025 | prompt injection, sensitive disclosure, vector/embedding weakness 등 위험 | https://genai.owasp.org/llm-top-10/ |
| NIST AI RMF, 2026-07-28 확인 | NIST-AI-600-1 Generative AI Profile로 생성형 AI 위험 관리 제시 | https://www.nist.gov/itl/ai-risk-management-framework |

## 10. 최종 판정

OHVIS에 필요한 것은 새 외부 앱이 아니라 **AADS 내부 지식·작업 맥락 운영체계**다. 현재 AADS는 데이터가 이미 많고 실행 원장도 있으므로, 다음 단계는 저장소를 늘리는 일이 아니라 원천근거, 시간성, 관계, 검증, 적용 provenance를 연결하는 일이다.

가장 먼저 해야 할 일은 `research_archive=0` 상태를 해소하고, 모든 보고서와 최신자료 확인 결과를 `knowledge_sources/evidence_chunks`로 남기는 것이다. 그 다음 `ohvis_events`와 `ohvis_tasks`를 연결하면 "CEO 지시 -> 에이전트 작업 -> 산출물 -> 검증 -> 지식 승격" 흐름이 닫힌다.
