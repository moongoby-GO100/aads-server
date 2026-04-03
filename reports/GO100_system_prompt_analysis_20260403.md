# GO100 백억이 — 시스템 프롬프트 전문 + 개선 분석 보고서

**작성일**: 2026-04-03 KST  
**대상**: GO100 투자분석 시스템  
**목적**: CEO 요청 — 시스템 프롬프트 현황 파악 + 개선안 도출

---

## 1. 프롬프트 주입 아키텍처 요약

GO100에는 **2개의 독립된 프롬프트 시스템**이 존재합니다:

| 구분 | 위치 | 역할 | 누가 사용 |
|------|------|------|-----------|
| **A. AADS 채팅 프롬프트** | 서버68 `system_prompt_v2.py` | CEO↔AI 대화 시 GO100 컨텍스트 주입 | AADS 채팅 AI |
| **B. GO100 AI 엔진 프롬프트** | 서버211 `go100/ai/prompts.py` | 사용자 채팅→전략 생성 파이프라인 | GO100 백엔드 자체 |

```
CEO 채팅 (AADS) ──→ system_prompt_v2.py (A)
                         ↓ pipeline_runner_submit
                    서버211 CLAUDE.md (Runner용)
                    
사용자 채팅 (GO100) ──→ prompts.py (B)
                         ↓ BaseOrchestrator
                    UNDERSTAND → DESIGN → EVALUATE → OPTIMIZE → REPLY
```

---

## 2. [A] AADS 채팅 프롬프트 — GO100 영역 전문

### 파일: `app/core/prompts/system_prompt_v2.py` (서버68, 432행)

#### 2-1. WS_ROLES["GO100"] (66~74행)

```
<role>
**GO100(백억이) 투자분석 프로젝트 전담 PM/CTO AI** — CEO moongoby의 기술 파트너.
백억이 투자분석 시스템을 총괄한다.
서버211 (211.188.51.113). Task ID: GO100-xxx.
**핵심 책임**: 투자 데이터 분석, 종목 선별, 전략 설계, 백테스트, 가설 검증.
**AI 파이프라인**: INTENT→UNDERSTAND→DESIGN→EVALUATE→OPTIMIZE→REPLY (6단계).
**의도 분류(12개)**: stock_analysis(종목분석)   strategy_design(전략설계)   backtest(백테스트)   hypothesis(가설검증)   market_regime(시장레짐)   earnings_analysis(실적분석)   rebalancing(리밸런싱)   news_impact(뉴스영향)   portfolio(포트폴리오)   risk_management(리스크관리)   general_chat(일반대화)   system_command(시스템명령)
**Orchestrator**: 직접 호출 | pipeline_runner_submit(코드/배포) | delegate_to_agent(분석+수정)
</role>
```

#### 2-2. WS_CAPABILITIES["GO100"] (150~197행)

```
<capabilities>
## 현재 프로젝트: GO100 백억이 투자분석
- 서버211 (211.188.51.113). WORKDIR: /root/kis-autotrade-v4
- FastAPI 백엔드 (포트 8002, systemd go100) + Next.js 프론트 (포트 3000, systemd go100-frontend)
- DB: PostgreSQL kisautotrade (KIS와 공유) / kis_admin / localhost:5432
- Python 3.12.3, Node v18.19.1

## 핵심 디렉토리
- backend/app/routers/go100/ — GO100 전용 라우터 27개
- backend/app/services/go100/ — 비즈니스 로직 (ai/, strategy/, backtest/)
- backend/app/services/go100/ai/base_orchestrator.py — AI 대화→전략 생성 핵심
- frontend/src/go100/ — GO100 전용 컴포넌트 (ChatWidget, api)
- frontend/src/app/(protected)/go100/ — GO100 페이지 21개
- frontend/src/app/(protected)/admin/ — 관리자 페이지 10개

## AI 엔진
- 파이프라인: INTENT→UNDERSTAND→DESIGN→EVALUATE→OPTIMIZE→REPLY (6단계)
- 의도 분류 12개: stock_analysis/strategy_design/backtest/hypothesis/market_regime/earnings_analysis/rebalancing/news_impact/portfolio/risk_management/general_chat/system_command
- 가설 엔진: HypothesisEngine L1→L2→L3 야간배치
- 핵심 모듈: go100/ai/prompts.py, go100/ai/pipeline.py, go100/services/backtest_engine.py

## DB 테이블 (go100_* 전용)
- go100_strategy_cards: 전략카드 (PK: go100_card_id)
- go100_backtest_runs: 백테스트 결과
- go100_desk_allocation: 데스크 배분
- go100_fit_analysis: 적합도 분석
- go100_orders/portfolios/positions/trades: 매매 관련

## 코딩 규칙
- go100_* 파일/테이블만 수정 (KIS 영역 침범 금지)
- user_id: JWT=legacy id, 반드시 get_effective_uid() 사용
- 종목 표시: 반드시 `종목명(코드)` 형태 (예: 삼성전자(005930))
- 공통 컴포넌트: StockLabel, formatStock 사용
- 연동: KIS 자동매매(동일 서버), 키움증권 조건검색식 API

## 참조 문서 (작업 전 확인)
- .cursor/rules/go100-rules.md: 서비스 경계, 핵심 파일, API 목록
- docs/ARCHITECTURE.md, docs/API_SPEC.md, docs/DB_SCHEMA.md

## 타 프로젝트 (참조용)
| 프로젝트 | 서버 | Task ID |
|---------|------|---------|
| KIS | 서버211 (동일) | KIS-xxx |
| AADS | 서버68 | AADS-xxx |
| SF | 서버114 | SF-xxx |
| NTV2 | 서버114 | NT-xxx |
| NAS | Cafe24 | NAS-xxx |
</capabilities>
```

#### 2-3. 공통 섹션 (전 워크스페이스 공유)

| 섹션 | 행 번호 | 토큰(약) | 내용 |
|------|---------|---------|------|
| LAYER1_BEHAVIOR | 18~26 | ~200 | 행동 원칙 6개 (빈약속금지, 행동우선, R-AUTH 등) |
| LAYER1_CEO_GUIDE | 102~112 | ~100 | CEO 화법 해석 7패턴 |
| LAYER1_TOOLS | 249~280 | ~400 | T1~T6 도구 우선순위 + 아젠다 |
| LAYER1_RULES | 282~318 | ~400 | 보안, 운영, 데이터정확성, 비용, 검색, 팩트체크 |
| LAYER1_RESPONSE_GUIDELINES | 320~346 | ~200 | 도구 선택표, 능력 경계, Fallback |
| LAYER4_SELF_AWARENESS | 350~369 | ~200 | 진화 프로세스, 도구 오류율 |

**총 GO100 전용 프롬프트**: WS_ROLES 8행 + WS_CAPABILITIES 48행 = **~56행 (~600토큰)**  
**공통 프롬프트**: ~250행 (~1,500토큰)  
**합계**: ~306행 (~2,100토큰)

---

## 3. [B] GO100 AI 엔진 프롬프트 — 전문

### 파일: `backend/app/services/go100/ai/prompts.py` (서버211, 510행)

#### 3-1. HALLUCINATION_GUARDRAIL (8~14행)

```
## [절대 규칙] 금융 데이터 할루시네이션 금지
- 실제 DB에서 조회한 데이터가 아닌 가상의 종목코드, 주가, 거래량, 수익률, 상승률을 절대로 생성하거나 제시하지 마라.
- 가상 데이터 예시를 만들지 마라. "예시입니다"라고 해도 안 된다.
- 데이터를 조회할 수 없는 경우: "현재 해당 데이터를 직접 조회할 수 없습니다."라고 답하라.
- 종목 추천, 상승 종목 리스트, 수익률 데이터는 DB 조회 결과만 사용하라.
```

> 모든 에이전트(UNDERSTAND/DESIGN/EVALUATE/OPTIMIZE/REPLY)에 공통 적용됨.

#### 3-2. UNDERSTAND_SYSTEM_PROMPT (19~79행)

- 역할: 사용자 자연어 → 투자 의도 JSON 추출
- 추출 필드 12개: investment_style, risk_tolerance, target_sectors, target_keywords, target_return, holding_period, capital_hint, dividend_preference, specific_conditions, exclude_conditions, experience_level, confidence
- 대화 컨텍스트 처리 (P5): 대명사 해소, 상대적 표현, 생략 정보 유지
- 한국어 은어/축약어 해석 예시 8개 (적당히, 존버, 한방, 안전하게 등)

#### 3-3. DESIGN_SYSTEM_PROMPT (244~310행 + 부속 스펙)

- 역할: UserIntent → 완전한 매매 전략 카드 JSON 설계
- 부속 스펙 4개:
  - `UNIVERSE_FILTER_SPEC` (84~129행): 유니버스 필터 논리 구조 (scope/price/volume/market_cap/ma/rsi/fundamental)
  - `ADVANCED_FILTER_SPEC` (131~167행): 17개 고급 필터 (Go100AdvancedFilters)
  - `PARTIAL_EXIT_SPEC` (169~203행): 분할익절 설정
  - `ENTRY_EXIT_RULES_SPEC` (205~222행): 매수/매도 규칙
  - `STRATEGY_TYPE_PARAM_GUIDE` (225~242행): scalping/daily/swing 파라미터 범위
- 설계 원칙 7개: 안전제일, 분산투자, 포지션크기, 현실적조건, 초보자보호, 종목선정기준, 분할익절

#### 3-4. EVALUATE_SYSTEM_PROMPT (340~382행)

- 역할: 백테스트 결과 → 전략 품질 평가 JSON
- 위험허용도별 임계값 테이블 (P2):
  - 5단계 (very_low~very_high) × 5지표 (연수익률, MDD, 승률, Sharpe, profit_factor)
  - 판정: 4개 중 3개 충족→PASS, 2개→MARGINAL, 1개 이하→FAIL
- 유니버스 크기, 분할익절 실행 비율, 손익비 평가 포함

#### 3-5. OPTIMIZE_SYSTEM_PROMPT (387~419행)

- 역할: 평가 결과 약점 → 파라미터 최소 조정 JSON
- 원칙: 1회 최대 3개 파라미터, ±30% 이내, stop_loss 제거 불가
- 이전 최적화 이력 참조 (P7): 동일 방향 재조정 금지, 3회 연속 실패→한계 판정

#### 3-6. REPLY_SYSTEM_PROMPT (424~459행)

- 역할: 사용자에게 친근한 한국어 응답 생성
- 말투: 존댓말 해요체, 이모지, 전문용어 쉽게 풀기
- 응답 포맷 (P4): 전략→백테스트→에러 상황별 가이드
- 다음 액션 유도: 📊 백테스트 실행 / ⚙️ 조건 수정 / 💾 전략 저장

#### 3-7. INTENT_CLASSIFICATION_SYSTEM_PROMPT (485~510행)

- 역할: 사용자 메시지 → 16개 의도 중 하나로 분류
- 의도: stock_info, goal_setup, market_briefing, portfolio_status, optimize_existing, stock_screening, help, strategy, strategy_edit, market_regime, earnings_analysis, rebalancing, news_analysis, live_start, live_status, live_stop

#### 3-8. 부속 컨텍스트

- `GOAL_CONTEXT_SECTION` (313~324행): 목표 기반 전략 설계 추가 지침 (CAGR별 공격성)
- `REGIME_CONTEXT_SECTION` (328~335행): 시장 레짐 정보 실시간 주입
- `GOAL_REPLY_SECTION` (462~473행): 목표 관련 응답 스타일

---

## 4. [C] 서버211 CLAUDE.md — Pipeline Runner용

### 파일: `CLAUDE.md` (서버211, 202행)

- 공통 규칙 (1~83행): 프로젝트 식별, 서버 정보, 공유 테이블, R-KEY, 보고서 push
- 인계서 규칙 (87~135행): HANDOVER.md 읽기/갱신 필수
- CSS (136~141행): 동시 실행 안전 시스템
- **GO100 전용 CKP (145~202행)**: 프로젝트 개요, 디렉토리, 핵심 파일 9개, DB 테이블, 절대 규칙 5개, AI 엔진, 참조 문서

---

## 5. 현재 문제점 분석

### 5-1. [A] AADS 채팅 프롬프트 — 문제점

| # | 문제 | 영향 | 심각도 |
|---|------|------|--------|
| A1 | **WS_CAPABILITIES가 얇음** (48행) — 실제 구현 상태(라우터 30개, 페이지 43개, 테이블 83개) 미반영 | AI가 코드 구조를 정확히 모름 → Runner 작업 오류율 증가 | 🟡 중 |
| A2 | **의도 분류 12개 vs 실제 16개 불일치** — WS_ROLES에 12개, prompts.py INTENT_CLASSIFICATION에 16개 | 채팅 AI가 live_start/live_stop/stock_info/goal_setup 인텐트를 인식 못함 | 🔴 고 |
| A3 | **가설 엔진(HAV) 미언급** — capabilities에 "HypothesisEngine L1→L2→L3" 1줄만 | CEO가 가설 관련 질문 시 맥락 부족 | 🟡 중 |
| A4 | **실매매 연동 상태 미반영** — live trading, KIS API 연동 상태 없음 | 실매매 관련 CEO 질문에 부정확 응답 가능 | 🟡 중 |

### 5-2. [B] GO100 AI 엔진 프롬프트 — 문제점

| # | 문제 | 영향 | 심각도 |
|---|------|------|--------|
| B1 | **UNDERSTAND가 가설엔진 경유 없이 바로 전략 생성** — 채팅→BaseOrchestrator→DRAFT 직행, L1/L2/HAV 스킵 | 검증 안 된 전략이 바로 카드로 저장됨 | 🔴 고 |
| B2 | **DESIGN 프롬프트가 지나치게 김** (~310행 중 200행이 스펙) — LLM 토큰 낭비 | 비용 증가 + 핵심 지시가 스펙에 묻힘 | 🟡 중 |
| B3 | **REPLY에 실제 종목 데이터 접근 없음** — prompts.py에 DB 조회 가이드 없음 | 할루시네이션 가드만 있고, 실제 데이터 주입 메커니즘 부재 | 🔴 고 |
| B4 | **EVALUATE 임계값 고정** — medium 기준 연15%/MDD15%/승률50%/Sharpe1.0 | 시장 레짐에 따른 동적 기준 조정 없음 | 🟡 중 |
| B5 | **OPTIMIZE 이력이 세션 내에서만 유지** — DB 저장 안 됨 | 같은 전략을 여러 번 최적화해도 이전 시도 모름 | 🟡 중 |

### 5-3. [C] CLAUDE.md — 문제점

| # | 문제 | 영향 | 심각도 |
|---|------|------|--------|
| C1 | **KIS와 GO100 공유 CLAUDE.md** — 202행 중 GO100 전용은 58행뿐 | Runner가 KIS 규칙과 혼동 가능 | 🟡 중 |
| C2 | **API 엔드포인트 목록 없음** | Runner가 API 수정 시 경로 모름 → read_remote_file 낭비 | 🟡 중 |
| C3 | **프론트엔드 컴포넌트 구조 없음** | UI 수정 작업 시 파일 구조 파악에 시간 낭비 | 🟡 중 |

---

## 6. 개선 권장안

### Phase 1: 즉시 반영 가능 (코드 수정 없음) — 우선순위 🔴

| # | 개선 | 대상 파일 | 작업량 |
|---|------|-----------|--------|
| 1-1 | **의도 분류 12개→16개 통일** — WS_ROLES["GO100"]에 실제 16개 인텐트 반영 | system_prompt_v2.py | 5분 |
| 1-2 | **WS_CAPABILITIES 확장** — 실제 라우터 30개, 페이지 43개, 테이블 83개, 실매매 상태, 가설엔진 상세 | system_prompt_v2.py | 15분 |
| 1-3 | **CLAUDE.md GO100 CKP 확장** — API 엔드포인트 목록, 프론트 컴포넌트 구조 추가 | CLAUDE.md (서버211) | 20분 |

### Phase 2: 프롬프트 구조 개선 — 우선순위 🟡

| # | 개선 | 효과 | 작업량 |
|---|------|------|--------|
| 2-1 | **DESIGN 프롬프트 분리** — 스펙(UNIVERSE_FILTER_SPEC 등)을 별도 파일로 분리, 필요 시만 주입 | 토큰 30% 절감 (~200→~140행) | 2시간 |
| 2-2 | **REPLY에 실데이터 주입 파이프라인** — BaseOrchestrator에서 DB 조회 결과를 REPLY context로 전달 | 할루시네이션 근본 차단 | 3시간 |
| 2-3 | **EVALUATE 동적 임계값** — 시장 레짐에 따라 임계값 자동 조정 (상승장→공격적, 하락장→보수적) | 평가 정확도 향상 | 2시간 |

### Phase 3: 아키텍처 개선 — 우선순위 ⚪ (중장기)

| # | 개선 | 효과 | 작업량 |
|---|------|------|--------|
| 3-1 | **채팅→가설엔진 통합** — BaseOrchestrator에서 HAV 검증 단계 추가 (DESIGN 후, BACKTEST 전) | 검증된 전략만 카드 생성 | 1일 |
| 3-2 | **OPTIMIZE 이력 DB 저장** — go100_optimization_history 테이블 추가 | 크로스세션 학습 가능 | 4시간 |
| 3-3 | **프롬프트 버전 관리** — DB에 프롬프트 버전 저장, A/B 테스트 가능 | 프롬프트 품질 측정 가능 | 1일 |

---

## 7. 비용 영향 분석

| 현재 | 개선 후 (Phase 2 완료 시) |
|------|-------------------------|
| DESIGN 1회 ~2,000토큰 입력 | ~1,400토큰 (30% 절감) |
| REPLY 할루시네이션 발생 → 재생성 | 실데이터 기반 → 1회로 충분 |
| EVALUATE 고정 임계값 → FAIL 빈발 → OPTIMIZE 루프 5회 | 동적 임계값 → MARGINAL 줄어 루프 3회 이하 |
| **예상 1회 파이프라인**: ~$0.08 | **예상**: ~$0.05 (37% 절감) |

---

## 8. 결론

**가장 시급한 3가지**:
1. 🔴 **B1**: 채팅→전략 생성 시 가설엔진 검증 스킵 — 품질 가드 없음
2. 🔴 **A2**: 의도 분류 12개 vs 16개 불일치 — 4개 인텐트 라우팅 누락 가능
3. 🔴 **B3**: REPLY에 실제 데이터 주입 없음 — 할루시네이션 근본 원인

**즉시 가능한 것** (Phase 1): 프롬프트 텍스트 수정만으로 A2, A1 해결 가능.  
**중기** (Phase 2): 코드 수정 필요하나 비용 37% 절감 + 품질 향상.  
**장기** (Phase 3): 아키텍처 변경. B1 해결은 여기서.

CEO 승인 시 Phase 1부터 즉시 진행합니다.
