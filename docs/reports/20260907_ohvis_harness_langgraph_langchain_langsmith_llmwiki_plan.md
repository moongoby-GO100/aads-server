# OHVIS Harness / LangGraph / LangChain / LangSmith / LLM Wiki 전수 분석 및 반영 기획

- 작성 시각: 2026-09-07 09:48 KST
- 대상: OHVIS/AADS 실행 하네스, 에이전트 오케스트레이션, 도구 스킬, 관측, 지식 축적 구조
- CEO 질의 해석: "langchan"은 LangChain, "langsmich"는 LangSmith로 정정해 분석한다.
- 판정: OHVIS에는 LangGraph와 LangChain 기반 하네스가 이미 부분 반영되어 있고, LLM Wiki의 핵심 개념은 DB 메모리/지식그래프 형태로 부분 반영되어 있다. LangSmith는 패키지는 존재하지만 운영 관측은 Langfuse 중심이라 LangSmith 제품 기능은 미반영 상태다.

## 1. 요약

OHVIS는 현재 "LangGraph 실행 그래프 + LangChain 도구/모델 추상화 + MCP 도구 연결 + AADS DB 메모리" 구조를 갖고 있다. 그러나 최신 LangChain 생태계가 제시하는 하네스 구분, LangGraph durable execution/HITL, LangSmith traces/evals, LLM Wiki의 컴파일형 지식 구조를 한 제품 아키텍처로 닫지는 못했다.

가장 좋은 개선 방향은 기존 구현을 버리지 않고, OHVIS Harness를 4개 계층으로 재정의하는 것이다.

1. Execution Harness: LangGraph 기반 상태 그래프, checkpoint, interrupt, resume.
2. Tool/Skill Harness: LangChain `create_agent` 또는 기존 MCP tool adapter를 통한 표준 도구 정책.
3. Observability Harness: 현행 Langfuse를 유지하되 LangSmith 호환 trace/eval schema를 함께 저장.
4. Knowledge Harness: `memory_facts`/`project_memory`를 LLM Wiki 스타일의 문서-팩트-링크-오류북 구조로 승격.

## 2. 외부 최신 자료 요약

| 항목 | 최신 공식/연구 내용 | OHVIS 적용 의미 | 출처 |
|---|---|---|---|
| LangChain | `create_agent`를 "model + harness"로 설명하며, prompt/tools/middleware를 모델 루프 주변의 하네스로 정의한다. | OHVIS의 도구 스킬, 모델 라우팅, 승인 정책을 LangChain middleware 계약으로 표준화할 수 있다. | LangChain Docs, 2026-09-07 접근: https://docs.langchain.com/oss/python/langchain/overview |
| LangGraph | 장기 실행 stateful agent를 위한 low-level orchestration runtime이며 durable execution, streaming, human-in-the-loop, persistence를 핵심으로 둔다. | OHVIS loop/task/runner를 LangGraph graph + checkpoint + interrupt/resume으로 재정렬해야 한다. | LangGraph Docs, 2026-09-07 접근: https://docs.langchain.com/oss/python/langgraph/overview |
| LangGraph/LangChain 1.0 | LangGraph 1.0은 durable state, built-in persistence, HITL 패턴을 production agent 핵심 기능으로 제시한다. LangChain은 빠른 agent shipping, LangGraph는 복잡한 deterministic+agentic workflow에 적합하다. | OHVIS는 단순 agent loop보다 복잡한 운영 자동화가 많으므로 LangGraph를 core runtime으로 두고 LangChain은 tool/model harness로 쓰는 구성이 맞다. | LangChain Blog, 2025-10-22: https://www.langchain.com/blog/langchain-langgraph-1dot0 |
| Human-in-the-loop | LangChain HITL middleware는 tool call 정책에 따라 실행을 interrupt하고, LangGraph persistence로 안전하게 pause/resume한다. | CAPTCHA/OTP, 파일쓰기, DB변경, 배포, 금융주문 같은 고위험 도구를 `approve/edit/reject/respond` 정책으로 분류해야 한다. | LangChain Docs HITL, 2026-09-07 접근: https://docs.langchain.com/oss/python/langchain/human-in-the-loop |
| LangSmith | production trace, quality monitoring, dataset/eval 생성을 제공하고 OpenAI/Anthropic/CrewAI/Vercel AI SDK 등과 통합 가능하다. | OHVIS의 현재 Langfuse trace를 유지하더라도, trace->dataset->eval->개선 PR 흐름은 LangSmith식으로 보강할 가치가 있다. | LangSmith Docs, 2026-09-07 접근: https://docs.langchain.com/langsmith/observability |
| LLM Wiki | Karpathy의 LLM Wiki는 RAG처럼 매번 재검색하지 않고, raw/docs/schema 형태의 지속 지식 기반을 LLM이 컴파일/유지하게 하는 패턴이다. | OHVIS memory_facts를 단순 벡터 검색이 아니라 사람이 읽는 wiki page + source archive + schema/lint로 승격해야 한다. | Karpathy Gist, 2026-04-02: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f |
| LLM Wiki v2 | wiki가 썩지 않으려면 memory lifecycle, 최신성, 신뢰도, 반복 관찰 횟수, 오래된 사실 약화가 필요하다고 보강한다. | OHVIS의 Sleep-Time 정제, confidence, superseded_by를 wiki lifecycle 정책으로 명문화해야 한다. | LLM Wiki v2 Gist, 2026-09 접근: https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2 |
| LLM-Wiki 연구 | 문서를 구조화된 Wiki page와 양방향 링크로 컴파일하고 search/read/link-following tool을 노출하며 Error Book으로 자기수정한다. HotpotQA/MuSiQue/2WikiMultiHopQA/AuthTrace에서 2.0~8.1 F1 향상을 보고한다. | OHVIS는 `memory_facts`와 `knowledge_graph`를 link-following 가능한 tool surface로 노출하고, 오류 패턴을 Error Book으로 승격해야 한다. | arXiv 2605.25480v2, 2026-05-26: https://arxiv.org/html/2605.25480v2 |

## 3. 현재 OHVIS/AADS 실측

| 항목 | 실측 결과 | 판정 | 출처 |
|---|---:|---|---|
| LangGraph 의존성 | `langgraph>=1.1.6`, checkpoint/postgres 의존성 존재 | 반영됨 | `pyproject.toml` |
| LangChain 의존성 | `langchain-anthropic`, `langchain-openai`, `langchain-google-genai`, `langchain-mcp-adapters==0.2.0` 존재 | 반영됨 | `pyproject.toml` |
| LangSmith 패키지 | 컨테이너 import 가능 | 설치됨, 활용 미약 | `docker exec aads-server python -c importlib.util.find_spec` |
| Langfuse 관측 | `app/core/langfuse_config.py`에서 trace 생성, LiteLLM callback 설정 | LangSmith 대체 구현 | 코드 확인 |
| LangGraph StateGraph | PM -> Supervisor -> Architect -> Developer -> QA -> Judge -> DevOps -> END | 반영됨 | `app/graph/builder.py` |
| AADS State | messages, task_queue, checkpoint_stage, cost, QA/Judge/DevOps 결과 포함 | 반영됨 | `app/graph/state.py` |
| LiteLLM Runner | LangGraph ReAct + MCP + LangChain ChatOpenAI 기반 runner 존재 | 부분 반영 | `scripts/litellm_runner.py` |
| MCP 도구 연결 | MultiServerMCPClient로 filesystem/git/memory 연결 | 반영됨 | `app/mcp/client.py`, `scripts/litellm_runner.py` |
| OHVIS task 원장 | 총 265건, done 241, error 1, running 20, stale_cleaned 3 | 반영됨, running 정리 필요 | DB 조회 |
| OHVIS loop 원장 | 총 18건, active 4, paused 2, completed 7, cancelled 5 | 반영됨, active 감사 필요 | DB 조회 |
| Memory facts | 총 71,028건. GO100 34,617, AADS 17,702, NTV2 7,746 등 | LLM Wiki 기반 후보 | DB 조회 |
| 품질 점수 | 최근 7일 quality_score 기록 629건 | Reflexion/eval 후보 | DB 조회 |
| API route | `/api/v1/ops` 64개, `/api/v1/ohvis` 8개, `/api/v1/loops` 8개 | 운영 API 반영 | 컨테이너 route dump |
| Wiki 전용 테이블 | `wiki_*` 테이블 없음 | 미반영 | DB information_schema |

## 4. 네 가지 기술별 상세 설명

### 4.1 Harness 개념

하네스는 모델 자체가 아니라 모델 주변을 감싸는 실행 장치다. 최신 LangChain 문서의 표현을 OHVIS에 맞게 풀면 다음과 같다.

| 구성 | 의미 | OHVIS 대응 |
|---|---|---|
| Model | 실제 LLM 호출 | OpenAI/Anthropic/Gemini/LiteLLM 라우팅 |
| Prompt | 역할, 프로젝트, intent, 모델별 지시 | L1~L5 prompt_assets + prompt_compiler |
| Tools | DB, SSH, Git, 브라우저, PC Agent, Runner | tool_registry/tool_executor/MCP bridge |
| Middleware | tool 승인, 재시도, 가드레일, 비용 제한 | 일부는 구현, 표준 middleware 형태는 미흡 |
| State | 작업 상태, 메시지, 비용, 산출물, 판정 | AADSState, ohvis_tasks, ohvis_loops |
| Checkpoint | 중단 후 재개 지점 | LangGraph checkpointer 의존성은 있으나 OHVIS task/loop와 완전 통합은 미흡 |
| Observability | trace, span, latency, cost, eval | Langfuse 부분 구현, LangSmith식 eval loop 미흡 |
| Knowledge | 장기 기억, 문서, 링크, 검증근거 | memory_facts/knowledge_graph 부분 구현, LLM Wiki 미완 |

결론적으로 OHVIS Harness는 현재 "있다". 다만 각 요소가 따로 존재하고, `OHVISHarness`라는 명시적 모듈 계약으로 묶여 있지 않다.

### 4.2 LangGraph

LangGraph는 OHVIS에 가장 잘 맞는 핵심 런타임이다. 이유는 OHVIS 작업이 단순 질의응답이 아니라 장기 실행, 승인, 재개, 배포, 모니터링, 다중 에이전트 협업을 포함하기 때문이다.

현재 반영:

- `app/graph/builder.py`에서 Native `StateGraph`를 사용한다.
- `langgraph-supervisor`는 금지하고 native graph만 쓰는 규칙이 있다.
- PM, Supervisor, Architect, Developer, QA, Judge, DevOps, Researcher 8-agent 흐름이 구성되어 있다.
- `compile_graph(checkpointer=None)`는 checkpointer를 받을 수 있다.
- `app/graphs/ideation_subgraph.py`, `app/graphs/full_cycle_graph.py` 등 서브그래프가 존재한다.

부족한 점:

- OHVIS task/loop/runner가 모두 하나의 durable graph run id로 연결되어 있지 않다.
- `ohvis_tasks.running` 20건처럼 DB 상태와 실제 runner 상태가 어긋날 수 있다.
- HITL interrupt가 파일쓰기/DB변경/배포/OTP/CAPTCHA 정책에 일관되게 묶여 있지 않다.
- 장기 실행 loop가 LangGraph checkpoint 기반 pause/resume보다 자체 DB 스케줄러 중심으로 동작한다.

반영 시 좋은 점:

- 응답 중단, 서버 재시작, 모델 fallback 후에도 같은 task graph에서 이어가기 쉬워진다.
- CEO 승인 단계가 graph interrupt로 표준화된다.
- Runner 실패/승인/배포/롤백을 graph state로 추적할 수 있다.
- 각 프로젝트 Ops를 같은 graph template으로 실행할 수 있다.

### 4.3 LangChain

LangChain은 OHVIS에서 "고수준 agent harness와 도구/모델 추상화"로 쓰는 것이 맞다. LangChain agents는 LangGraph 위에서 동작하므로, 단순 도구 호출 루프에는 LangChain을 쓰고, 장기 업무 흐름은 LangGraph로 내리는 구조가 적합하다.

현재 반영:

- `app/llm/client.py`에 LangChain 계열 ChatAnthropic/ChatOpenAI/ChatGoogleGenerativeAI import가 있다.
- `scripts/litellm_runner.py`는 `ChatOpenAI(base_url=LiteLLM)`로 공급자 중립 호출을 수행한다.
- `langchain_mcp_adapters.client.MultiServerMCPClient`로 MCP 도구를 LangChain tool surface에 붙인다.
- 여러 agent 파일에서 `langchain_core.messages`를 사용한다.

부족한 점:

- tool policy가 LangChain middleware 형태로 통합되어 있지 않다.
- tool schema/permission/risk tier가 LangChain agent 생성 시 자동 주입되는 구조가 아니다.
- 프로젝트별 skill/runbook을 LangChain tool 또는 middleware로 승격하는 라이브러리가 없다.
- `create_react_agent` 사용이 runner 스크립트에 국한되어 제품 런타임 전체 표준은 아니다.

반영 시 좋은 점:

- 모델 공급자 변경이 쉬워진다.
- 각 프로젝트의 도구 스킬을 같은 schema로 노출할 수 있다.
- 승인 필요 tool, read-only tool, destructive-blocked tool을 middleware 정책으로 관리할 수 있다.
- 매장비서/마케팅/GO100/KIS 같은 프로젝트별 사이트 수집·운영 스킬을 재사용하기 쉬워진다.

### 4.4 LangSmith

LangSmith는 "도입하면 좋은 것"이지만, 현재 OHVIS에는 직접 운영 플랫폼으로 반영되어 있지 않다. 대신 Langfuse가 관측 계층으로 구현되어 있다.

현재 반영:

- 컨테이너에서 `langsmith` import는 가능하다.
- `langfuse>=3.0.0` 의존성과 `app/core/langfuse_config.py`가 존재한다.
- `create_trace()`가 session_id, user_id, metadata, input을 받아 trace를 생성한다.
- LiteLLM success/failure callback에 langfuse를 붙이는 코드가 있다.
- chat_service 내부에서 trace/span 생성 위치가 확인된다.

부족한 점:

- LangSmith API key/tracing 환경변수 기반의 실제 trace 전송 구현은 확인되지 않았다.
- trace를 dataset/eval/annotation queue로 승격하는 workflow가 없다.
- 실패 trace를 자동으로 개선 PR 또는 runner 지시서로 바꾸는 루프가 없다.
- CEO 화면에서 trace->원인->개선안->재검증 결과를 한 흐름으로 보지 못한다.

권장:

LangSmith를 바로 유료 운영 필수로 붙이기보다, 먼저 OHVIS 내부 trace schema를 LangSmith/Langfuse 양쪽에 호환되게 만든다. 이후 비용과 보안 요구에 따라 LangSmith SaaS를 선택적으로 켠다.

### 4.5 LLM Wiki

LLM Wiki는 OHVIS의 장기기억을 크게 개선할 수 있는 방향이다. 현재 OHVIS는 DB 기반 memory_facts가 많지만, 위키처럼 "컴파일된 문서, 링크, 원문, 오류북, lint"가 닫혀 있지는 않다.

현재 반영:

- `memory_facts` 71,028건이 프로젝트별 사실 저장소 역할을 한다.
- `project_memory`, `experience_memory`, `procedural_memory`, `system_memory`, `ai_meta_memory`가 존재한다.
- `app/memory/store.py`는 5-layer memory architecture를 주석과 API로 정의한다.
- `app/core/knowledge_graph.py`는 memory_facts에서 엔티티/관계를 추출하는 구조다.
- `quality_score`, Reflexion, Sleep-Time 정제 개념이 시스템 프롬프트와 문서에 반영되어 있다.

부족한 점:

- `wiki_pages`, `wiki_links`, `wiki_sources`, `wiki_error_book` 같은 전용 테이블이 없다.
- 보고서/코드/DB 사실이 자동으로 양방향 링크 문서로 컴파일되지 않는다.
- 원문 출처 snapshot과 fact confidence가 강하게 연결되지 않는다.
- 검색이 embedding top-k 중심이면 다중 hop 추론에서 약해질 수 있다.
- 위키 lint/중복/오래된 사실 약화/폐기 정책이 제품 기능으로 닫혀 있지 않다.

반영 시 좋은 점:

- CEO가 같은 질문을 던질 때 매번 새로 조사하지 않고 누적 지식을 활용할 수 있다.
- 프로젝트별 결정, 장애, 배포, 계약, 정책을 사람이 읽을 수 있는 문서로 유지할 수 있다.
- memory_facts의 양이 커져도 위키 링크와 출처로 탐색성이 좋아진다.
- 오류 패턴을 Error Book으로 남겨 같은 실패를 줄일 수 있다.

## 5. 반영/미반영 판정표

| 기술 | 현재 반영도 | 근거 | 미반영 핵심 |
|---|---|---|---|
| Harness | 부분 반영 | prompt_assets, tool_registry, AADSState, task/loop DB | 명시적 `OHVISHarness` 계약 부재 |
| LangGraph | 높음 | StateGraph, checkpointer 인자, 8-agent graph, graph tests | task/runner/loop를 durable graph run으로 통합 미흡 |
| LangChain | 중간 | LangChain model/messages/tools/MCP adapter 사용 | middleware/tool policy/skill library 표준화 미흡 |
| LangSmith | 낮음 | 패키지 import 가능 | 실제 LangSmith trace/eval/annotation workflow 미확인 |
| Langfuse | 중간 | trace config, LiteLLM callback | eval dataset/자동 개선 루프 미흡 |
| LLM Wiki | 중간 이하 | memory_facts 71,028건, knowledge_graph, memory layers | wiki page/link/source/error book/lint 테이블 없음 |

## 6. 목표 아키텍처

```text
CEO / Chat / Dashboard / Mobile OHVIS
        |
        v
OHVIS Harness Kernel
        |
        +-- LangGraph Runtime
        |      - graph_run_id
        |      - checkpoint
        |      - interrupt/resume
        |      - task/loop/runner state
        |
        +-- LangChain Tool & Skill Harness
        |      - model abstraction
        |      - tool registry adapter
        |      - HITL middleware policy
        |      - project skill packs
        |
        +-- Observability Harness
        |      - Langfuse current trace
        |      - LangSmith-compatible trace schema
        |      - eval dataset
        |      - failure-to-directive pipeline
        |
        +-- LLM Wiki Knowledge Harness
               - source archive
               - wiki pages
               - bidirectional links
               - memory_facts promotion
               - error book
               - lint/decay/supersede
```

## 7. 개선안

### P0. OHVIS Harness Kernel 명시화

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| `app/services/ohvis_harness.py` 추가 | graph/task/tool/trace/wiki를 묶는 실행 계약 정의 | unit test에서 task 생성->tool policy->trace id->result 저장 확인 |
| `graph_run_id` 도입 | `ohvis_tasks`, `ohvis_loops`, `pipeline_jobs`를 같은 실행 id로 연결 | running task와 runner terminal 불일치 0건 |
| HITL 정책 표준화 | approve/edit/reject/respond를 tool risk tier에 매핑 | 파일쓰기/DB변경/배포/OTP/CAPTCHA 정책 테스트 통과 |

### P1. LangGraph durable task 전환

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| task graph template | report/audit/code/deploy/browser-collection graph template 정의 | 주요 intent 5종 graph template 존재 |
| checkpoint 저장소 고정 | Postgres checkpointer를 운영 설정으로 연결 | 서버 재시작 후 graph resume smoke test 통과 |
| loop executor 정렬 | `ohvis_loops` 실행을 LangGraph node 기반으로 전환 | active loop 4건 상태가 실제 next_run/worker와 일치 |

### P1. LangChain skill middleware

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| `ops_skill_library` 테이블 | skill slug, scope, allowed tools, risk tier, validation 저장 | AADS/KIS/GO100/SF/NTV2/NAS 공통 skill 8종 seed |
| tool risk tier | read/write/deploy/financial/auth/security 등 분류 | 모든 도구에 risk tier 누락 0건 |
| middleware adapter | LangChain agent 생성 시 승인 정책 자동 주입 | 고위험 tool 호출 interrupt 테스트 통과 |

### P1. Observability 강화

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| trace schema 통합 | Langfuse current + LangSmith-compatible fields 저장 | trace_id, run_id, tool_calls, latency, cost, error 저장 |
| eval dataset 생성 | 실패/저품질 응답을 eval case로 자동 승격 | quality_score < 0.4 샘플이 eval_cases에 적재 |
| failure-to-directive | 반복 실패 패턴을 runner 지시서 초안으로 변환 | error_pattern 상위 10개에 개선 지시서 생성 |

### P1. LLM Wiki Knowledge Harness

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| wiki schema | `wiki_sources`, `wiki_pages`, `wiki_links`, `wiki_error_book` 추가 | 마이그레이션 + read API 통과 |
| report compiler | 보고서/웹자료/코드검수 결과를 wiki page로 컴파일 | 신규 보고서 저장 시 wiki page 자동 생성 |
| link-following tools | search/read/follow/link/sufficiency tool 제공 | 다중 문서 질문에서 link traversal 로그 확인 |
| Error Book | 잘못된 사실/오래된 사실/중복 fact를 기록 | 동일 오류 재발 시 Error Book constraint 주입 |

### P2. CEO 화면 반영

| 화면 | 추가 기능 | 완료 기준 |
|---|---|---|
| `/ops` | Harness health, active graph runs, stale task, trace errors | 6개 프로젝트 상태 카드 표시 |
| `/ops/memory` | LLM Wiki page/link/source/error book 뷰 | wiki page 열람/검색/링크 이동 |
| `/chat` | 현재 응답의 graph_run_id, source wiki, trace 요약 | 답변 근거 추적 가능 |
| `/admin/prompts` | skill/harness 적용 provenance | prompt+skill+tool policy 적용 이력 확인 |

## 8. 프로젝트별 활용 방식

| 프로젝트 | 활용 방식 | 우선 적용 |
|---|---|---|
| AADS/OHVIS | 모든 chat/runner/loop를 OHVIS Harness로 통합 | P0 |
| 매장비서 | 로그인 사이트 수집, CAPTCHA/OTP HITL, 매장별 site_profile skill | P1 |
| 마케팅 | 광고/리뷰/검색콘솔 데이터 수집 recipe, trace/replay | P1 |
| GO100 | 장중 전략 점검 graph, 금융 tool 승인 정책, 운영 wiki | P1 |
| KIS | 주문/체결/계좌 tool 고위험 HITL, 장중 checkpoint | P1 |
| SF | 영상 생성 queue, 실패 trace, 원본/결과 wiki | P2 |
| NTV2 | 커머스/AI Studio/입점 운영 runbook wiki | P2 |
| NAS | 이미지 QC/rsync pipeline trace와 wiki error book | P2 |

## 9. 지시서 초안

>>>DIRECTIVE_START
TASK_ID: AADS-OHVIS-HARNESS-001
TITLE: OHVIS Harness Kernel 및 LangGraph/LangChain/LangSmith-compatible/LLM Wiki 통합 기반 구현
PRIORITY: P1-HIGH
SIZE: L
DESCRIPTION:
1. `app/services/ohvis_harness.py`를 추가해 OHVIS task, loop, runner, tool policy, trace, memory/wiki 연결 계약을 정의한다.
2. `ohvis_tasks`, `ohvis_loops`, `pipeline_jobs`를 `graph_run_id` 또는 동등한 실행 식별자로 연결하는 마이그레이션과 backward-compatible API를 구현한다.
3. LangGraph checkpoint/interrupt/resume을 report/audit/code/deploy/browser-collection 5개 intent template에 적용한다.
4. LangChain tool middleware adapter를 구현해 도구별 risk tier와 HITL decision type(approve/edit/reject/respond)을 표준화한다.
5. 현행 Langfuse trace를 유지하면서 LangSmith-compatible trace/eval schema를 내부 DB에 저장한다. 외부 LangSmith 전송은 환경변수로 선택 가능하게 하며 기본값은 off로 둔다.
6. LLM Wiki 최소 스키마(`wiki_sources`, `wiki_pages`, `wiki_links`, `wiki_error_book`)와 search/read/follow tool을 구현한다.
7. `memory_facts`와 신규 wiki page를 양방향 연결하고, 보고서 저장 시 source archive와 wiki page를 자동 생성한다.
8. `/ops`와 `/ops/memory`에 Harness health, active graph run, stale task, trace errors, wiki page/link/error book 화면을 추가한다.
9. 검증: unit tests, DB migration dry-run, route import, API smoke, protected dashboard route, blue-green 배포, 5분 P0/P1 모니터링을 수행한다.
>>>DIRECTIVE_END

## 10. 완료 기준

| 기준 | 성공 조건 |
|---|---|
| 하네스 계약 | `OHVISHarness`에서 task 생성, tool policy 적용, trace 기록, result 저장이 한 흐름으로 테스트됨 |
| LangGraph | graph checkpoint 후 강제 중단/재개 smoke test 통과 |
| LangChain | 고위험 tool 호출이 HITL interrupt로 멈추고 승인 후 실행됨 |
| LangSmith-compatible | 외부 전송 off 상태에서도 trace/eval DB row가 생성됨 |
| LLM Wiki | 보고서 1건이 wiki source/page/link로 컴파일되고 search/read/follow tool로 조회됨 |
| 운영 화면 | `/ops`, `/ops/memory`, `/chat`에서 graph_run/trace/wiki 근거가 표시됨 |
| 배포 | backend/dashboard blue-green, same digest, external health, 5분 P0/P1 모니터링 통과 |

## 11. 리스크와 통제

| 리스크 | 통제 |
|---|---|
| LangSmith 외부 SaaS 비용/데이터 반출 | 기본 off, 내부 Langfuse/DB trace 우선, 민감값 masking |
| LangGraph 전환 중 기존 runner 불안정 | 기존 `pipeline_runner` 유지, 신규 graph template는 intent별 점진 적용 |
| Wiki DB 증가 | source archive 압축, page summary, link index, 오래된 fact decay |
| 자동 승인 오남용 | risk tier별 HITL 정책, CEO 승인 로그, destructive action block |
| 기존 dirty와 충돌 | Runner worktree 격리, 직접 수정 금지, 문서/코드 커밋 분리 |

## 12. 최종 판정

OHVIS는 이미 LangGraph/LangChain 기반의 초석을 갖고 있으므로 새 시스템을 사오는 방향보다 내부 Harness Kernel로 정리하는 것이 맞다. LangSmith는 당장 필수 도입보다 LangSmith-compatible schema와 eval workflow부터 내부화하고, LLM Wiki는 기존 `memory_facts` 71,028건을 사람이 읽고 에이전트가 traversable하게 쓰는 지식 운영체계로 승격하는 것이 가장 효과가 크다.

---

## 13. 2026-09-07 09:55 KST 추가 검수: Hermes와 Skill Find 반영

CEO 추가 지시로 Hermes와 Skill Find/Tool Search를 추가 조사해 반영한다. 이 부록은 기존 보고서의 네 축(LangGraph, LangChain, LangSmith, LLM Wiki)을 7축(Harness, LangGraph, LangChain, LangSmith, LLM Wiki, Hermes, Skill Find)으로 확장한다.

### 13.1 최신 실측 정정

| 항목 | 최신 실측 | 기존 보고서 대비 정정 |
|---|---:|---|
| 기준 시각 | 2026-09-07 09:55:31 KST | 최신 재측정 |
| 실행 컨테이너 | `aads-server`, `aads-server-green`, `aads-dashboard`, `aads-dashboard-green` healthy | 운영 중 확인 |
| `memory_facts` | 총 71,057건 | 기존 71,028건에서 증가 |
| 프로젝트별 memory_facts | GO100 34,643 / AADS 17,705 / NTV2 7,746 / CEO 3,363 / KIS 2,980 | 최신 DB 재조회 |
| `ohvis_tasks` | done 241 / error 1 / running 20 / stale_cleaned 3 | running 정리 필요 유지 |
| `ohvis_loops` | active 4 / paused 2 / completed 7 / cancelled 5 | 최신 DB 재조회 |
| `prompt_assets` | 총 141건 / enabled 140건 / ops 관련 9건 / skill 관련 0건 | 스킬 메타는 prompt_assets에 없음 |
| `wiki_*`, `skill_*`, `trace_*`, `eval_*`, `hermes_*` 테이블 | 0건 | 전용 제품 테이블 미반영 |
| 컨테이너 import | `langgraph=True`, `langchain_core=True`, `langchain_mcp_adapters=True`, `langsmith=True`, `langfuse=True`, `langchain=False` | "LangChain 전체 패키지 도입"이 아니라 Core/Provider/MCP 부분 도입으로 정정 |
| 로컬 SKILL.md | `.claude/skills/tpp`, `.claude/skills/handoff`, `.claude/skills/sales-channel-collector` 3개 | repo-local 스킬은 일부 존재 |

### 13.2 Hermes 상세 설명

이번 "헤르메스"는 두 가지를 분리해야 한다.

| 구분 | 설명 | OHVIS 적용 판단 |
|---|---|---|
| Hermes Agent | Nous Research의 self-improving autonomous agent runtime. memory, autonomous skill creation, skill improvement, messaging gateway, cron, subagents, MCP, remote terminal backend를 제공한다. | 참고 가치 높음. OHVIS를 대체하지 말고 자가개선/스킬/멀티채널/원격실행 패턴을 흡수하는 것이 맞다. |
| Hermes 모델 | Nous Research의 Hermes 4 등 오픈 모델 계열. tool use, reasoning, local inference 후보로 볼 수 있다. | 운영 핵심 모델로 즉시 전환하지 말고, 별도 벤치 후 저비용/로컬 fallback 후보로만 검토한다. |
| React Native Hermes | JavaScript engine/parser 계열. AADS dashboard `node_modules/hermes-parser`, `hermes-estree`가 여기에 해당한다. | OHVIS agent/harness와 무관하다. |

공식/원천 자료 기준으로 Hermes Agent는 "폐쇄 학습 루프"가 강점이다. 메모리를 스스로 축적하고, 복잡한 작업 경험에서 skill을 만들며, 사용 중 skill을 개선하고, 과거 대화를 검색해 요약하고, Telegram/Discord/Slack/WhatsApp/CLI 등 여러 채널에서 동작하며, cron 자동화와 subagent 병렬화를 제공한다.

OHVIS에는 이 기능들이 산발적으로 이미 있다. 예를 들어 `memory_facts`, `pipeline_runner`, `spawn_subagent`, `schedule_task`, PC Agent, 모바일 OHVIS, MCP bridge가 있다. 그러나 Hermes처럼 "자가 스킬 생성 -> 스킬 개선 -> 기억 저장 -> 반복 실행"이 하나의 닫힌 루프로 연결되어 있지는 않다.

#### Hermes에서 OHVIS로 가져올 것

| Hermes 패턴 | OHVIS 적용 방식 | 기대효과 |
|---|---|---|
| Closed learning loop | 완료 작업에서 reusable skill 후보를 자동 추출하고 실패 작업에서 Error Book을 생성 | 같은 실패 반복 감소 |
| Autonomous skill creation | 동일 유형 작업 3회 이상 성공 시 `SKILL.md` 또는 DB skill draft 생성 | 운영 노하우 자동 자산화 |
| Skill self-improvement | 스킬 실행 중 실패한 selector, command, 검증 누락을 patch 후보로 저장 | 스킬이 시간이 지날수록 정확해짐 |
| Cross-session recall | memory_facts + chat history에 lexical recall/summary index 추가 | 과거 결정을 더 빠르게 회수 |
| Messaging gateway | 모바일/push/Telegram/Slack을 같은 task terminal event에 연결 | 완료/중단/승인요청 알림 일관화 |
| Bot mode | 프로젝트별 Ops Bot, QA Bot, Research Bot, Deploy Bot을 role/session/template로 제품화 | 6개 프로젝트 운영 역할 분리 |
| Remote backends | AADS/KIS/GO100/SF/NTV2/NAS 서버 실행환경을 backend profile로 추상화 | 프로젝트 오인/경로 혼선 감소 |
| Natural-language cron | 시장 시작 점검, 배포 후 5분 모니터링, 일일 보고를 자연어 스케줄로 등록 | CEO 운영 자동화 강화 |

#### Hermes 도입 시 피해야 할 것

- 외부 Hermes Agent를 AADS 운영 서버에 직접 붙여 권한·시크릿·배포 제어를 넘기는 방식은 피한다.
- AADS의 DB-fenced execution, blue-green release, active_project isolation과 충돌할 수 있으므로 Hermes 런타임 통합은 별도 sandbox 실험으로 제한한다.
- 모델 Hermes 계열은 품질/비용/보안 벤치 전까지 실매매, 배포, 계정 자동화 판단에는 쓰지 않는다.

### 13.3 Skill Find / Tool Search 상세 설명

CEO가 말한 "스킬 파인드"는 다음 두 층으로 해석한다.

1. Codex/ChatGPT Skills: `SKILL.md` manifest와 optional scripts/references/assets를 가진 재사용 워크플로우다. Codex는 `$skill-name`, `/skills`, 또는 description 기반 implicit matching으로 스킬을 활성화한다.
2. OpenAI Tool Search: 모든 도구 정의를 선로딩하지 않고, 필요할 때 검색해 모델 context 끝에 주입하는 lazy tool discovery다. 목적은 token/cost/latency를 줄이면서 필요한 도구만 쓰게 하는 것이다.

현재 OHVIS/AADS 반영:

| 항목 | 상태 | 근거 |
|---|---|---|
| 로컬 스킬 파일 | 부분 반영 | `.claude/skills/tpp`, `.claude/skills/handoff`, `.claude/skills/sales-channel-collector` |
| 스킬 작성 규칙 | 반영 | SKILL.md front matter와 instructions 사용 |
| PC Agent 판매채널 스킬 | 반영 | CAPTCHA/OTP 우회 금지, 승인 범위 자동화, 같은 work_key 재개 원칙 포함 |
| Skill registry DB | 미반영 | `skill_*` 테이블 0건 |
| Skill search API | 미반영 | 전용 `/api/v1/skills/search` 확인 안 됨 |
| Skill provenance | 미반영 | 응답/러너에 사용 skill slug/version/hash 저장 구조 미확인 |
| Tool Search형 lazy loading | 미반영 | AADS 자체 도구/스킬 정의를 runtime search로 주입하는 제품 기능 없음 |

#### Skill Find를 OHVIS에 반영하면 좋은 점

- AADS/KIS/GO100/SF/NTV2/NAS 전체 운영 지시를 시스템 프롬프트에 계속 넣지 않아도 된다.
- "GO100 장 시작 확인", "매장비서 배민 수집", "AADS 대시보드 배포", "NTV2 입점계약서 생성"처럼 요청이 들어오면 필요한 스킬만 검색해 즉시 읽고 실행할 수 있다.
- active_project, project_scope, role_scope, risk_tier를 같이 보므로 프로젝트 오인과 무단 실행을 줄일 수 있다.
- 스킬별 검증 명령, 금지 조건, 승인 조건을 강제할 수 있어 완료 보고 품질이 올라간다.
- 스킬을 scripts/references/assets와 함께 관리하면 단순 프롬프트보다 반복 실행 품질이 안정된다.

### 13.4 7축 반영/미반영 최종 판정표

| 기술 | 현재 반영도 | 근거 | 미반영 핵심 | 우선순위 |
|---|---|---|---|---|
| Harness | 중간 | prompt_assets, tool_executor, AADSState, tasks/loops, runner | 단일 `OHVISHarness` 실행 계약 없음 | P0 |
| LangGraph | 높음 | StateGraph, checkpointer, 8-agent graph | task/runner/loop durable run 통합 미흡 | P0 |
| LangChain Core | 중간 | `langchain_core`, provider packages, MCP adapter | middleware/tool policy/skill adapter 미흡 | P1 |
| LangChain meta package | 낮음 | 컨테이너 `langchain=False` | 전체 LangChain package import 불가 | P2 |
| LangSmith | 낮음 | `langsmith=True` import 가능 | 실제 trace/eval 전송·dataset loop 없음 | P1 |
| Langfuse | 중간 | `langfuse_config.py`, tests | trace를 eval/개선 지시서로 승격하지 않음 | P1 |
| LLM Wiki | 중간 이하 | memory_facts 71,057건, knowledge_graph | wiki 테이블/API/tool/lint 없음 | P1 |
| Hermes Agent | 낮음 | 핵심 코드 도입 없음 | closed learning loop, auto skill, multi gateway 없음 | P1 |
| Hermes model | 낮음 | 운영 라우팅 근거 없음 | 벤치/비용/보안 검증 없음 | P3 |
| Skill Find | 낮음 | 로컬 SKILL.md 3개 | DB registry/search/provenance/execution gate 없음 | P0 |

### 13.5 개선안 확장

#### P0. Skill Find Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| skill registry schema | `ops_skill_library`, `ops_skill_versions`, `ops_skill_runs` 추가 | 마이그레이션 + seed + read API 통과 |
| repository skill indexer | `.claude/skills`, `.codex/skills`, docs runbook 스캔 | 현행 3개 로컬 스킬이 DB에 색인됨 |
| skill search API | project/intent/query 기반 top-k 반환 | "매장비서 배민 수집" 요청에서 `sales-channel-collector` top-1 |
| skill provenance | 응답/러너에 사용 skill 기록 | 완료 보고에 skill slug/version/hash 표시 |
| risk gate | 스킬별 allowed tools와 승인 정책 적용 | deploy/financial/auth/security 스킬은 승인 전 실행 차단 |

#### P1. Hermes Pattern Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| auto skill draft | 반복 성공 작업에서 SKILL.md/DB skill 후보 생성 | 동일 패턴 3회 성공 시 draft 생성 |
| skill improvement loop | 스킬 실패 원인을 skill patch 후보로 저장 | 실패 후 재실행 성공률 추적 |
| bot mode | 프로젝트별 Ops/QA/Research/Deploy specialist profile | `/ops/bots` 화면에서 역할/권한 확인 |
| messaging gateway abstraction | mobile/push/telegram/slack 등 알림 채널을 task state와 연결 | task terminal 이벤트가 채널별로 동일 payload 발송 |
| scheduled automation | natural-language cron -> OHVIS loop/task 생성 | 장 시작/배포후모니터링/일일보고 자동 생성 |

### 13.6 프로젝트별 활용 예시

| 프로젝트 | Skill/Harness 적용 | 필요한 스킬 예시 |
|---|---|---|
| AADS | 배포, 프롬프트, 러너, 문서, 장애 대응 | `aads-bluegreen-release`, `prompt-provenance-audit`, `runner-recovery`, `docs-publish-check` |
| GO100 | 장 시작 점검, 주문/데이터 수집, 전략 검수 | `go100-market-open-check`, `go100-entry-zero-audit`, `go100-order-risk-gate` |
| KIS | 계좌/주문/브로커 상태, 자동매매 운영 | `kis-broker-health`, `kis-order-ledger-audit`, `kis-risk-stop` |
| SF | 영상 생성 큐, 크롤링, 배포/헬스 | `sf-video-pipeline-health`, `sf-source-collector` |
| NTV2 | 입점/계약/AI Studio/상품 운영 | `ntv2-merchant-contract`, `ntv2-ai-studio-qa`, `ntv2-product-onboarding` |
| NAS | 이미지 처리, 스토리지, 내부망 점검 | `nas-image-job-health`, `nas-storage-capacity-audit` |
| 매장비서/마케팅 | 로그인 사이트 수집, CAPTCHA/OTP 승인 재개 | `sales-channel-collector`, `authenticated-site-collector`, `marketing-channel-import` |

### 13.7 확장 지시서 초안

```text
>>>DIRECTIVE_START
TASK_ID: AADS-OHVIS-HARNESS-SKILL-WIKI-HERMES-001
TITLE: OHVIS Harness Kernel, Skill Find, LLM Wiki, Hermes Pattern 통합 기반 구현
PRIORITY: P1-HIGH
SIZE: XL
DESCRIPTION:
1. 현재 AADS/OHVIS의 LangGraph, LangChain Core/MCP adapter, Langfuse, memory_facts, .claude/skills 구조를 보존한다.
2. `OHVISHarness` 실행 계약을 추가해 chat/task/loop/runner/tool/trace/wiki/skill을 `graph_run_id` 기준으로 연결한다.
3. Skill Find Layer를 구현한다: `ops_skill_library`, `ops_skill_versions`, `ops_skill_runs` 테이블, `.claude/skills` indexer, `/api/v1/skills/search`, skill provenance 저장.
4. LangGraph checkpoint/interrupt/resume을 report/audit/code/deploy/browser-collection 5개 intent template에 적용한다.
5. LangChain Core/MCP adapter 기반 tool middleware를 구현해 tool risk tier와 HITL decision type(approve/edit/reject/respond)을 표준화한다.
6. 현행 Langfuse trace를 유지하면서 LangSmith-compatible trace/eval schema를 내부 DB에 저장한다. 외부 LangSmith 전송은 환경변수 opt-in이며 기본 off로 둔다.
7. LLM Wiki 최소 스키마(`wiki_sources`, `wiki_pages`, `wiki_links`, `wiki_error_book`)와 search/read/follow tool을 구현한다.
8. Hermes Agent의 패턴만 흡수한다: auto skill draft, skill self-improvement, bot mode profile, messaging gateway abstraction, scheduled automation. 외부 Hermes Agent 런타임 도입은 하지 않는다.
9. `/ops/harness`, `/ops/skills`, `/ops/memory`, `/ops/traces` CEO 화면 초안을 구현한다.
10. 기존 unrelated dirty 파일은 건드리지 말고 isolated worktree 또는 Runner 의존성 그래프로 진행한다.
11. 검증: DB migration 전후 SELECT, unit tests, API smoke, dashboard route screenshot 또는 API 폴백, blue-green 배포 전 clean SHA 확인, 배포 시 5분 P0/P1 모니터링.
>>>DIRECTIVE_END
```

### 13.8 최종 권장

가장 먼저 구현할 것은 LangSmith 외부 연동이 아니라 `Skill Find Layer + OHVIS Harness Kernel`이다. 이 두 가지가 들어가야 이후 LangGraph durable execution, LLM Wiki, Hermes형 자가개선 루프가 프로젝트별로 안전하게 확장된다.

## 14. 2026-09-07 10:34 KST 즉시 구현 반영

CEO 지시에 따라 보고서의 P0 기반을 코드로 전환했다. 이번 반영은 운영 DB를 즉시 변경하지 않고도 API가 뜨도록 graceful fallback을 포함한다.

| 구분 | 반영 파일 | 구현 내용 | 상태 |
|---|---|---|---|
| OHVIS Harness Kernel | `app/services/ohvis_harness.py` | 하네스 구성요소, LangGraph/LangChain/LangSmith/Langfuse import 상태, DB readiness, risk policy를 한 API 계약으로 노출 | 구현 |
| Skill Find | `app/services/ohvis_harness.py`, `app/api/ohvis_harness.py` | 프로젝트/intent/query 기반 skill 추천, `.claude/skills`/`.codex/skills` 스캔, 승인 정책 반환 | 구현 |
| LLM Wiki | `app/services/ohvis_harness.py` | `ohvis_wiki_pages`가 있으면 wiki 검색, 없으면 `memory_facts`로 fallback 검색 | 구현 |
| Hermes Pattern | `app/services/ohvis_harness.py` | recall → skill 선택 → risk gate → learn → self-improve 폐쇄 루프 추천 API | 구현 |
| LangSmith-compatible 기반 | `migrations/158_ohvis_harness_skill_wiki_foundation.sql` | `ohvis_harness_traces` 테이블 설계, 외부 LangSmith 전송은 opt-in 전제로 분리 | 마이그레이션 파일 추가 및 DB 적용 |
| API | `app/api/ohvis_harness.py`, `app/main.py` | `/api/v1/ohvis/harness/status`, `/policies`, `/skill-find`, `/wiki/search`, `/hermes/recommend` 등록 | 구현 |
| 테스트 | `tests/unit/test_ohvis_harness.py` | Skill Find, Hermes guardrail, repo skill scan, destructive/deploy risk gate 단위 테스트 | 추가 |

### 추가된 API

| Method | Path | 용도 |
|---|---|---|
| GET | `/api/v1/ohvis/harness/status?project=AADS` | OHVIS 하네스 구성요소와 DB foundation readiness 조회 |
| GET | `/api/v1/ohvis/harness/policies` | read/write/deploy/auth/financial/destructive risk policy 조회 |
| POST | `/api/v1/ohvis/harness/skill-find` | 자연어 작업에 맞는 프로젝트별 skill 후보 검색 |
| POST | `/api/v1/ohvis/harness/wiki/search` | LLM Wiki 검색, 미마이그레이션 시 `memory_facts` fallback |
| POST | `/api/v1/ohvis/harness/hermes/recommend` | Hermes식 자가개선 실행 순서와 guardrail 추천 |

### 남은 구현

이번 작업은 P0 foundation이다. 운영 DB에는 foundation 테이블 8개와 기본 skill 9건이 적용됐다. 다음 단계에서는 repository skill indexer 자동 동기화, skill run provenance 저장, graph_run_id를 runner/task/loop에 실제 연결, `/ops/harness`와 `/ops/skills` 화면 반영이 필요하다.
