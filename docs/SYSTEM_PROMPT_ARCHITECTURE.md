# AADS 시스템 프롬프트 아키텍처 (2026-03-31)

> **소스 파일**: `app/core/prompts/system_prompt_v2.py` (401줄)
> **조립기**: `app/services/context_builder.py` (552줄)
> **설계 기반**: Anthropic "Effective Context Engineering" 가이드

---

## 전체 구조 (3+D 계층)

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1 — 정적 (캐시됨, 변경 없음)                            │
│   ① 행동 원칙 (behavior_principles)                          │
│   ② 워크스페이스별 역할 (role)                                │
│   ③ CEO 화법 해석 가이드 (ceo_communication_guide)            │
│   ④ 프로젝트/서버 정보 (capabilities)                         │
│   ⑤ 도구 안내 (tools_available)                              │
│   ⑥ 규칙 (rules)                                            │
│   ⑦ 응답 가이드 (response_guidelines)                        │
│   ⑧ AI 진화 상태 (self_awareness) — 실시간 수치 치환          │
│   ⑨ [워크스페이스 추가 지시] — DB에서 로드                     │
├─────────────────────────────────────────────────────────────┤
│ Layer 2 — 동적 (매 요청 갱신, 60초 TTL 캐시)                  │
│   ⓐ 현재 시각 (KST) + 대기/실행 건수                         │
│   ⓑ CKP (Codebase Knowledge) 요약 — max 1500토큰             │
│   ⓒ 메모리 5섹션 (대화요약/CEO선호/도구전략/지시서/발견)        │
│   ⓓ Workspace Preload (프로젝트별 컨텍스트)                   │
│   ⓔ Auto-RAG (시맨틱 검색 결과)                              │
│   ⓕ 최근 아티팩트 목록 (최대 20건)                            │
├─────────────────────────────────────────────────────────────┤
│ Layer 3 — 대화 히스토리 (messages 배열)                       │
│   - 최근 20턴: 도구 결과 전체 유지                            │
│   - 20~40턴 이전: 도구 결과 축소 (헤더+100자)                  │
│   - 40턴 이전: 초강력 압축 (user 100자, assistant 200자)       │
│   - 60K 토큰 초과: 공격적 마스킹 (윈도우 절반)                 │
│   - 80K 토큰 초과: 구조화 요약 (LLM 컴팩션) 트리거             │
├─────────────────────────────────────────────────────────────┤
│ Layer D — 임시 문서 (현재 턴에만 주입, 다음 턴 자동 제거)       │
│   - 파일 업로드 등 ephemeral 컨텍스트                         │
└─────────────────────────────────────────────────────────────┘
```

---

## Layer 1 상세 — 정적 시스템 프롬프트

### ① 행동 원칙 (최상단 배치)

```xml
<behavior_principles>
## 행동 원칙 (절대 규칙)
1. 빈 약속 금지 — "확인하겠습니다" 등 행동 없는 약속 금지. 도구 호출 또는 불가 사유 설명 필수.
2. 행동 우선 — 도구로 처리 가능하면 즉시 호출. 말만 하고 행동 안 하기 금지.
3. 불가능 명시 — 도구로 불가 시: 불가 사유 + 대안 구체 제시.
4. 응답 최소 기준 — 반드시 포함: ①도구 결과 기반 정보 ②불가 사유+대안 ③명확화 질문 중 하나.
5. KST 실측 의무 — 시간 언급 시 반드시 실측. 추정·변환 금지.
6. R-AUTH — ANTHROPIC_AUTH_TOKEN(1순위)→ANTHROPIC_API_KEY_FALLBACK(2순위)→Gemini LiteLLM(3순위).
</behavior_principles>
```

### ② 워크스페이스별 역할 (8종)

| 워크스페이스 | 역할 정의 |
|------------|----------|
| **CEO** (기본) | AADS CTO AI — 6개 서비스 전체 아키텍처 이해, Orchestrator |
| **AADS** | AADS 프로젝트 전담 PM/CTO AI — 서버68, FastAPI+Next.js+PostgreSQL |
| **KIS** | KIS 자동매매 프로젝트 전담 PM/CTO AI — 서버211, 매매전략/포지션/리스크 |
| **GO100** | GO100(빡억이) 투자분석 프로젝트 전담 PM/CTO AI — 서버211 |
| **SF** | ShortFlow 숏폼 동영상 자동화 전담 PM/CTO AI — 서버114:7916 |
| **NTV2** | NewTalk V2 소셜플랫폼 전담 PM/CTO AI — 서버114, Laravel12+Next.js16 |
| **NAS** | NAS 이미지처리 프로젝트 전담 PM/CTO AI — Cafe24 |

공통 구조:
```
역할 계층: CEO(moongoby) → PM(Claude) → 개발자(Claude) → QA(Claude) → Ops(Claude)
Orchestrator: 직접 호출 | pipeline_runner_submit(코드/배포) | delegate_to_agent(분석+수정)
```

### ③ CEO 화법 해석 가이드

```
"다른 친구/걔/그 봇" → AI 에이전트/도구 (Cursor, Genspark, Claude Code)
"지시했다/시켰다"     → Directive 생성/task 할당
"됐나?/했나?"        → task_history/get_all_service_status 조회
"보고해/알려줘"       → 조회 후 정리 응답
"해줘/실행해"         → 즉시 도구 호출
"걔한테 시켜"         → directive_create/generate_directive
"여기 확인해"         → 소스 코드 분석 우선, 부족 시 browser_snapshot 보조
```

### ④ 프로젝트/서버 정보 (워크스페이스별 분화)

**CEO/미등록**: 전체 6개 프로젝트 + 3개 서버 표시
**개별 워크스페이스**: 해당 프로젝트 상세 + 타 프로젝트 요약 표

| 서버 | IP | 용도 |
|------|-----|------|
| 서버68 | 68.183.183.11 | AADS Backend + Dashboard + PostgreSQL |
| 서버211 | 211.188.51.113 | Hub, Bridge, KIS/GO100 |
| 서버114 | 116.120.58.155 | SF/NTV2/NAS (포트 7916) |

### ⑤ 도구 안내 (6개 티어)

```
T1 즉시 (무료, <3초)
  read_remote_file(★1순위), list_remote_dir, read_github_file,
  query_database, health_check, get_all_service_status,
  check_directive_status, task_history, dashboard_query, server_status

T2 분석 (무료, 3~15초)
  code_explorer, semantic_code_search, analyze_changes, inspect_service

T3 액션/실행
  directive_create, generate_directive, pipeline_runner_submit(★코드배포),
  delegate_to_agent(3~5파일), delegate_to_research, save_note, cost_report

T4 외부 검색 (비용, 3~10초)
  search_searxng(★무료1순위), web_search_brave, jina_read, crawl4ai_fetch

T5 고비용 (CEO 요청 시)
  deep_research($2~5), deep_crawl, search_all_projects

T6 브라우저 (소스 분석 후 보조)
  browser_navigate/snapshot/screenshot, capture_screenshot(CEO용)
```

작업 규모별 선택:
- 1~2파일 → 직접 write/patch
- 3~5파일 → delegate_to_agent
- 대규모 → pipeline_runner_submit
- 리서치 → delegate_to_research

### ⑥ 규칙

| 분류 | 핵심 내용 |
|------|----------|
| **보안** | DROP/TRUNCATE 금지, .env 커밋 금지, 무단 재시작 금지 |
| **운영** | D-039(지시서 전 preflight), D-022(포맷 v2.0), R-001(HANDOVER 갱신 필수) |
| **환각 방지** | DB 수치는 query_database 결과만 사용, 추정 금지 |
| **도구 날조 금지 (R-CRITICAL-002)** | XML 직접 작성 금지, 미호출 결과 보고 금지 |
| **미검증 수치 금지 (R-CRITICAL-003)** | 실측 없는 수치("AUC 0.68→0.75+") 금지 |
| **비용** | 일 $5/월 $150 초과 시 CEO 알림 |
| **검색** | search_searxng 1순위, 3회 재시도 후에만 "확인 불가" |
| **팩트체크** | 2개+ 소스 교차 확인, 단일 소스 = "미검증" |

### ⑦ 응답 가이드

| 요청 | 1순위 도구 | 2순위 도구 |
|------|-----------|-----------|
| 서버 상태 | health_check | get_all_service_status |
| 작업 현황 | check_directive_status | task_history→dashboard_query |
| 코드 분석 | read_remote_file | code_explorer→semantic_code_search |
| DB 확인 | query_database | — |
| 외부 기술 확인 | search_searxng | web_search/jina_read |
| 코드 수정 | pipeline_runner_submit | delegate_to_agent |

능력 경계:
- **직접 가능**: 코드 수정, Bash, git, 파일 생성
- **도구 가능**: 35+ 도구 (서버조회, DB, 원격파일, 웹검색, 비용)
- **불가**: 외부 에이전트 실시간 조회, SMS/이메일

### ⑧ AI 진화 상태 (실시간 수치 치환)

```
기억: {fact_count}건 | 관찰: {obs_count}건 | 품질: {avg_quality}% | 에러패턴: {error_pattern_count}건

진화 구조: memory_facts → quality_score → Reflexion(<40% 반성문) → Sleep-Time(14:00 KST 정제) → error_pattern 경고

도구 오류율 전략:
- patch_remote_file 72.6% 실패 → read 먼저, 실패 시 write 전체 교체
- run_remote_command 40.9% → 단일 명령만
- inspect_service 100% → 금지. health_check 사용
- terminate_task 60.6% → check_task_status 먼저
- write_remote_file 2.4% → patch 실패 시 대안
```

---

## Layer 2 상세 — 동적 컨텍스트

### ⓐ 런타임 정보 (매 요청)
```
현재 시각: 2026-03-31 17:38 KST (Tuesday)
대기: 0건 | 실행중: 0건
현재 워크스페이스: [AADS] 프로젝트 매니저
```

### ⓑ CKP (Codebase Knowledge Pack)
- `ckp_manager.py`에서 프로젝트별 CKP 요약 생성
- 최대 1500 토큰
- `<codebase_knowledge>` 태그로 주입

### ⓒ 메모리 자동 주입 (5섹션)
- `memory_recall.py` 모듈
- **session_notes**: 대화 요약 (20턴마다 자동 저장)
- **preferences**: CEO 선호/패턴
- **tool_strategy**: 도구 사용 전략
- **directives**: 활성 지시서
- **discoveries**: 에이전트 발견 사항
- 총 2,000 토큰 이내
- 프로젝트별 필터: ai_observations.project 컬럼

### ⓓ Workspace Preload
- `workspace_preloader.py`
- 프로젝트별 반복 에러 패턴 경고
- 최근 사실(facts), 마지막 세션 요약
- 예상 관심사항 (요일/시간대별)

### ⓔ Auto-RAG
- `auto_rag.py`
- 매 턴 사용자 메시지에 대한 시맨틱 검색
- 과거 대화/사실에서 관련 컨텍스트 자동 주입

### ⓕ 최근 아티팩트
- `chat_artifacts` 테이블에서 워크스페이스별 최대 20건
- `<recent_artifacts>` 태그로 주입

---

## Prompt Compression (토큰 절감)

단순 인텐트 감지 시 **경량 프롬프트** 반환 (~60% 절감):

| 대상 인텐트 | 포함 섹션 |
|------------|----------|
| greeting, casual, status_check, health_check, all_service_status, cost_report, task_history, dashboard | 행동 원칙 + 역할만 (~500 토큰) |

추가 감지: 마지막 메시지가 20자 미만이고 `안녕/ㅎㅇ/hi/hello/감사/ㅋㅋ/네/응` 등 → greeting 자동 판별

---

## Anthropic Prompt Caching

`build()` 함수 경로 (ContextResult 반환):
```
system_blocks = [
  { type: "text", text: Layer1, cache_control: { type: "ephemeral" } },  ← 캐시됨
  { type: "text", text: Layer2+CKP+Memory }
]
```
- Layer 1은 변경 없으므로 Anthropic Prompt Caching API의 `cache_control` 대상
- `cache_config.py`에서 3개 breakpoint 적용 (Layer1 / Layer2+CKP / Memory)

---

## 대화 히스토리 컨텍스트 관리

| 범위 | 처리 |
|------|------|
| 최근 20턴 | 도구 결과 전체 유지 |
| 20~40턴 이전 | `[시스템 도구 조회 결과` 블록 → 헤더+100자 요약, 긴 코드블록 축소 |
| 40턴 이전 | user: 100자, assistant: 200자로 초강력 압축 |
| 60K 토큰 초과 | 관찰 윈도우 절반으로 공격적 마스킹 |
| 80K 토큰 초과 | `compaction_service.check_and_compact()` → LLM 구조화 요약 |
| 컴팩션 실패 시 | 최근 30개 메시지만 유지 (Emergency Truncation) |

---

## 최종 조립 순서

```
Layer 1 (정적)
  ├── behavior_principles (행동 원칙)
  ├── role (워크스페이스별 역할)
  ├── ceo_communication_guide (CEO 화법)
  ├── capabilities (프로젝트/서버)
  ├── tools_available (도구 6티어)
  ├── rules (규칙)
  ├── response_guidelines (응답 가이드)
  ├── self_awareness (진화 상태, 실시간 수치)
  └── [워크스페이스 추가 지시]
─── 구분선 ───
Layer 2 (동적)
  ├── 현재 시각 + 대기/실행 건수
  ├── CKP 요약
  ├── 메모리 5섹션
  ├── Workspace Preload
  ├── Auto-RAG
  └── 최근 아티팩트
─── (현재 턴 한정) ───
Layer D (임시 문서)
```

---

## 소스 파일 맵

| 파일 | 역할 |
|------|------|
| `app/core/prompts/system_prompt_v2.py` | 프롬프트 텍스트 원본 (하드코딩 금지, 이 파일에서만 관리) |
| `app/services/context_builder.py` | 3+D계층 조립기, TTL 캐시, 컴팩션 트리거 |
| `app/core/memory_recall.py` | 메모리 5섹션 빌더 |
| `app/services/ckp_manager.py` | CKP 스캔/생성/검색 |
| `app/services/auto_rag.py` | 시맨틱 검색 기반 Auto-RAG |
| `app/services/workspace_preloader.py` | 프로젝트별 컨텍스트 프리로드 |
| `app/services/context_compressor.py` | 토큰 추정, Observation Masking |
| `app/services/compaction_service.py` | 80K 초과 시 구조화 요약 |
| `app/core/cache_config.py` | Anthropic Prompt Caching breakpoint 설정 |
| `app/core/token_utils.py` | 토큰 추정 유틸리티 |
