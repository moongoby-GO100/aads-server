# AADS Knowledge-to-Wisdom Evolution Research Report

> 작성 시각: 2026-05-18 08:04 KST  
> 대상 프로젝트: AADS 및 연동 프로젝트 KIS, GO100, SF, NTV2, NAS  
> 목적: AADS 개발/운영에 필요한 자료와 지식을 수집, 분류, 저장, 관리하고 이를 지혜화하여 시스템 진화와 발전에 연결하는 운영 아키텍처 제안

## 1. 요약

AADS는 이미 `memory_facts`, `ai_observations`, `ai_meta_memory`, `research_archive`, `compiled_prompt_provenance`, `quality_score`, `error_pattern`, Sleep-Time 정제 구조를 갖고 있다. 다음 단계는 단순 메모리 축적이 아니라 **자료 원본 -> 검증 가능한 근거 -> 지식 사실 -> 운영 판단 규칙 -> 실험/진화 액션**으로 승격시키는 지식 운영 체계를 만드는 것이다.

권장 구조는 "RAG + 지식그래프 + 버전형 메모리 + 평가 루프"의 하이브리드다. 벡터DB는 유사도 검색용 보조 인덱스이고, 최종 진화 판단은 출처, 시간, 신뢰도, 충돌관계, 적용범위, 실험 결과가 붙은 구조화 지식에서 내려야 한다.

## 2. 외부 최신 자료에서 확인한 핵심 방향

| 근거 | 확인 내용 | AADS 적용 의미 |
|---|---|---|
| NIST AI RMF Generative AI Profile, 2024-07-26, updated 2026-04-08 | 생성형 AI는 설계, 개발, 사용, 평가 전 생애주기에서 신뢰성 리스크를 관리해야 한다. | AADS 지식도 수집/저장만으로 끝내지 말고 수명주기, 검증, 감사, 책임 주체를 붙여야 한다. |
| OWASP LLM Top 10 2025 | Prompt Injection, Sensitive Information Disclosure, Data/Model Poisoning, Excessive Agency, System Prompt Leakage, Vector and Embedding Weaknesses가 핵심 위험이다. | 외부 문서, 업로드 파일, 웹 검색 결과, 벡터 스토어는 모두 공격면이다. ingestion 단계에서 출처 신뢰도와 악성 지시 제거가 필요하다. |
| OpenAI Retrieval docs, 2026년 확인 | Retrieval API는 벡터 스토어를 데이터 인덱스로 사용하며 semantic search와 attribute filtering을 제공한다. | AADS는 pgvector/embedding 검색을 유지하되 project, source_type, date, sensitivity, confidence 필터를 강제해야 한다. |
| Google Vertex AI Grounding with Vertex AI Search, last updated 2026-05-15 | 기업 문서/웹사이트 데이터로 모델 응답을 grounded response로 만들고 Google Search grounding과 결합할 수 있다. | 내부 문서 grounded RAG와 최신 웹 검색 grounded RAG를 분리하고, 답변마다 내부/외부 출처를 표시해야 한다. |
| Google Gemini Enterprise Agent Platform Memory Bank, last updated 2026-05-15 | 장기 사용자 선호와 사실을 저장하고 생성/수집/조회/리비전/접근통제 기능을 분리한다. | AADS memory는 scope(project, role, user, session), revision, access policy를 명시해야 한다. |
| Anthropic Claude Code Memory docs | 프로젝트별 `MEMORY.md`는 concise index로 두고 세부 주제 파일은 필요할 때 읽는 구조다. | AADS도 전체 메모리를 프롬프트에 밀어 넣지 말고 index -> on-demand retrieval 구조로 유지해야 한다. |
| LangChain Long-term Memory docs | 장기 메모리는 thread 밖에서 유지되고 namespace/key로 계층화된 JSON 문서로 저장된다. | AADS의 project/workspace/role/intent namespace 설계는 현행 프롬프트 레이어와 맞다. DB schema에도 namespace를 명시해야 한다. |
| Microsoft GraphRAG, 2024 | GraphRAG는 text extraction, network analysis, LLM prompting/summarization을 결합해 텍스트 데이터셋을 관계 중심으로 이해한다. | 단순 청크 검색으로는 "어떤 결정이 어떤 장애를 낳았는가" 같은 질문에 약하다. `related_facts`, decision graph를 강화해야 한다. |
| Memory for Autonomous LLM Agents, arXiv 2026-03 | 에이전트 메모리는 write-manage-read 루프이며 temporal scope, representation, control policy를 함께 다뤄야 한다. | AADS의 저장 파이프라인은 write path filtering, contradiction handling, privacy governance를 1급 기능으로 다뤄야 한다. |
| ByteRover, arXiv 2026-04 | LLM이 직접 큐레이션하는 계층형 Context Tree, provenance, lifecycle, importance, recency decay를 제안한다. | AADS의 Sleep-Time 정제는 "중요도, 성숙도, 최신성, 출처"를 기준으로 memory_facts를 승격/강등해야 한다. |
| LightRAG, arXiv 2024 / EMNLP Findings 2025 | 그래프 구조와 dual-level retrieval로 저수준/고수준 지식 검색을 결합한다. | AADS는 keyword, vector, graph, SQL exact lookup의 4중 검색을 query type별로 라우팅해야 한다. |

## 3. 현재 AADS 상태와 강점

2026-05-18 08:04 KST 실측 기준:

| 항목 | 값 | 근거 |
|---|---:|---|
| `memory_facts` | 48,347건 | DB 조회 |
| `ai_observations` | 1,461건 | DB 조회 |
| `ai_meta_memory` | 4,183건 | DB 조회 |
| 주요 메모리 테이블 | `memory_facts`, `ai_observations`, `ai_meta_memory`, `research_archive`, `project_memory`, `procedural_memory`, `experience_memory`, `system_memory` | DB schema 조회 |
| 핵심 문서 | `docs/MEMORY_EVOLUTION_ARCHITECTURE.md`, `docs/SYSTEM_PROMPT_ARCHITECTURE.md` | 파일 확인 |

현재 구조의 강점:

- `memory_facts`에 `confidence`, `embedding`, `referenced_count`, `superseded_by`, `related_facts`, `tags`가 이미 있어 버전형 지식 기반으로 확장 가능하다.
- `ai_observations`와 `ai_meta_memory`가 분리되어 있어 원시 관찰과 증류된 운영 규칙을 분리할 수 있다.
- `research_archive`가 `topic`, `query`, `sources`, `summary`, `full_report`, `model_used`, `cost`, `session_id`를 갖고 있어 리서치 결과 보존의 기본 골격이 있다.
- `memory_recall` 10섹션과 프롬프트 provenance가 있어 "무엇이 프롬프트에 실제 적용됐는지"를 검증할 수 있다.

현재 리스크:

| 리스크 | 영향 | 보완 방향 |
|---|---|---|
| 메모리 축적량 대비 검증/승격 기준이 약함 | 오래되거나 충돌하는 지식이 회상될 수 있음 | `maturity_state`, `evidence_level`, `superseded_by`, `verified_at` 기준 승격 |
| 벡터 검색과 구조화 사실의 역할 혼재 | 유사도 높은 낡은 문서가 정답처럼 쓰일 수 있음 | exact lookup -> graph -> vector -> web 순 라우팅 |
| 외부 자료 ingestion 보안 경계 부족 | prompt injection, poisoning, system prompt leakage 위험 | OWASP 기준 source trust, sanitizer, secret scan, instruction stripping |
| 지식이 행동으로 이어지는 경로가 불명확 | 학습은 많지만 진화 액션이 일관되지 않음 | fact -> insight -> hypothesis -> experiment -> decision -> prompt/code change 파이프라인 |
| 연구 보고서가 DB/파일/프롬프트로 나뉘어 흩어질 가능성 | 재사용성과 감사성이 낮아짐 | research_archive + markdown report + memory_facts 핵심요약 동시 기록 |

## 4. 목표 모델: DIKW+E

AADS는 일반 DIKW(Data, Information, Knowledge, Wisdom)에 Evolution을 붙인 **DIKW+E** 모델로 운영하는 것이 적합하다.

| 단계 | 의미 | AADS 저장 단위 | 예시 |
|---|---|---|---|
| Data | 원천 자료 | raw source, file snapshot, URL, DB row, log line | Git diff, 장애 로그, 공식문서 URL |
| Information | 정규화된 근거 | evidence chunk, metadata, source score | "OWASP LLM08는 vector weakness를 지목" |
| Knowledge | 검증된 사실 | `memory_facts`, related facts, tags | "AADS는 vector store retrieval에 source filtering이 필요" |
| Wisdom | 적용 가능한 판단 규칙 | `ai_meta_memory`, prompt asset, policy | "외부 문서는 ingestion sanitizer 통과 전 프롬프트 주입 금지" |
| Evolution | 실험/개선/배포 | directive, runner job, eval result, changelog | "P0: research_archive to memory_facts 승격 배치 구현" |

핵심 원칙:

1. 원천 자료는 삭제하지 않고 보존한다.
2. LLM 요약은 원천 근거를 대체하지 않는다.
3. 지식은 confidence만이 아니라 provenance, freshness, scope, sensitivity, contradiction 상태를 가져야 한다.
4. 지혜는 "정답 문장"이 아니라 "다음 행동을 바꾸는 검증된 규칙"으로 취급한다.
5. 진화는 prompt/code/tool/DB/workflow 변경으로 귀결되고, 변경 전후 평가가 있어야 한다.

## 5. 수집 체계

### 5.1 내부 자료

| 수집 대상 | 수집 주기 | 저장 위치 | 승격 기준 |
|---|---|---|---|
| 채팅 대화와 CEO 교정 | 매 턴, 20턴 배치 | `chat_messages`, `ai_observations`, `session_notes` | 명시적 지시, 반복 교정, 품질평가 실패 |
| 코드 변경과 커밋 | commit/push 시 | `memory_facts(file_change/config_change)` | 테스트 통과, 배포 반영, HANDOVER 기록 |
| 러너/에이전트 작업 결과 | 작업 종료 시 | directive/task tables, `memory_facts` | 성공/실패 원인과 재발 방지 규칙 |
| 장애 로그 | 에러 감지 시 | `error_pattern`, `error_resolution` | 동일 원인 재발, 복구 절차 확인 |
| DB 스키마/운영 수치 | 스냅샷/변경 시 | `project_snapshot`, `data_model_change` | 스키마 변경, 운영 임계치 변화 |
| 디자인/제품 기획 문서 | 문서 변경 시 | docs + `project_insight` | CEO 승인, 화면/기획 반영 |

### 5.2 외부 자료

| 자료 유형 | 예시 | 처리 원칙 |
|---|---|---|
| 공식 API/모델 문서 | OpenAI, Anthropic, Google, LangChain | 공식 URL 우선, last updated 기록, 모델/가격/제약은 최신 재조회 |
| 보안/거버넌스 표준 | NIST, OWASP, ISO | 정책/가드레일 후보로 분류, 적용 전 위험도 매핑 |
| 논문/기술 리포트 | arXiv, Microsoft Research | "검증 전 연구"로 분류하고 실험 계획을 붙임 |
| 프로젝트 도메인 자료 | 금융, 쇼핑몰, 숏폼, SNS 정책 | 프로젝트별 compliance/sensitivity 라벨 필수 |
| 시장/경쟁/제품 자료 | 벤치마크, 경쟁 서비스, 가격 | 날짜, 출처, 측정 방식 없으면 미검증 |

수집 시 필수 메타데이터:

- `source_uri`, `source_title`, `publisher`, `published_at`, `accessed_at_kst`
- `project`, `domain`, `source_type`, `language`, `license`
- `trust_level`: official, primary_research, vendor_docs, reputable_media, community, unknown
- `sensitivity`: public, internal, confidential, secret
- `freshness_policy`: static, versioned, volatile, real_time
- `ingestion_status`: raw, sanitized, chunked, embedded, verified, promoted, archived

## 6. 분류 체계

권장 taxonomy는 다음 8축이다.

| 축 | 값 예시 | 목적 |
|---|---|---|
| Project | AADS, KIS, GO100, SF, NTV2, NAS, GLOBAL | 프로젝트 오염 방지 |
| Domain | prompt, memory, runner, deploy, security, finance, design, media | 검색/승격 범위 제한 |
| Knowledge Type | fact, decision, procedure, policy, lesson, hypothesis, experiment, metric | 지식의 사용법 결정 |
| Evidence Level | observed, source-backed, tested, deployed, CEO-approved, deprecated | 답변 신뢰도 표시 |
| Freshness | static, time-sensitive, volatile, real-time | 재조회 필요 여부 |
| Sensitivity | public, internal, confidential, secret | 프롬프트 주입/외부 전송 통제 |
| Lifecycle | raw, candidate, verified, promoted, superseded, rejected, archived | 메모리 GC와 충돌 해결 |
| Scope | global, project, role, session, user, task | 회상 범위 통제 |

현재 `memory_facts.category`는 file_change/error_resolution 중심으로 강하다. 다음 카테고리를 추가하면 지혜화에 유리하다.

- `external_source`
- `evidence_chunk`
- `research_finding`
- `hypothesis`
- `experiment_result`
- `wisdom_rule`
- `governance_policy`
- `knowledge_conflict`
- `retrieval_eval`
- `prompt_asset_effect`

## 7. 저장/관리 아키텍처

### 7.1 저장 계층

| 계층 | 역할 | 권장 저장소 |
|---|---|---|
| Raw Archive | 원문 보존, 재처리 가능성 확보 | 파일 스냅샷, object storage, `research_archive.sources` |
| Evidence Store | 문서 단위/문단 단위 근거 | `evidence_chunks` 신규 테이블 또는 `memory_facts` 확장 |
| Fact Store | 검증된 사실 | 현행 `memory_facts` |
| Meta Memory | 운영 규칙/교훈/CEO 선호 | 현행 `ai_meta_memory` |
| Graph Index | 원인, 영향, 의존성 | `related_facts`, `query_decision_graph`, 신규 edge table |
| Vector Index | 의미 검색 | `embedding`/pgvector |
| Report Library | 사람이 읽는 연구 보고서 | `docs/reports/*.md`, `research_archive.full_report` |
| Prompt Assets | 실제 행동 반영 | `prompt_assets`, provenance |

### 7.2 권장 DB 확장

기존 테이블을 유지하면서 다음 필드를 추가하거나 별도 테이블을 붙이는 방식이 안전하다.

```sql
-- 개념 설계: 실제 적용 전 migration 검토 필요
knowledge_sources(
  id uuid primary key,
  source_uri text,
  source_title text,
  publisher text,
  published_at timestamptz,
  accessed_at timestamptz,
  trust_level text,
  license text,
  checksum text,
  raw_snapshot_path text,
  created_at timestamptz
)

evidence_chunks(
  id uuid primary key,
  source_id uuid references knowledge_sources(id),
  project text,
  domain text,
  chunk_text text,
  metadata jsonb,
  embedding vector,
  sensitivity text,
  ingestion_status text,
  created_at timestamptz
)

knowledge_promotions(
  id uuid primary key,
  evidence_id uuid,
  memory_fact_id uuid,
  promotion_reason text,
  evaluator text,
  evidence_level text,
  promoted_at timestamptz
)

knowledge_evals(
  id uuid primary key,
  eval_name text,
  query text,
  expected_evidence_ids uuid[],
  retrieved_evidence_ids uuid[],
  answer_quality numeric,
  citation_quality numeric,
  created_at timestamptz
)
```

## 8. 지혜화 파이프라인

권장 파이프라인:

1. **수집**: 내부 이벤트와 외부 자료를 원문으로 저장한다.
2. **정화**: secret scan, prompt injection 패턴 제거, HTML/광고/중복 제거, 라이선스 기록.
3. **분해**: 문서 -> evidence chunk -> fact 후보로 분해한다.
4. **분류**: project/domain/type/evidence/sensitivity/freshness 라벨을 붙인다.
5. **검증**: 공식/DB/로그/테스트/CEO 결정과 대조한다.
6. **연결**: related_facts, superseded_by, depends_on_subject, source_id를 연결한다.
7. **승격**: verified fact -> wisdom_rule/prompt_asset/procedure 후보로 올린다.
8. **적용**: 프롬프트, 도구 라우팅, 러너 정책, UI, 문서, 코드로 반영한다.
9. **평가**: retrieval precision, citation coverage, conflict rate, task success를 측정한다.
10. **망각/보존**: deprecated/superseded 지식은 회상에서 제외하고 archive에는 보존한다.

중요한 설계 판단:

- 외부 자료는 바로 `ai_meta_memory`에 넣지 않는다. 먼저 `research_archive`와 `evidence_chunks`에 넣고, 검증된 항목만 `memory_facts`로 승격한다.
- CEO 명시 지시는 `ceo_instruction`/`ceo_preference`로 즉시 승격하되, 적용 범위와 충돌 여부를 함께 기록한다.
- 코드/배포 사실은 `git commit`, `test result`, `health check`, `HANDOVER` 중 최소 2개 근거가 있을 때 운영 지식으로 승격한다.

## 9. 검색/회상 전략

AADS 답변 품질을 위해 검색 라우팅을 다음 순서로 고정하는 것을 권장한다.

| 질문 유형 | 1순위 | 2순위 | 3순위 |
|---|---|---|---|
| 현재 운영 상태 | DB/log/health exact lookup | git status | 없음 |
| 코드 구현 방식 | `rg`, source file | CKP/semantic code | external docs |
| CEO 지시/선호 | `ai_meta_memory`, `ai_observations` | chat history | 없음 |
| 아키텍처 결정 이유 | decision graph | related facts | HANDOVER |
| 외부 최신 지식 | official docs/web search | paper/vendor docs | community |
| 반복 장애 해결 | error_pattern/error_resolution | logs | code history |

검색 품질 기준:

- 답변에 들어간 핵심 주장은 출처 id 또는 파일 경로를 가져야 한다.
- 동일 subject에 superseded fact가 있으면 최신 fact만 회상한다.
- volatile knowledge는 답변 전 재조회한다.
- vector retrieval 결과는 exact metadata filter를 통과해야 한다.
- 검색 결과가 내부 DB와 외부 자료가 충돌하면 내부 운영 사실을 우선하고 외부는 참고로 표시한다.

## 10. 보안/거버넌스

OWASP 2025 기준 AADS 지식 시스템의 주요 방어선:

| 위험 | AADS 방어 |
|---|---|
| Prompt Injection | ingestion sanitizer, retrieved text를 instruction으로 실행 금지, source trust 표시 |
| Sensitive Information Disclosure | sensitivity label, secret scan, 외부 LLM 전송 차단 |
| Supply Chain | source publisher/license/checksum 기록, dependency 문서 provenance |
| Data/Model Poisoning | external source trust score, duplicate/cross-source validation, suspicious chunk quarantine |
| Excessive Agency | 지식 승격과 실행 액션 사이 human approval/evidence gate |
| System Prompt Leakage | prompt assets와 memory를 public report에 자동 노출 금지 |
| Vector and Embedding Weaknesses | tenant/project filter, source ACL, embedding index audit |
| Misinformation | citation required, stale fact decay, contradiction detection |
| Unbounded Consumption | retrieval top-k/token budget, background consolidation, report summarization |

## 11. 평가 지표

수치를 확정하려면 별도 계측이 필요하므로, 여기서는 측정 항목과 완료 기준만 정의한다.

| 지표 | 측정 방법 | 완료 기준 |
|---|---|---|
| Retrieval Precision | 골든 질문별 top-k 근거 적중률 | 기준선 측정 후 개선 추적 |
| Citation Coverage | 답변 핵심 주장 중 출처 연결 비율 | 운영 보고/리서치 보고 100% 목표 |
| Conflict Rate | 동일 subject의 충돌 fact 비율 | 신규 충돌은 `knowledge_conflict`로 자동 기록 |
| Stale Recall Rate | superseded/deprecated fact가 답변에 쓰인 비율 | 0건 유지 |
| Promotion Accuracy | 승격된 wisdom_rule이 재교정 없이 유지되는 비율 | CEO 교정과 품질평가로 측정 |
| Task Impact | 지식 적용 후 러너 실패/재작업 변화 | 프로젝트별 추세로 측정 |
| Governance Coverage | source/sensitivity/freshness 누락률 | 신규 외부 지식 0건 누락 목표 |

## 12. 구현 로드맵

### P0: 지식 수집/승격 안전장치

1. `knowledge_sources`, `evidence_chunks`, `knowledge_promotions`, `knowledge_evals` migration 설계.
2. `research_archive` 저장 시 source metadata와 report file path를 함께 기록.
3. 외부 자료 ingestion sanitizer: prompt injection marker, secret pattern, HTML noise 제거.
4. `memory_facts` 승격 함수에 evidence_level, source_id, freshness, sensitivity 라벨 추가.
5. 회상 시 `superseded_by IS NULL`, project/scope/sensitivity filter 강제.

### P1: 지식그래프와 평가 루프

1. `related_facts` 자동 연결: decision -> file_change -> error_resolution -> lesson.
2. 골든 질문 세트 구축: AADS 운영, KIS/GO100, SF/NTV2, 프롬프트 거버넌스, 배포/러너.
3. retrieval eval 실행 및 품질 대시보드 추가.
4. Sleep-Time 정제에서 duplicate, contradiction, stale fact, promotion candidate를 매일 산출.
5. CEO 대시보드에 "지식 승격 후보/충돌/폐기 후보" 큐 표시.

### P2: 지혜화 자동 실행

1. 지식에서 hypothesis 자동 생성: "반복 실패 원인", "도구 라우팅 개선", "프롬프트 충돌" 후보.
2. hypothesis -> experiment -> directive/runner 연결.
3. 실험 결과가 성공하면 prompt asset, tool rule, documentation, code change로 승격.
4. 실패한 실험은 `experience_failure`와 `error_pattern`으로 저장하고 재시도 조건을 기록.

## 13. AADS 운영 규칙 제안

1. **원문 우선**: 리서치/웹검색/업로드 파일은 원문 URL 또는 snapshot 없이는 memory_facts로 승격하지 않는다.
2. **출처 없는 지식 금지**: 운영 판단에 쓰이는 지식은 DB row, 파일 경로, URL, 로그, 테스트 결과 중 하나를 가져야 한다.
3. **벡터는 인덱스, 지식은 구조화 사실**: 벡터 유사도만으로 행동 규칙을 만들지 않는다.
4. **CEO 교정은 최상위 신호**: "절대/반드시/금지"는 즉시 `ceo_instruction`으로 저장하고 충돌 지식은 deprecated 처리한다.
5. **최신성 분리**: 법규, 가격, 모델 스펙, API 제약, 스포츠/뉴스/시장자료는 매 답변 전 재조회 대상으로 표시한다.
6. **승격은 평가 후**: 리서치 finding은 후보이고, 운영 wisdom은 검증 후 승격한다.
7. **프롬프트 적용 검증**: prompt asset으로 승격된 지식은 `compiled_prompt_provenance`에서 실제 적용 확인 전 완료로 보지 않는다.

## 14. 이번 연구의 결론

AADS의 진화 능력은 "얼마나 많이 기억하느냐"보다 "어떤 근거를 어떤 범위에서 신뢰하고, 언제 행동 규칙으로 승격하며, 실패하면 어떻게 폐기하느냐"에 달려 있다. 현행 AADS는 메모리 테이블과 프롬프트 레이어가 이미 준비되어 있으므로, 다음 투자는 신규 벡터DB 도입보다 **지식 provenance, lifecycle, conflict handling, eval gate, wisdom promotion**에 두는 것이 맞다.

## 15. 참고 출처

- NIST, "Artificial Intelligence Risk Management Framework: Generative Artificial Intelligence Profile", published 2024-07-26, updated 2026-04-08: https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence
- OWASP GenAI Security Project, "2025 Top 10 Risk & Mitigations for LLMs and Gen AI Apps": https://genai.owasp.org/llm-top-10/
- OWASP Foundation, "OWASP Top 10 for Large Language Model Applications": https://owasp.org/www-project-top-10-for-large-language-model-applications/
- OpenAI, "Retrieval", accessed 2026-05-18 KST: https://developers.openai.com/api/docs/guides/retrieval
- Google Cloud, "Grounding with Vertex AI Search", last updated 2026-05-15 UTC: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/grounding/grounding-with-vertex-ai-search
- Google Cloud, "Gemini Enterprise Agent Platform - Memory Bank", last updated 2026-05-15 UTC: https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale
- Anthropic, "How Claude remembers your project", accessed 2026-05-18 KST: https://code.claude.com/docs/en/memory
- LangChain, "Long-term memory", accessed 2026-05-18 KST: https://docs.langchain.com/oss/python/langchain/long-term-memory
- Microsoft Research, "Project GraphRAG": https://www.microsoft.com/en-us/research/project/graphrag/
- Du, "Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers", arXiv 2603.07670, 2026-03: https://arxiv.org/abs/2603.07670
- Nguyen et al., "ByteRover: Agent-Native Memory Through LLM-Curated Hierarchical Context", arXiv 2604.01599, 2026-04: https://arxiv.org/abs/2604.01599
- Guo et al., "LightRAG: Simple and Fast Retrieval-Augmented Generation", arXiv 2410.05779, 2024-10 / EMNLP Findings 2025: https://arxiv.org/abs/2410.05779
