# OHVIS Harness / LangGraph / LangChain / LangSmith / LLM Wiki / Hermes / Skill Find 검증 보고서

- 작성 시각: 2026-09-07 10:12 KST
- 대상: AADS/OHVIS 하네스, 에이전트 오케스트레이션, 관측, 지식기반, 스킬 탐색/활용 구조
- 용어 정정: CEO 질의의 `langchan`은 LangChain, `langsmich`는 LangSmith로 정정해 분석한다.
- 결론: OHVIS에는 LangGraph, LangChain Core, MCP, Langfuse, 메모리/지식그래프의 기반이 이미 있다. 그러나 최신 하네스 제품 구조인 durable graph run, HITL middleware, trace/eval loop, LLM Wiki/OpenWiki, Hermes식 self-improving skills, Skill Find registry는 아직 하나의 운영 커널로 닫히지 않았다.

## 1. 핵심 요약

OHVIS는 "없는 상태"가 아니라 "부분 구현이 흩어져 있는 상태"다. 현재 구현은 LangGraph 기반 다중 에이전트 그래프, LangChain Core 메시지/모델 provider, MCP tool adapter, Langfuse trace, `memory_facts`/`kg_entities` 기반 지식 저장소를 갖고 있다.

우선순위는 LangSmith 외부 SaaS를 먼저 붙이는 것이 아니다. 먼저 `OHVISHarness` 실행 계약과 `Skill Find Layer`를 만들어 chat/task/loop/runner/tool/trace/wiki/skill을 같은 `graph_run_id`로 묶어야 한다. 그 다음 LangGraph checkpoint/resume, LLM Wiki/OpenWiki, Hermes형 self-improvement를 단계적으로 연결하는 것이 맞다.

## 2. 현재 실측값

| 항목 | 실측값 | 판정 | 출처 |
|---|---:|---|---|
| 기준 시각 | 2026-09-07 10:07:05 KST | 최신 측정 | [DB 조회: `now() at time zone 'Asia/Seoul'`] |
| `ohvis_tasks` | 총 266건 | 반영됨 | [DB 조회] |
| `ohvis_tasks.done` | 241건 | 정상 누적 | [DB 조회] |
| `ohvis_tasks.running` | 21건 | 정리/동기화 필요 | [DB 조회] |
| `ohvis_tasks.error` | 1건 | 낮음 | [DB 조회] |
| `ohvis_tasks.stale_cleaned` | 3건 | stale 정리 경험 있음 | [DB 조회] |
| `ohvis_loops` | 총 18건 | 반영됨 | [DB 조회] |
| `ohvis_loops.active` | 4건 | 감사 필요 | [DB 조회] |
| `ohvis_loop_iterations` | 36건 | 루프 실행 흔적 있음 | [DB 조회] |
| `memory_facts` | 71,067건 | LLM Wiki 후보 기반 충분 | [DB 조회] |
| 상위 프로젝트 memory | GO100 34,648 / AADS 17,705 / NTV2 7,746 / CEO 3,368 / KIS 2,980 | 프로젝트별 편중 있음 | [DB 조회] |
| `prompt_assets` | 총 141건 / enabled 140건 | 프롬프트 계층 반영됨 | [DB 조회] |
| Ops 유사 prompt asset | 9건 | Ops 프롬프트 일부 반영 | [DB 조회] |
| Skill 유사 prompt asset | 0건 | Skill 전용 프롬프트 미반영 | [DB 조회] |
| `wiki_*`, `skill_*`, `hermes_*`, `trace_*`, `eval_*` 테이블 | 0건 | 전용 제품 테이블 없음 | [DB 조회] |
| API route | `/api/v1/ops` 64개, `/api/v1/ohvis` 8개, `/api/v1/loops` 8개, `skill` 0개, `wiki` 0개 | Ops/Loop는 있음, Skill/Wiki API 없음 | [컨테이너 route dump] |
| 운영 컨테이너 import | `langgraph=True`, `langchain_core=True`, `langchain_mcp_adapters=True`, `langsmith=True`, `langfuse=True`, `langchain=False` | LangChain 전체 패키지 아님 | [컨테이너 import] |
| repo-local SKILL.md | 3개 | 스킬 파일은 일부 반영 | [파일 확인: `.claude/skills/*/SKILL.md`] |

## 3. 외부 최신 자료 요약

| 기술 | 최신 내용 | OHVIS 적용 의미 | 출처 |
|---|---|---|---|
| LangChain | `create_agent`는 model, tools, prompt, middleware를 묶는 configurable agent harness다. LangChain agents는 LangGraph 위에서 durable execution/HITL/persistence를 활용한다. | OHVIS는 LangChain을 전체 런타임이 아니라 tool/model/middleware 표준 계층으로 써야 한다. | LangChain Docs, 2026-09-07 접근: https://docs.langchain.com/oss/python/langchain/overview |
| LangGraph | long-running stateful agent를 위한 low-level orchestration runtime이며 durable execution, streaming, human-in-the-loop, persistence, memory를 핵심으로 둔다. | OHVIS task/loop/runner를 graph run으로 묶고 checkpoint/resume을 표준화해야 한다. | LangGraph Docs, 2026-09-07 접근: https://docs.langchain.com/oss/python/langgraph/overview |
| LangChain HITL | tool call 정책에 따라 실행 전 interrupt하고, approve/edit/reject/respond 결정을 받은 뒤 같은 thread에서 resume한다. production에서는 persistent checkpointer가 필요하다. | DB 변경, 파일쓰기, 배포, 금융주문, CAPTCHA/OTP 사용자 개입 정책을 같은 승인 모델로 통합할 수 있다. | LangChain HITL Docs, 2026-09-07 접근: https://docs.langchain.com/oss/python/langchain/human-in-the-loop |
| LangSmith | trace, production metrics, quality monitoring, dataset/eval, feedback queue, automations, Engine 기반 failure fix를 제공한다. | OHVIS의 Langfuse trace를 유지하면서 LangSmith-compatible trace/eval schema를 내부 DB에 먼저 만들어야 한다. | LangSmith Docs, 2026-09-07 접근: https://docs.langchain.com/langsmith/observability |
| OpenWiki | 코드베이스/개인지식을 Markdown wiki로 작성·유지하는 CLI다. 에이전트가 매번 repo를 재탐색하지 않도록 durable context를 제공한다. | OHVIS의 보고서/코드/장애/결정을 agent-readable wiki로 자동 컴파일하는 방향과 맞다. | OpenWiki Docs, 2026-09-07 접근: https://docs.langchain.com/oss/openwiki/overview |
| OpenWiki 자동 갱신 | CI 스케줄로 wiki를 갱신하고 PR/MR을 열 수 있으며, claims와 manifest를 함께 관리한다. | AADS 배포/러너 완료 시 wiki 갱신 PR을 자동 생성하는 구조가 적합하다. | OpenWiki Automate Updates, 2026-09-07 접근: https://docs.langchain.com/oss/openwiki/automate-updates |
| LLM-Wiki 연구 | 문서를 wiki page와 양방향 링크로 컴파일하고 search/read/link-following tool, Error Book을 제공한다. 기존 GraphRAG/LightRAG 대비 2.0~8.1 F1 향상을 보고했다. | `memory_facts`를 단순 저장소에서 link traversal 가능한 지식 운영체계로 승격해야 한다. | arXiv 2605.25480v2, 2026-05-26 |
| Vector RAG vs Wiki | small corpus 실험에서 wiki는 연결/인용 지원이 좋지만 query token 비용은 RAG보다 높을 수 있다. | OHVIS는 모든 질의를 wiki로 보내면 안 되고, single-fact는 RAG, cross-doc synthesis는 wiki traversal로 라우팅해야 한다. | arXiv 2605.18490v2, 2026-08-16 |
| WikiKV | 계층형 wiki storage를 path-indexed KV로 다루며 schema evolution, consistency protocol, budgeted navigation을 제안한다. | wiki table 설계 시 path, schema_version, source_manifest, partial update 일관성을 처음부터 넣어야 한다. | arXiv 2606.14275, 2026-06-12 |
| Hermes Agent | self-improving agent로 skill creation/improvement, persistent memory, session search, messaging gateway, cron, subagents, multiple terminal backends를 제공한다. | OHVIS는 Hermes 런타임을 그대로 도입하지 말고 closed learning loop와 skill lifecycle 패턴을 흡수해야 한다. | NousResearch hermes-agent, 2026-09-07 접근 |
| Hermes Skills | 스킬은 on-demand knowledge document이고 progressive disclosure로 token 비용을 줄인다. `/learn`으로 자료/워크플로우를 skill로 만들고 agent-managed skill도 가능하다. | OHVIS Skill Find는 `SKILL.md`와 references/scripts/assets를 DB 색인하고 필요한 skill만 로딩해야 한다. | Hermes Skills Docs, 2026-09-07 접근 |
| OpenAI Skills/Plugins | Skill은 reusable workflow이며 instructions, examples, code/supporting resources를 포함할 수 있다. Plugins는 skills/apps/app templates를 묶고, Codex task view에서는 Sources -> Use plugins로 설치 plugin을 선택한다. | AADS/OHVIS도 skill registry, 권한, provenance, workspace install 정책을 분리해야 한다. | OpenAI Help: Skills in ChatGPT, Plugins in ChatGPT and Codex, 2026-09-07 접근 |

## 4. 기술별 상세 설명과 OHVIS 반영 상태

### 4.1 Harness

하네스는 모델 자체가 아니라 모델 주변의 실행 장치다. LangChain 관점에서는 prompt, tools, middleware가 모델 루프를 감싸고, LangGraph 관점에서는 state, edge, checkpoint, interrupt, persistence가 장기 실행을 감싼다.

| 하위 요소 | 의미 | OHVIS 현재 상태 | 판정 |
|---|---|---|---|
| Model routing | OpenAI/Anthropic/Gemini/LiteLLM 선택 | `app/llm`, LiteLLM runner, fallback 규칙 존재 | 부분 반영 |
| Prompt | L1~L5 역할/프로젝트/intent 규칙 | `prompt_assets` 141건, enabled 140건 | 반영 |
| Tools | DB, Git, Docker, SSH, Browser, PC Agent, Runner | 내부 tool registry와 MCP adapter 존재 | 반영 |
| Middleware | 승인, 비용, 보안, retry, HITL | 정책은 산발적, LangChain middleware형 표준은 없음 | 미흡 |
| State | 작업 상태, 비용, QA, DevOps, 산출물 | `AADSState`, `ohvis_tasks`, `ohvis_loops` 존재 | 반영 |
| Checkpoint | 중단 후 같은 상태로 재개 | 의존성과 인자는 있으나 운영 task/runner와 완전 통합 전 | 부분 반영 |
| Trace/Eval | 실행 기록, 실패 분석, 품질 평가 | Langfuse는 있음, trace/eval 테이블 없음 | 부분 반영 |
| Knowledge | 장기기억, 문서, 링크, 오류북 | memory/knowledge graph는 있음, wiki 전용 API 없음 | 부분 반영 |

개선 판단: `OHVISHarness`라는 단일 커널을 만들어 모든 실행이 `graph_run_id`, `task_id`, `session_id`, `tool_policy_id`, `trace_id`, `wiki_context_id`, `skill_run_id`를 갖게 해야 한다.

### 4.2 LangGraph

LangGraph는 OHVIS의 핵심 실행 런타임으로 가장 적합하다. OHVIS 작업은 단순 질의응답이 아니라 승인, 재개, 배포, 장기 모니터링, 다중 에이전트 협업을 포함하기 때문이다.

현재 반영:

- `pyproject.toml`에 `langgraph>=1.1.6`, `langgraph-checkpoint`, `langgraph-checkpoint-postgres`가 있다. [코드 확인]
- `app/graph/builder.py`가 Native `StateGraph`를 사용한다. [코드 확인]
- PM, Supervisor, Architect, Developer, QA, Judge, DevOps, Researcher 노드가 구성되어 있다. [코드 확인]
- `compile_graph(checkpointer=None)`가 checkpointer를 받을 수 있다. [코드 확인]
- `app/agents/pm.py`에서 `langgraph.types.interrupt`를 사용한다. [코드 확인]

미반영/미흡:

- `ohvis_tasks`, `ohvis_loops`, `pipeline_jobs`가 하나의 durable graph run id로 연결되어 있지 않다.
- `ohvis_tasks.running` 21건이 남아 있어 runner/process와 DB 상태가 불일치할 가능성이 있다. [DB 조회]
- LangGraph checkpoint가 chat/runner/loop 전체의 운영 재개 계약으로 고정되어 있지 않다.
- 승인 흐름이 LangGraph interrupt/HITL 표준으로 모든 고위험 tool에 일괄 적용되지는 않는다.

반영 시 좋은 점:

- 응답 중단, 서버 재시작, 모델 fallback 이후에도 같은 작업을 이어가기 쉬워진다.
- CEO 승인 대기, CAPTCHA/OTP 사용자 입력, 배포 승인, 금융주문 승인 같은 고위험 분기를 같은 state machine으로 관리할 수 있다.
- 장중 GO100/KIS, 수집 자동화, 배포 후 5분 모니터링을 같은 graph template로 표준화할 수 있다.

### 4.3 LangChain

LangChain은 OHVIS에서 전체 장기 실행 엔진이라기보다 모델/도구/스킬/middleware 하네스 계층으로 쓰는 것이 맞다.

현재 반영:

- `langchain-anthropic`, `langchain-openai`, `langchain-google-genai`, `langchain-mcp-adapters==0.2.0` 의존성이 있다. [코드 확인]
- 운영 컨테이너에서 `langchain_core`와 `langchain_mcp_adapters`는 import 가능하다. [컨테이너 import]
- `scripts/litellm_runner.py`는 `ChatOpenAI(base_url=LiteLLM)`와 `MultiServerMCPClient`, `create_react_agent`를 사용한다. [코드 확인]
- agent 파일들이 `langchain_core.messages`를 사용한다. [코드 확인]

정정:

- 운영 컨테이너에서 `langchain` 메타 패키지는 import 불가다. 따라서 "LangChain 전체 패키지가 반영됨"이 아니라 "LangChain Core/Provider/MCP adapter가 반영됨"으로 표현해야 한다. [컨테이너 import]

미반영/미흡:

- `create_agent` 기반 표준 agent factory가 제품 런타임에 없다.
- HumanInTheLoopMiddleware 같은 tool call policy가 AADS tool registry에 일괄 연결되어 있지 않다.
- 프로젝트별 runbook/skill을 LangChain tool/middleware로 자동 주입하지 않는다.

반영 시 좋은 점:

- active_project별 tool allowlist, 승인 정책, 모델 라우팅, 비용 제한을 agent 생성 시 자동 주입할 수 있다.
- 매장비서/마케팅/GO100/KIS/SF/NTV2/NAS별 반복 업무를 skill pack으로 유지할 수 있다.
- read-only/side-effecting/destructive-blocked tool을 코드 계약으로 분리할 수 있다.

### 4.4 LangSmith

LangSmith는 관측/평가/개선 루프 플랫폼이다. OHVIS에는 패키지 import 가능성과 Langfuse 대체 관측은 있지만, LangSmith 제품 기능이 운영으로 닫혀 있지는 않다.

현재 반영:

- 운영 컨테이너에서 `langsmith=True`다. [컨테이너 import]
- `app/core/langfuse_config.py`가 Langfuse trace 생성, LiteLLM callback 설정, graceful degradation을 구현한다. [코드 확인]
- `tests/test_e2e_code_modify.py`, `tests/test_e2e_agent_sdk.py`, `tests/test_e2e_deep_research.py`에 Langfuse 관련 테스트 흔적이 있다. [코드 확인]

미반영/미흡:

- `trace_*`, `eval_*` 테이블이 없다. [DB 조회]
- LangSmith API key/tracing으로 실제 trace 전송하는 운영 계약은 확인되지 않았다.
- trace -> dataset -> eval -> 개선 PR 또는 Runner 지시서로 이어지는 자동 루프가 없다.
- CEO 화면에서 trace, tool call, latency, cost, error, correction을 한 흐름으로 보는 UI가 없다.

권장:

- 외부 LangSmith 전송은 기본 off로 둔다.
- 내부 DB에 LangSmith-compatible trace/eval schema를 만든다.
- 현재 Langfuse를 유지하면서 export adapter를 붙인다.
- 반복 실패 trace는 `error_pattern`과 연결해 자동 지시서 후보를 만든다.

### 4.5 LLM Wiki / OpenWiki

LLM Wiki는 기존 RAG의 "flat chunk lookup" 한계를 줄이기 위해 문서를 wiki page, 양방향 링크, source archive, Error Book, link-following tool로 컴파일하는 방향이다. 2026년 최신 흐름에서는 LangChain `OpenWiki`가 코드베이스/개인지식용 실용 CLI로 등장했고, 학술 LLM-Wiki/WikiKV는 agent-native retrieval과 storage consistency를 제안한다.

현재 반영:

- `memory_facts` 71,067건이 장기 기억 기반으로 존재한다. [DB 조회]
- `project_memory`, `experience_memory`, `system_memory`, `procedural_memory`를 포함한 5-layer memory store가 있다. [코드 확인]
- `app/core/knowledge_graph.py`가 `memory_facts`에서 entity/relation을 추출해 `kg_entities`, `kg_relations`로 저장한다. [코드 확인]
- 시스템 프롬프트에는 confidence 강화, error pattern, Sleep-Time 정제 개념이 있다. [프롬프트 컨텍스트/DB 확인]

미반영/미흡:

- `wiki_*` 테이블이 없다. [DB 조회]
- `/api/v1/wiki` 또는 wiki search/read/follow API가 없다. [route dump]
- 보고서, 코드, DB 쿼리 결과가 source archive와 linked page로 자동 컴파일되지 않는다.
- OpenWiki처럼 `AGENTS.md`/`CLAUDE.md`에서 wiki pointer를 연결하고 CI로 PR을 여는 구조가 없다.
- 단일 fact lookup과 cross-doc synthesis를 cost-aware로 라우팅하지 않는다.

반영 시 좋은 점:

- CEO가 "이전 결정/장애/계약/배포/전략"을 물을 때 매번 재조사하지 않고 출처 연결된 wiki를 탐색할 수 있다.
- 보고서 저장이 곧 지식 축적으로 이어진다.
- 장기적으로 프롬프트에 모든 지식을 넣는 비용을 줄이고, 필요한 wiki page만 읽는 구조가 된다.
- 반복 오류는 `wiki_error_book`으로 승격해 같은 실패를 줄일 수 있다.

### 4.6 Hermes

Hermes는 세 가지로 구분해야 한다.

| 구분 | 설명 | OHVIS 적용 판단 |
|---|---|---|
| Hermes Agent | Nous Research의 self-improving autonomous agent runtime. skill creation/improvement, memory, session search, messaging gateway, cron, subagents, MCP, multiple terminal backends를 제공한다. | 패턴 흡수 가치 높음. 운영 런타임 직접 교체는 비권장. |
| Hermes Skills | on-demand knowledge documents. progressive disclosure, `/learn`, agent-managed skills, external/project skill directories를 제공한다. | OHVIS Skill Find 설계에 직접 반영할 가치 높음. |
| Hermes 4 모델 | Nous Research의 open-weight hybrid reasoning model family. | 운영 핵심 모델로 즉시 전환하지 말고 벤치 후 fallback 후보로만 검토. |

현재 OHVIS 반영:

- `memory_facts`, `pipeline_runner`, `spawn_subagent`, `schedule_task`, PC Agent, Android OHVIS, Telegram/push 계열이 있어 구성요소는 산발적으로 있다.
- 그러나 Hermes처럼 "작업 경험 -> skill 후보 생성 -> skill 개선 -> 검증 -> 배포/공유 -> 다음 실행 자동 적용"이 닫힌 루프는 아니다.
- `hermes_*` 테이블과 Hermes Agent 런타임 코드는 확인되지 않았다. [DB 조회][코드 검색]
- dashboard/node 의존성의 `hermes-parser`류는 React Native/JS parser 계열로 agent harness와 무관하다.

반영 시 좋은 점:

- 반복 성공 업무를 스킬화해 CEO가 매번 절차를 설명하지 않아도 된다.
- 실패한 스킬은 실행 trace와 함께 자동 개선 후보가 된다.
- 모바일, Telegram, dashboard, PC Agent, cron을 같은 task event로 묶어 "완료/중단/승인요청" 알림을 일관화할 수 있다.
- 프로젝트별 Bot mode를 만들 수 있다: AADS Ops Bot, GO100 Market Ops Bot, KIS Risk Bot, NTV2 Merchant Bot, SF Media Ops Bot, NAS Storage Bot.

주의:

- 외부 Hermes Agent에 AADS 운영 권한, 시크릿, 배포 제어를 넘기면 안 된다.
- skill 자동 생성은 유용하지만, 보안/금융/배포/인증 사이트 자동화에는 typed policy와 CEO 승인 로그가 먼저 필요하다.
- 2026년 skill 보안 연구는 "Auto-skill보다 Auto-policy가 더 중요하다"는 문제를 제기한다. OHVIS는 스킬 문서와 실행 권한을 반드시 분리해야 한다.

### 4.7 Skill Find

Skill Find는 "필요할 때 필요한 스킬만 찾아 읽고 즉시 적용하는 계층"으로 정의하는 것이 맞다. OpenAI/Codex와 Hermes 모두 공통적으로 progressive disclosure를 사용한다.

현재 반영:

- repo-local `.claude/skills/handoff/SKILL.md`, `.claude/skills/tpp/SKILL.md`, `.claude/skills/sales-channel-collector/SKILL.md` 3개가 있다. [파일 확인]
- `sales-channel-collector`는 CAPTCHA/OTP 우회 금지, 승인 범위 자동화, 같은 work_key 재개 원칙을 포함한다. [파일 확인]
- OpenAI/Codex 환경에는 `tool_search`가 있어 deferred tool metadata를 BM25로 검색해 필요한 도구를 다음 모델 호출에 노출하는 lazy loading 구조가 있다. [현재 도구 설명]

미반영/미흡:

- `skill_*` 테이블이 없다. [DB 조회]
- `/api/v1/skills/search` route가 없다. [route dump]
- skill slug/version/hash/source/provenance가 `chat_turn_executions`, `pipeline_jobs`, `ohvis_tasks`에 남는 구조가 확인되지 않았다.
- 스킬별 allowed tools, risk tier, approval policy, 검증 명령이 DB에서 강제되지 않는다.
- 스킬 설치/신뢰/공유/버전/비활성화 UI가 없다.

반영 시 좋은 점:

- 시스템 프롬프트를 계속 비대하게 만들지 않아도 된다.
- "GO100 장 시작 확인", "매장비서 배민 수집", "AADS blue-green 배포", "NTV2 입점계약서 생성" 같은 요청에서 top-k skill을 즉시 찾아 실행할 수 있다.
- project_scope와 active_project를 skill metadata로 강제해 프로젝트 오인을 줄일 수 있다.
- 스킬 provenance가 남으면 완료보고 품질과 사후감사가 좋아진다.

## 5. 최종 반영 판정표

| 축 | 현재 반영도 | 근거 | 부족한 핵심 | 우선순위 |
|---|---|---|---|---|
| Harness | 중간 | AADSState, prompt_assets, tool/MCP, task/loop, runner | 단일 `OHVISHarness` 계약 없음 | P0 |
| LangGraph | 높음 | StateGraph, 8-agent graph, checkpointer 인자, interrupt 사용 | chat/task/loop/runner durable run 통합 미흡 | P0 |
| LangChain Core/Provider/MCP | 중간 | `langchain_core`, providers, MCP adapter import 가능 | middleware/tool policy 표준화 미흡 | P1 |
| LangChain meta package | 낮음 | 운영 컨테이너 `langchain=False` | `create_agent` 최신 표준 직접 사용 불가 | P2 |
| LangSmith | 낮음 | import 가능 | trace/eval/dataset/Engine workflow 없음 | P1 |
| Langfuse | 중간 | config와 callback 구현 | trace를 eval/개선 지시서로 승격하지 않음 | P1 |
| LLM Wiki/OpenWiki | 중간 이하 | memory_facts 71,067건, knowledge_graph | wiki tables/API/page/link/source/error book 없음 | P1 |
| Hermes Agent Pattern | 낮음 | memory/runner/schedule/subagent 일부 산재 | closed learning loop 없음 | P1 |
| Skill Find | 낮음 | SKILL.md 3개 | registry/search/provenance/risk gate 없음 | P0 |

## 6. 목표 아키텍처

```text
CEO / Chat / Dashboard / Mobile / PC Agent
        |
        v
OHVIS Harness Kernel
        |
        +-- Execution Layer: LangGraph
        |      - graph_run_id
        |      - checkpoint
        |      - interrupt/resume
        |      - task/loop/runner state sync
        |
        +-- Tool & Skill Layer: LangChain Core + MCP + Skill Find
        |      - model/provider abstraction
        |      - tool registry adapter
        |      - risk tier / allowed tools
        |      - project skill packs
        |      - skill provenance
        |
        +-- Observability Layer: Langfuse current + LangSmith-compatible schema
        |      - trace/span/tool-call/cost/error
        |      - eval dataset
        |      - failure-to-directive
        |
        +-- Knowledge Layer: LLM Wiki / OpenWiki
        |      - source archive
        |      - wiki pages
        |      - bidirectional links
        |      - claims / confidence / freshness
        |      - error book
        |
        +-- Self-Improvement Layer: Hermes Pattern
               - auto skill draft
               - skill improvement proposal
               - bot mode profile
               - scheduled automation
               - messaging gateway
```

## 7. 개선안

### P0. OHVIS Harness Kernel

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| `OHVISHarness` 서비스 | chat/task/loop/runner/tool/trace/wiki/skill을 하나의 실행 계약으로 묶는다. | 단위테스트에서 task 생성, tool policy 적용, trace 기록, result 저장 통과 |
| `graph_run_id` 도입 | `ohvis_tasks`, `ohvis_loops`, `pipeline_jobs`, `chat_turn_executions`를 연결한다. | terminal runner와 running task 불일치 0건 |
| HITL 표준화 | approve/edit/reject/respond를 tool risk tier에 연결한다. | 파일쓰기/DB변경/배포/금융/인증 tool 승인 테스트 통과 |

### P0. Skill Find Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| skill registry schema | `ops_skill_library`, `ops_skill_versions`, `ops_skill_runs` 추가 | migration + seed + SELECT 검증 통과 |
| repository skill indexer | `.claude/skills`, `.codex/skills`, docs runbook을 스캔 | 현재 3개 SKILL.md가 DB에 색인됨 |
| skill search API | project/role/intent/query 기반 top-k skill 반환 | "매장비서 배민 수집"에서 `sales-channel-collector` top-1 |
| skill provenance | 응답/러너/작업에 skill slug/version/hash 저장 | 완료보고에 skill provenance 자동 표시 |
| risk gate | skill별 allowed tools, forbidden tools, approval policy 강제 | deploy/financial/auth/security skill은 승인 전 실행 차단 |

### P1. LLM Wiki / OpenWiki Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| wiki schema | `wiki_sources`, `wiki_pages`, `wiki_links`, `wiki_claims`, `wiki_error_book` 추가 | migration + read/search API 통과 |
| report compiler | 보고서/웹자료/코드검수/DB 조회를 wiki page로 컴파일 | 신규 보고서 저장 시 source/page/link 자동 생성 |
| OpenWiki-style repo docs | repo별 `openwiki/` 또는 `docs/wiki/` 생성, AGENTS pointer 추가 | AADS repo wiki 1차 생성 + docs lint 통과 |
| router | single-fact는 memory/RAG, cross-doc은 wiki traversal로 분기 | query type별 token/cost 로그 생성 |
| Error Book | 틀린 사실, 오래된 사실, 반복 오류를 wiki_error_book에 기록 | 동일 오류 재발 시 prompt constraint 주입 |

### P1. Observability / LangSmith-Compatible Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| trace schema | Langfuse trace와 LangSmith-compatible fields를 내부 DB에 저장 | trace_id, run_id, tool_calls, latency, cost, error 저장 |
| eval dataset | 실패/저품질 응답과 성공사례를 eval case로 승격 | 최근 실패 패턴 상위 10개 eval 생성 |
| failure-to-directive | 반복 실패 trace를 Runner 지시서 초안으로 변환 | error_pattern -> directive draft 자동 생성 |
| external LangSmith opt-in | 외부 LangSmith 전송은 환경변수로만 활성화 | 기본 off, masking 테스트 통과 |

### P1. Hermes Pattern Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| auto skill draft | 동일 유형 작업 3회 이상 성공 시 skill 후보 생성 | draft 생성 + CEO 승인 전 비활성 |
| skill improvement loop | 실패 원인과 성공 수정안을 skill patch 후보로 저장 | 실패 후 재실행 성공률 추적 |
| bot mode | 프로젝트별 Ops/QA/Research/Deploy profile 구성 | `/ops/bots`에서 역할/권한 확인 |
| messaging gateway | 모바일 push, Telegram, dashboard notification을 task event로 통합 | terminal event payload가 모든 채널에서 동일 |
| scheduled automation | 자연어 일정이 OHVIS loop/task로 생성 | 장 시작/배포후모니터링/일일보고 자동 생성 |

### P2. UI

| 화면 | 추가 기능 | 완료 기준 |
|---|---|---|
| `/ops/harness` | active graph run, stale task, checkpointer, tool policy 상태 | active_project별 상태 카드 표시 |
| `/ops/skills` | skill search, installed/shared/project skill, risk gate, provenance | skill 검색/열람/활성화/비활성화 가능 |
| `/ops/memory` | wiki page, source, links, claims, error book | search/read/follow UI 동작 |
| `/ops/traces` | trace, tool call, eval case, failure-to-directive | 실패 trace에서 지시서 초안 생성 |
| `/chat` | 현재 응답의 graph_run_id, used skills, wiki sources, trace summary | 답변 근거와 스킬 적용 이력 확인 |

## 8. 프로젝트별 활용안

| 프로젝트 | 적용 방식 | 우선 스킬 |
|---|---|---|
| AADS/OHVIS | 배포, 프롬프트, 러너, 문서, 장애 대응을 Harness Kernel로 통합 | `aads-bluegreen-release`, `prompt-provenance-audit`, `runner-recovery`, `docs-publish-check` |
| 매장비서 | 로그인 사이트 수집, CAPTCHA/OTP HITL, 매장별 site_profile 관리 | `sales-channel-collector`, `authenticated-site-collector` |
| 마케팅 | 광고/리뷰/검색콘솔/소셜 데이터를 site recipe로 수집 | `marketing-channel-import`, `campaign-report-compiler` |
| GO100 | 장 시작 점검, 전략 무진입 원인, 금융 승인 정책 | `go100-market-open-check`, `go100-entry-zero-audit`, `go100-order-risk-gate` |
| KIS | 계좌/주문/브로커 상태, 실매매 리스크 stop | `kis-broker-health`, `kis-order-ledger-audit`, `kis-risk-stop` |
| SF | 영상 생성 큐, 크롤링, 실패 trace, 산출물 wiki | `sf-video-pipeline-health`, `sf-source-collector` |
| NTV2 | 입점/계약/AI Studio/상품 운영 runbook | `ntv2-merchant-contract`, `ntv2-ai-studio-qa`, `ntv2-product-onboarding` |
| NAS | 이미지 처리, storage, 내부망/rsync/큐 상태 | `nas-image-job-health`, `nas-storage-capacity-audit` |

## 9. 권장 지시서 초안

```text
>>>DIRECTIVE_START
TASK_ID: AADS-OHVIS-HARNESS-SKILL-WIKI-HERMES-001
TITLE: OHVIS Harness Kernel, Skill Find, LLM Wiki, Hermes Pattern 통합 기반 구현
PRIORITY: P1-HIGH
SIZE: XL
MODEL: gpt-5.6-sol
DESCRIPTION:
1. 현재 AADS/OHVIS의 LangGraph, LangChain Core/MCP adapter, Langfuse, memory_facts, .claude/skills 구조를 보존한다.
2. `app/services/ohvis_harness.py`를 추가해 chat/task/loop/runner/tool/trace/wiki/skill을 `graph_run_id` 기준으로 연결한다.
3. Skill Find Layer를 구현한다: `ops_skill_library`, `ops_skill_versions`, `ops_skill_runs` 테이블, `.claude/skills` indexer, `/api/v1/skills/search`, skill provenance 저장.
4. LangGraph checkpoint/interrupt/resume을 report/audit/code/deploy/browser-collection/financial-risk 6개 intent template에 적용한다.
5. LangChain Core/MCP adapter 기반 tool middleware를 구현해 tool risk tier와 HITL decision type(approve/edit/reject/respond)을 표준화한다.
6. 현행 Langfuse trace를 유지하면서 LangSmith-compatible trace/eval schema를 내부 DB에 저장한다. 외부 LangSmith 전송은 환경변수 opt-in이며 기본 off로 둔다.
7. LLM Wiki/OpenWiki Layer를 구현한다: `wiki_sources`, `wiki_pages`, `wiki_links`, `wiki_claims`, `wiki_error_book`, search/read/follow tool, source archive, report compiler.
8. Hermes Agent의 패턴만 흡수한다: auto skill draft, skill self-improvement, bot mode profile, messaging gateway abstraction, scheduled automation. 외부 Hermes Agent 런타임 도입은 하지 않는다.
9. `/ops/harness`, `/ops/skills`, `/ops/memory`, `/ops/traces` CEO 화면 초안을 구현한다.
10. 기존 unrelated dirty 파일은 건드리지 말고 isolated worktree 또는 Runner 의존성 그래프로 진행한다.
11. 검증: DB migration 전후 SELECT, unit tests, route import, API smoke, dashboard screenshot 또는 API 폴백, blue-green 배포 전 clean SHA 확인, 배포 시 5분 P0/P1 모니터링.
>>>DIRECTIVE_END
```

## 10. 리스크와 통제

| 리스크 | 통제 |
|---|---|
| LangSmith 외부 SaaS 비용/데이터 반출 | 기본 off, 내부 DB/Langfuse 우선, 민감값 masking |
| LangGraph 전환 중 기존 runner 불안정 | 기존 runner 유지, intent별 template 점진 적용 |
| Wiki query token 비용 증가 | single-fact/RAG와 cross-doc/wiki traversal 라우터 분리 |
| Skill 자동 생성 오남용 | draft는 비활성 저장, CEO 승인 후 활성화 |
| Skill 권한 상승 | skill metadata에 allowed tools/risk tier/approval policy를 machine-checkable로 저장 |
| Hermes 런타임 권한 충돌 | 외부 런타임 직접 도입 금지, 패턴만 내부 구현 |
| 기존 dirty 충돌 | Runner worktree 격리, 문서/코드/배포 커밋 분리 |

## 11. 최종 판정

OHVIS는 이미 LangGraph/LangChain Core 기반 초석과 대규모 메모리 원장을 갖고 있다. 그러나 현재 구조는 "에이전트 실행", "도구 권한", "관측", "지식", "스킬"이 각각 따로 존재한다. 다음 구현의 핵심은 새 프레임워크를 사오는 것이 아니라, `OHVISHarness + Skill Find + LLM Wiki`를 내부 표준 계약으로 만드는 것이다.

첫 구현 순서는 P0 `OHVISHarness`와 `Skill Find Layer`다. 이 두 축이 들어가야 LangGraph durable execution, LangChain HITL middleware, LangSmith-compatible eval, OpenWiki/LLM Wiki, Hermes형 자가개선 루프가 안전하게 확장된다.

## 12. 검증 내역

| 검증 | 결과 |
|---|---|
| KST 기준 시각 | `2026-09-07 10:05:45 KST` shell, `2026-09-07 10:07:05 KST` DB |
| AGENTS 규칙 확인 | `/root/aads/AGENTS.md`, `AGENTS.md` 확인 |
| Git 상태 | 기존 unrelated dirty 다수 확인. 이 보고서는 신규 파일로만 추가 |
| DB 측정 | `ohvis_tasks`, `ohvis_loops`, `ohvis_loop_iterations`, `memory_facts`, `prompt_assets`, 전용 테이블 존재 여부 조회 |
| 코드 확인 | `pyproject.toml`, `app/graph/builder.py`, `app/graph/state.py`, `app/core/langfuse_config.py`, `app/memory/store.py`, `app/core/knowledge_graph.py`, `scripts/litellm_runner.py`, `app/mcp/client.py` |
| 컨테이너 확인 | import 가능 모듈과 route count 확인 |
| 외부 자료 | LangChain/LangGraph/LangSmith/OpenWiki/OpenAI Skills/Plugins/Hermes/LLM-Wiki 최신 자료 확인 |
| 테스트 | 문서 작성 작업이므로 코드 테스트는 미실행 |
| 배포 | 문서 신규 작성만 수행. 커밋/푸시/배포는 미수행 |

## 13. 2026-09-07 10:18 KST 최신 보정판

### 13.1 최종 결론

OHVIS에는 하네스의 핵심 부품이 이미 반영되어 있다. `LangGraph` 실행 그래프, `LangChain Core/Provider/MCP adapter`, `Langfuse`, `langsmith` 패키지, `memory_facts`/지식그래프, `.claude/skills` 3개가 실측으로 확인됐다.

그러나 최신 에이전트 제품 구조 관점에서는 아직 "통합 하네스"가 아니다. `wiki_*`, `skill_*`, `hermes_*`, `trace_*`, `eval_*` 전용 테이블은 0건이고, 운영 컨테이너 API 라우트도 `skill=0`, `wiki=0`이다. 따라서 다음 구현 목표는 새 도구를 단순 설치하는 것이 아니라 `OHVISHarness Kernel`을 중심으로 실행, 지식, 관측, 스킬, 정책을 하나의 run provenance로 묶는 것이다.

### 13.2 최신 실측 근거

| 항목 | 최신 실측값 | 판정 | 출처 |
|---|---:|---|---|
| KST 기준 시각 | 2026-09-07 10:16:41 | 최신 보정 | DB `now() at time zone 'Asia/Seoul'` |
| 운영 컨테이너 | `aads-server`, `aads-server-green`, `aads-dashboard`, `aads-dashboard-green` 모두 healthy | 정상 | `docker ps` |
| `ohvis_tasks` | done 241 / running 21 / error 1 / stale_cleaned 3 | task 원장 반영, running 감사 필요 | DB 조회 |
| `ohvis_loops` | active 4 / completed 7 / paused 2 / cancelled 5 | loop 원장 반영 | DB 조회 |
| `memory_facts` | 71,067건 | LLM Wiki 후보 기반 충분 | DB 조회 |
| 프로젝트별 memory 상위 | GO100 34,648 / AADS 17,705 / NTV2 7,746 / CEO 3,368 / KIS 2,980 / FOOD 1,593 | 프로젝트별 지식 편중 있음 | DB 조회 |
| `prompt_assets` | enabled 140 / total 141 | 프롬프트 계층 반영 | DB 조회 |
| Ops 유사 prompt asset | 9건 | Ops 프롬프트 일부 반영 | DB 조회 |
| Skill 유사 prompt asset | 0건 | Skill 전용 프롬프트 미반영 | DB 조회 |
| `wiki_*`, `skill_*`, `hermes_*`, `trace_*`, `eval_*` 테이블 | 0건 | 전용 제품 테이블 미반영 | DB `information_schema.tables` |
| `kg_entities` / `kg_relations` | 141건 / 624건 | 지식그래프 기반 존재 | DB 조회 |
| `ai_observations` | 801건 | 관찰 원장 존재 | DB 조회 |
| repo-local SKILL.md | 3개 | 스킬 파일은 일부 반영 | `.claude/skills/*/SKILL.md` |
| 운영 컨테이너 import | `langgraph=True`, `langchain_core=True`, providers=True, `langchain_mcp_adapters=True`, `langsmith=True`, `langfuse=True`, `langchain=False` | LangChain 전체가 아니라 Core/Provider 중심 | 컨테이너 import |
| 운영 API route | ops 64 / ohvis 8 / loops 8 / skill 0 / wiki 0 / total 644 | Ops/Loop는 반영, Skill/Wiki API 없음 | 컨테이너 route dump |

### 13.3 공식 자료 기준 정리

| 축 | 공식/1차 자료 기준 | OHVIS 적용 판단 |
|---|---|---|
| Harness | LangChain은 agent를 `Model + Harness`로 보고, prompt/tools/middleware를 모델 루프 주변 제어 계층으로 둔다. | OHVIS의 prompt_assets, tool registry, MCP, task ledger를 `OHVISHarness`로 묶어야 한다. |
| LangGraph | long-running stateful agent용 low-level orchestration runtime이며 durable execution, streaming, HITL, persistence가 핵심이다. | OHVIS의 task/loop/runner/chat을 graph_run_id와 checkpoint/resume으로 통합할 가치가 가장 높다. |
| LangChain | `create_agent`와 middleware가 표준이며, LangChain agents는 LangGraph 위에서 durable execution/HITL/persistence를 활용한다. | OHVIS는 LangChain을 장기 실행 엔진보다 tool/model/middleware 하네스로 써야 한다. |
| LangSmith | trace, production metrics, quality monitoring, dataset/eval, automations, failure diagnosis를 제공한다. | 외부 SaaS 직결보다 내부 trace/eval schema를 먼저 만들고 Langfuse와 병행하는 것이 안전하다. |
| LLM Wiki/OpenWiki | OpenWiki는 agent-readable Markdown wiki, source evidence, claims, 자동 갱신, 시각화를 제공한다. LLM-Wiki 연구는 search/read/link-following/Error Book 구조를 제안한다. | `memory_facts`를 wiki page/link/source/error book으로 승격해야 한다. |
| Hermes | closed learning loop, memory, skills, scheduled automation, messaging gateway, subagents, project skills, write-approval gate가 핵심이다. | Hermes 런타임 교체가 아니라 self-improving skill lifecycle과 approval gate 패턴을 흡수해야 한다. |
| Skill Find | OpenAI/Codex는 skill metadata로 발견하고 필요 시 `SKILL.md`/references/scripts를 읽는 progressive disclosure를 사용한다. | OHVIS에도 skill registry/search/version/provenance/risk policy가 필요하다. |

참고 공식/1차 자료:

- OpenAI Docs, Build skills: https://developers.openai.com/codex/build-skills
- LangChain overview: https://docs.langchain.com/oss/python/langchain/overview
- LangChain prebuilt middleware: https://docs.langchain.com/oss/python/langchain/middleware/built-in
- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangSmith observability: https://docs.langchain.com/langsmith/observability
- OpenWiki GitHub: https://github.com/langchain-ai/openwiki
- Hermes Agent docs: https://hermes-agent.nousresearch.com/docs/
- Hermes Skills docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
- LLM-Wiki paper: https://arxiv.org/abs/2605.25480
- Auto-Policy, not Auto-Skill: https://arxiv.org/abs/2608.25091
- GitSkills dataset: https://arxiv.org/abs/2608.10906

### 13.4 반영됨 / 미반영 / 개선 가치

| 축 | 반영됨 | 미반영 | 반영 시 좋은 점 | 우선순위 |
|---|---|---|---|---|
| 하네스 | prompt, tool, MCP, task/loop, model routing 일부 | 단일 `OHVISHarness` 계약, run provenance, tool policy kernel | 모든 응답/러너/배포/수집이 같은 기준으로 감사 가능 | P0 |
| LangGraph | StateGraph, 8-agent graph, subgraph, interrupt, checkpointer 인자 | task/loop/runner/chat 전체 durable graph run 연결 | 중단 복구, 승인 재개, 배포 롤백, 장기 모니터링 안정화 | P0 |
| LangChain | Core messages, provider packages, MCP adapter, 일부 ReAct runner | `create_agent` 표준 factory, middleware 기반 risk gate | 모델/도구/스킬/승인 정책을 프로젝트별로 재사용 | P1 |
| LangSmith | 패키지 import 가능 | trace/eval/dataset/annotation/Engine workflow | 실패 원인, 비용, 품질을 재현 가능한 eval로 전환 | P1 |
| Langfuse | 설정과 callback 구현 | eval case와 자동 개선 지시서 연결 | 기존 관측 투자 보존, 외부 SaaS 의존 최소화 | P1 |
| LLM Wiki | memory_facts, kg_entities, kg_relations, 관찰 원장 | wiki API/table/source/archive/error book/lint | CEO 질문 재조사 비용 절감, 출처 연결된 장기 기억 | P1 |
| Hermes | memory/runner/subagent/schedule/Android/push 일부 유사 | skill self-improvement lifecycle, bot mode, skill approval queue | 반복 성공 업무를 스킬화하고 실패를 자동 개선 후보로 전환 | P1 |
| Skill Find | `.claude/skills` 3개 | DB registry, `/api/v1/skills/search`, version/hash/provenance/risk tier | 필요한 스킬만 즉시 로딩, 프롬프트 비대화와 프로젝트 오인 감소 | P0 |

### 13.5 구현 기획

#### P0. OHVIS Harness Kernel

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| `app/services/ohvis_harness.py` | `graph_run_id`, `session_id`, `task_id`, `project`, `role`, `intent`, `tool_policy_id`, `trace_id`, `skill_run_ids`, `wiki_context_ids`를 한 계약으로 정의 | chat 요청 1건이 harness run 1건으로 기록 |
| task/loop/runner 연결 | `ohvis_tasks`, `ohvis_loops`, `pipeline_jobs`, `chat_turn_executions`를 run id로 연결 | terminal runner와 running task 불일치 0건 |
| 정책 게이트 | read/write/deploy/financial/auth/secret/destructive tier를 강제 | 위험 tool 승인 없는 실행 0건 |

#### P0. Skill Find Layer

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| skill registry | `.claude/skills`, `.agents/skills`, 시스템/플러그인 skill metadata 색인 | skill slug/version/hash/source/risk tier 조회 가능 |
| search API | `/api/v1/skills/search`, `/api/v1/skills/{slug}`, `/api/v1/skills/{slug}/preview` | 요청 intent 기준 top-k skill 반환 |
| provenance | `ohvis_tasks`/`chat_turn_executions`에 used skill 기록 | 답변마다 적용 skill 확인 가능 |
| approval | skill 생성/수정은 draft 저장 후 승인 | self-improving skill이 무단 활성화되지 않음 |

#### P1. LangGraph Durable Runtime

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| intent graph template | report/audit/code/deploy/browser-collection/financial-risk template | 6개 template 단위 테스트 |
| checkpoint | Postgres checkpointer를 운영 resume 계약으로 고정 | 서버 재시작 후 같은 thread_id 재개 smoke 통과 |
| HITL | approve/edit/reject/respond를 공통 interrupt payload로 표준화 | 배포/DB/금융/OTP/CAPTCHA 정책 테스트 |

#### P1. LLM Wiki Knowledge Harness

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| wiki schema | `wiki_sources`, `wiki_pages`, `wiki_links`, `wiki_claims`, `wiki_error_book` | 마이그레이션 전후 SELECT 검증 |
| compiler | 보고서, HANDOVER, 코드 분석, DB 조회 결과를 linked Markdown page로 컴파일 | 신규 보고서 저장 시 wiki page 자동 생성 |
| tools | `wiki_search`, `wiki_read`, `wiki_follow`, `wiki_sufficiency_check` | 다중 문서 질문에서 link traversal 로그 확인 |
| stale policy | source 변경 시 claim 재검증/폐기 | outdated claim 자동 표시 |

#### P1. Observability / LangSmith-compatible Eval

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| trace schema | Langfuse trace와 LangSmith-compatible fields 병행 저장 | run/tool/model/latency/cost/error 저장 |
| eval cases | 저품질/중단/반복 실패를 eval case로 자동 승격 | quality 실패 상위 패턴이 eval queue에 적재 |
| failure-to-directive | 반복 오류에서 지시서 초안 생성 | CEO 승인 전 review_hold/awaiting_approval 분리 |

#### P1. Hermes Pattern

| 작업 | 내용 | 완료 기준 |
|---|---|---|
| skill draft generator | 5회 이상 반복된 성공/실패 절차를 skill 초안으로 제안 | 초안은 비활성 상태로 저장 |
| bot mode profile | AADS Ops, GO100 Market Ops, KIS Risk Ops, NTV2 Merchant Ops 등 역할별 bot profile | project/role/tool policy가 분리됨 |
| gateway abstraction | chat/mobile/push/Telegram/PC Agent 이벤트를 같은 task event로 통합 | 완료/중단/승인 요청 알림 일관화 |

### 13.6 왜 지금 이 순서가 맞나

| 선택지 | 장점 | 문제 | 판정 |
|---|---|---|---|
| LangSmith SaaS 즉시 연결 | trace UI와 eval 기능 빠름 | 비용/데이터 반출/기존 Langfuse 중복 | P1 이후 opt-in |
| Hermes Agent 런타임 도입 | self-improving skill 완성도가 높음 | AADS 권한/시크릿/배포 제어 충돌 위험 | 직접 도입 비권장 |
| OpenWiki CLI 즉시 CI 적용 | agent-readable docs 빠르게 생성 | AADS 메모리/DB 원장과 분리될 수 있음 | 내부 wiki schema 후 선택 적용 |
| OHVISHarness 먼저 구현 | 기존 자산 보존, 권한/감사/재개 기준 통일 | 초기 설계 필요 | P0 권장 |
| Skill Find 먼저 구현 | 즉시 체감, 프롬프트 비대화 완화 | policy 없는 skill은 위험 | P0, policy 포함 조건 |

### 13.7 Runner 지시서 초안

```text
>>>DIRECTIVE_START
TASK_ID: AADS-OHVIS-HARNESS-SKILL-WIKI-HERMES-001
TITLE: OHVIS Harness Kernel, Skill Find, LLM Wiki, Hermes Pattern 통합 기반 구현
PRIORITY: P1-HIGH
SIZE: XL
DESCRIPTION:
1. 기존 LangGraph, LangChain Core/Provider/MCP adapter, Langfuse, memory_facts, .claude/skills 구조를 보존한다.
2. `OHVISHarness` 실행 계약을 추가해 chat/task/loop/runner/tool/trace/wiki/skill을 `graph_run_id`로 연결한다.
3. Skill Find Layer를 구현한다: skill registry/search/version/hash/source/provenance/risk tier/approval policy.
4. LangGraph checkpoint/interrupt/resume을 report/audit/code/deploy/browser-collection/financial-risk template에 적용한다.
5. LangChain middleware와 유사한 tool policy adapter를 구현해 approve/edit/reject/respond를 표준화한다.
6. Langfuse를 유지하면서 LangSmith-compatible trace/eval schema를 내부 DB에 저장한다. 외부 LangSmith 전송은 opt-in으로 둔다.
7. LLM Wiki/OpenWiki Layer를 구현한다: wiki_sources/pages/links/claims/error_book, search/read/follow tool, report compiler.
8. Hermes Agent는 런타임 도입이 아니라 skill self-improvement, bot mode, gateway, scheduled automation 패턴만 흡수한다.
9. `/ops/harness`, `/ops/skills`, `/ops/memory`, `/ops/traces` 화면 초안을 만든다.
10. 기존 unrelated dirty 파일은 건드리지 말고 isolated worktree 또는 Runner 의존성 그래프로 진행한다.
11. 검증: DB migration 전후 SELECT, unit tests, route import, API smoke, dashboard screenshot 또는 API 폴백, blue-green 배포 전 clean SHA 확인, 배포 시 5분 P0/P1 모니터링.
>>>DIRECTIVE_END
```

### 13.8 이번 보고의 완료 상태

| 항목 | 상태 |
|---|---|
| 외부 자료 수집 | 완료 |
| 내부 코드/DB/컨테이너 실측 | 완료 |
| 하네스 포함 보정 | 완료 |
| Hermes 포함 보정 | 완료 |
| Skill Find 포함 보정 | 완료 |
| 코드 구현 | 미수행, CEO 요청은 분석/기획 보고 |
| 테스트 | 문서 변경이므로 코드 테스트 미수행 |
| 커밋/푸시/배포 | 미수행 |

## 14. 구현 및 배포 보정판

- 보정 시각: 2026-09-07 10:47 KST
- CEO 추가 지시: 위 개선안을 즉시 구현하고 배포까지 진행한다. 기존 데이터가 소급 적용되는지도 확인한다.

### 14.1 구현 반영 범위

| 축 | 반영 파일 | 구현 내용 | 운영 활용 |
|---|---|---|---|
| OHVIS 하네스 | `app/services/ohvis_harness.py`, `app/api/ohvis_harness.py`, `app/main.py` | `/api/v1/ohvis/harness/status`, `/policies`, `/skill-find`, `/wiki/search`, `/hermes/recommend` API 추가 | 작업세션이 실행 전 하네스 상태, 위험 정책, 추천 스킬, 기존 기억, 개선 루프를 조회 |
| Skill Find | `app/services/ohvis_harness.py`, `migrations/158_ohvis_harness_skill_wiki_foundation.sql` | 내장 스킬 9종, repo-local `SKILL.md` 스캔, DB seed 기반 검색 | 프로젝트/intent/query 기준으로 필요한 스킬을 즉시 추천하고 risk tier를 반환 |
| LLM Wiki | `app/services/ohvis_harness.py`, `migrations/158_*`, `migrations/159_*` | wiki 테이블, memory fallback 검색, 고가치 `memory_facts` 1차 소급 백필 | 기존 장기기억을 `/wiki/search`에서 바로 활용하고, 점진적으로 wiki page로 승격 |
| LangSmith-compatible trace | `migrations/158_ohvis_harness_skill_wiki_foundation.sql` | `ohvis_harness_traces` 추가 | 외부 LangSmith 전송 없이 내부 trace/eval 저장 기반 확보 |
| Hermes pattern | `app/services/ohvis_harness.py` | recall -> select_skill -> execute_with_gate -> learn -> self_improve 권장 액션 API | 반복 실패를 error book/skill 개선 후보로 승격하는 운영 지침 제공 |

### 14.2 기존 데이터 소급 적용 정책

| 구분 | 현재 적용 | 이유 | 다음 확장 |
|---|---|---|---|
| `memory_facts` 전체 1:1 복사 | 미적용 | 약 6만 건 이상 전체 복사는 배포 중 DB 부하와 중복 wiki 품질 저하 위험 | 별도 background compiler로 배치 처리 |
| 고가치 기억 1차 소급 | 적용 대상 | AADS/CEO/GO100/KIS/NAS/NTV2/SF의 의사결정·장애·기능·API·구조 facts를 프로젝트별 최대 300건 승격 | source manifest, link graph, stale policy 추가 |
| 즉시 검색 활용 | 적용 | `ohvis_wiki_pages`에 결과가 없으면 `memory_facts`로 fallback | fallback hit를 wiki 승격 후보로 자동 기록 |
| Skill seed | 적용 | `ops_skill_library`에 내장 운영 스킬 9종 seed | repo/plugin skill version hash 동기화 |

결론: 기존 데이터는 “전량 복사”가 아니라 “검색 fallback + 고가치 1차 wiki 백필”로 소급 적용한다. 운영 안정성을 위해 전체 소급은 별도 배치 작업으로 분리한다.

### 14.3 배포 검증 기준

| 검증 | 완료 기준 |
|---|---|
| 단위 테스트 | `python3 -m pytest -q tests/unit/test_ohvis_harness.py` 통과 |
| 마이그레이션 | `158_*`, `159_*` 직접 적용 후 foundation table 8개, seed skill 9개, wiki page count 증가 확인 |
| API import | 운영 컨테이너에서 `app.main:app` 라우트에 `/api/v1/ohvis/harness/*` 등록 확인 |
| API smoke | 외부 HTTPS에서 인증 게이트 또는 정상 JSON 응답 확인, 컨테이너 내부 직접 호출로 200 응답 확인 |
| 배포 | `deploy.sh bluegreen` 단일 release SHA, `--no-build` slot start, 후보 health, same-digest standby, 5분 P0/P1 모니터링 |
