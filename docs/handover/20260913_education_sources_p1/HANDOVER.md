# AI 교육자료 출처 검수·보강 (sources_p1) — HANDOVER

- **작업일**: 2026-09-13 (KST)
- **담당 슬롯**: sources_p1 (12건 중 8건 담당 — 나머지 4건은 sources_p0 소관, 미변경)
- **작업 워크트리**: `/tmp/aads-wt-edu-p1-audit` (격리), 브랜치 `docs/education-sources-p1-audit-20260913`
- **기준 커밋**: `e0aca65c` (main)
- **검수 시각**: 2026-09-13 09:40~10:20 KST
- **푸시 상태**: **미푸시** (카드 지시 "Push only at authorized approval stage" 준수 — 로컬 커밋만)

---

## 0. 착수 전 확인 — 중복 러너 점검

이 카드는 여러 세션에 중복 배포된 정황이 있어, 편집 전 실행 중인 러너를 먼저 확인했습니다.

| 확인 항목 | 결과 |
|---|---|
| 기존 워크트리 `/tmp/aads-wt-edu-p1-1006172` (`docs/education-sources-p1-20260913`) | 8개 파일이 **dirty 상태로 방치**. 03:01:06에 일괄 기록된 뒤 변경 없음(md5 고정), 해당 경로를 cwd/cmdline으로 잡은 프로세스 **0건** → 작업 중단된 잔여물로 판단 |
| 그 워크트리의 작업 내용 | CSS·범례·목차 항목만 삽입된 **미완성 스캐폴드**. 목차가 `#source-audit-20260913`을 가리키지만 **해당 앵커가 8개 파일 어디에도 없어 링크가 깨진 상태** |
| 조치 | 해당 워크트리를 건드리지 않고 **main에서 새 격리 워크트리를 생성**해 작업 |
| 타 세션(`aads-server-a6`, 브랜치 `docs/edu-src-p1-iso1006172`) | 작업 중 "동일 워크트리 충돌" 메시지 수신. 확인 결과 **서로 다른 워크트리·브랜치로 파일 충돌 없음**. 사실관계 정정 회신 완료 (아래 9절) |

---

## 1. 담당 파일 8건

| # | 파일 | 담당 근거 |
|---|------|-----------|
| 1 | `app/static/reports/20260912_rag_vectordb_embedding_education.html` | sources_p1 카드 Owned files |
| 2 | `app/static/reports/20260912_mcp_tool_calling_education.html` | 〃 |
| 3 | `app/static/reports/20260912_finetuning_lora_practice_education.html` | 〃 |
| 4 | `app/static/reports/20260912_llmops_observability_education.html` | 〃 |
| 5 | `app/static/reports/20260912_multimodal_ai_education.html` | 〃 |
| 6 | `app/static/reports/20260912_ai_coding_agent_practice_education.html` | 〃 |
| 7 | `app/static/reports/20260912_ai_product_prd_ax_education.html` | 〃 |
| 8 | `app/static/reports/20260912_ai_cost_token_economics_education.html` | 〃 |
| 9 | `docs/handover/20260913_education_sources_p1/HANDOVER.md` | 〃 (본 문서) |

**소유 외 파일은 일절 변경하지 않았습니다.** 특히 공용 `app/static/reports/index.html`, `tests/`, `app/` 코드, 다른 4건의 교육자료는 미변경입니다.

### 원본 보존과 해시

편집 전 8건의 사본을 `/tmp/edu-p1-baseline/`에 보존하고 SHA256을 기록했습니다(`BASELINE_SHA256.txt`). 초판 원본은 커밋 `e0aca65c` 에 그대로 남아 있어 `git show e0aca65c:<경로>` 로 언제든 복원 가능합니다.

| 파일 | 편집 전 SHA256 (앞 16자) |
|---|---|
| ai_coding_agent_practice | `cf9b8449153bbe4a` |
| ai_cost_token_economics | `b1343047eb5b1bc4` |
| ai_product_prd_ax | `deb22be50d74228a` |
| finetuning_lora_practice | `e7f2296d911e26eb` |
| llmops_observability | `bfb6d2b305366ac1` |
| mcp_tool_calling | `570b64178b442ea1` |
| multimodal_ai | `dc172b643b89238e` |
| rag_vectordb_embedding | `08a24142874b732d` |

---

## 2. STEP 0 조사표 (유지 / 수정 / 신규 / 삭제)

### 2-1. 파일 단위

| 파일 | 구분 | 변경량 | 요지 |
|------|------|--------|------|
| mcp_tool_calling | **수정** | +99/-9 | 개정 이력 누락 교정, Roots·Sampling 미검증 해소, 이름 규칙 규범 강도 보강 |
| ai_cost_token_economics | **수정** | +90/-8 | 단가 15항목 전수 대조, 프리미엄 라인 교정, 토크나이저·캐시 손익분기 신설 |
| finetuning_lora_practice | **수정** | +87/-15 | 논문 8건 제목 전수 대조, 초록 원문 대조, Glossary 원문 인용 |
| llmops_observability | **수정** | +80/-12 | `gen_ai.system` 폐기 교정, semconv 이전 반영, 도구 5종 링크화 |
| ai_coding_agent_practice | **수정** | +77/-12 | 요금 재대조, Copilot Max 누락 보완, Cursor·Copilot 근거 등급 강등 |
| ai_product_prd_ax | **수정** | +76/-10 | 통계 4건 1차 출처 확보, 해석 가드레일 신설, ROI 예시 라벨링 |
| rag_vectordb_embedding | **수정** | +70/-10 | 논문 4건 연결, OpenAI 단가 확정, 벡터DB·평가도구 링크화 |
| multimodal_ai | **수정** | +66/-14 | 모델 라인업 2건 교정, Imagen 4 Deprecated 반영 |
| (8개 파일 공통) | **신규** | — | 인용 CSS + `출처 표기 범례` + `부록. 출처 검수 이력` + 목차 항목 |
| 그 외 4건 교육자료·공용 index.html·`tests/`·`app/` 코드 | **유지** | 0 | 카드 소유 범위 밖 — 미변경 |
| — | **삭제** | **0** | **삭제한 파일·섹션·문단 없음.** 초판 서술은 지우지 않고 교정 근거를 병기하는 방식으로 보존 |

교육 구조(한국어 설명·다이어그램·연습문제·CEO 체크포인트·다음 단계)는 **전 파일에서 원형 유지**했습니다. 섹션 id(`ch1`~`ch16`)는 8개 파일 모두 초판과 동일하게 보존됨을 자동 검증했습니다.

### 2-2. 지시서에 없는 변경 (사유 명시)

카드는 "Validate HTML structure"를 요구하므로, 소유 파일 내부에서 발견한 **초판부터 존재하던 HTML 구조 결함 4건**을 교정했습니다. 타 파일에는 영향이 없습니다.

| # | 파일 | 결함 | 실제 영향 | 조치 |
|---|---|---|---|---|
| 1 | multimodal_ai | alert-danger div가 `</div>` 대신 `</p>`로 닫힘 | **그 뒤 본문 전체가 빨간 경고 박스 안에 렌더링됨** (육안으로 확인되는 실제 깨짐) | `</div>`로 교정 |
| 2 | ai_cost_token_economics | `</strong하며,>` — 닫는 태그 안에 본문이 섞임 | 브라우저가 해당 토큰을 무시해 **"하며," 텍스트가 화면에서 사라짐** | `</strong>하며,`로 교정 |
| 3 | ai_cost_token_economics | 여는 `<p>` 없는 고아 `</p>` | 렌더링 영향은 경미하나 파서 스택 불일치 | 제거 |
| 4 | ai_coding_agent_practice | 여는 `<p>` 없는 고아 `</p>` | 〃 | 제거 |

**이 4건은 모두 초판(`e0aca65c`) 원본에 이미 존재했음을 HTMLParser 스택 검사로 확인**했으며, 본 검수가 유발한 것이 아닙니다.

---

## 3. 내용 단위 교정 — 핵심 항목

| # | 파일 | 대상 | 구분 | 초판 | 검수 후 | 1차 출처 |
|---|---|------|------|------|---------|----------|
| 1 | llmops | `gen_ai.system` | **수정** | 현행 표준 속성으로 표기 | **Deprecated — `gen_ai.provider.name`로 대체**. 유사 이름 `gen_ai.system_instructions`는 별개 속성이므로 단순 치환 금지 | OTel 속성 레지스트리 |
| 2 | llmops | semconv 문서 위치 | **수정** | "도입 시 `opentelemetry.io/.../gen-ai` 재확인" | 해당 URL은 **"이전됨·유지보수 중단" 안내 페이지**. 현행 위치는 `semantic-conventions-genai` 저장소, 상태 **Development**(Stable 아님) | OTel 공식 |
| 3 | llmops | 나머지 5개 속성 | 유지(재검증) | — | `request.model`·`operation.name`·`usage.*`·`response.finish_reasons` 동일 이름 유효 | OTel 공식 |
| 4 | mcp | 스펙 개정 이력 | **수정** | 직전 버전 = 2025-06-18 | **직전 버전은 `2025-11-25`**. 전체 5종(2024-11-05→2025-03-26→2025-06-18→2025-11-25→2026-07-28) | MCP versioning |
| 5 | mcp | Roots·Sampling | **미검증 해소** | "폐지인지 축약인지 미검증" | **삭제 아님 — Deprecated**(SEP-2577, 2026-07-28). 최이른 제거 2027-07-28 이후. Logging·HTTP+SSE 상태도 확정 | MCP deprecated 레지스트리 |
| 6 | mcp | 도구 이름 규칙 | **보강** | "명시합니다" | 원문은 전부 **SHOULD(권고)**. **고유성은 서버 1개 범위**이며 다중 서버 집계 시 충돌 가능, `serverInfo.name`은 구분자로 사용 금지 | MCP server/tools |
| 7 | cost | Fable 5.1 / Mythos 5.1 | **수정** | 둘 다 "제한적 접근", 가용성 미검증 | **limited availability는 Mythos 5.1에만**. Fable 5.1에는 해당 표기 없음 | Anthropic 가격표 |
| 8 | cost | 프리미엄 라인 캐시 | **신규** | 없음 | 캐시 히트가 표준 0.1배가 아닌 **0.025배** — 0.1배 적용 시 캐시 비용 4배 과대계상 | Anthropic 가격표 |
| 9 | cost | 토크나이저 변경 | **신규** | 없음 | **Claude 4.7 이후 모델은 같은 텍스트에 약 30% 더 많은 토큰 생성** → $/MTok 단가만으로 세대 비교 시 신형 비용 과소평가 | Anthropic 가격표 |
| 10 | cost | 캐싱 손익분기 | **신규** | 절감률만 제시 | **5분 캐시는 히트 1회, 1시간 캐시는 히트 2회부터 이득**. 최소 캐시 길이 미만은 오류 없이 조용히 미적용 | Anthropic 캐싱 문서 |
| 11 | cost | Fast Mode | **신규** | 2배 프리미엄만 기재 | **Opus 4.7은 오류 거부, Opus 4.6은 오류 없이 표준 속도·표준 단가 실행** — "켰는데 효과 없음"이 조용히 발생 | Anthropic Fast mode |
| 12 | cost | 단가표 15항목 | 유지(전수 대조) | — | 3개 모델 × 5개 단가 전부 **일치**. 배치 단가·배수 3종도 일치 | Anthropic 가격표 |
| 13 | multimodal | OpenAI 이미지 모델 | **수정** | GPT-Image-1/2 | **GPT-Image-2.5 Sunburst / Flare** | OpenAI 모델 문서 |
| 14 | multimodal | Imagen 4 | **수정** | 현행 라인업으로 표기 | 공식 목록에서 **Deprecated** — 신규 설계 사용 금지 | Gemini 모델 목록 |
| 15 | multimodal | Gemini TTS | **수정** | "Gemini TTS" | 정식 명칭 **Gemini 3.1 Flash TTS** | Gemini 모델 목록 |
| 16 | coding | Copilot 플랜 | **신규(누락 보완)** | Pro/Pro+만 | **Max $100/월**, **Business $19/시트·Enterprise $39/시트** 추가 | GitHub 공식 플랜 |
| 17 | coding | "2026-06-01 과금 전환" | **등급 강등** | 사실로 서술 | 공식 문서에서 **시행일 확인 불가**. 근거가 PCWorld·Business Insider **2차(언론)** 뿐 → 예산 확정 근거 부적합 | GitHub 공식 플랜 |
| 18 | coding | Cursor의 Grok 노출 | **등급 강등** | "프라이싱 최상단 Grok 노출" | 재확인 시점 페이지는 모델명 없이 "Access to frontier models" → **현재 확인 불가**로 표시(초판 시점엔 사실이었을 수 있어 삭제하지 않음) | Cursor 가격 |
| 19 | coding | Cursor 4개 플랜 요금 | 유지(재검증) | — | Hobby 무료·Pro $20·Pro+ $60·Ultra $200 **정확히 일치** | Cursor 가격 |
| 20 | coding | AGENTS.md | 유지(원문 대조) | 60k+·Agentic AI Foundation | 원문 문구로 확인 — *"used by over 60k open-source projects"*, *"stewarded by the Agentic AI Foundation under the Linux Foundation"* | agents.md |
| 21 | prd | 통계 4건 | **1차 출처 확보** | "널리 인용된 수치, 추가 검증 안 함" | MIT NANDA·Gartner(2024-07-29)·RAND(Ryseff 외)·BCG(2024-10) **원문 확보, 수치 전부 일치** | 각 기관 |
| 22 | prd | 해석 가드레일 | **신규** | 없음 | Gartner 30%=**예측**, RAND 80%=**65명 인터뷰 정성연구**, BCG 26%=**CxO 1,000명 자기보고 설문**. MIT 95%는 "P&L 효과 미확인"이지 "작동 안 함"이 아님 | 각 기관 |
| 23 | finetuning | 논문 8건 | **전수 대조** | arXiv ID만 텍스트 표기 | **8건 모두 ID↔제목 일치 확인** 후 링크화, 정식 제목 병기 | arXiv |
| 24 | finetuning | LoRA/QLoRA/DoRA 근거 | 유지(초록 대조) | — | 10,000배·65B/48GB·크기·방향 분해 서술이 **초록 원문과 일치** | arXiv |
| 25 | finetuning | Claude 파인튜닝 미지원 | **원문 인용** | 요약 서술 | 원문 문장 병기: *"The Claude API does not currently offer fine-tuning…"* | Claude Glossary |
| 26 | rag | 임베딩 단가 | **부분 해소** | 대부분 추정치 | **OpenAI 2건 확정**($0.02/$0.13, 초판 값 정확). 나머지 5개사는 **미검증 유지 — 계약 근거 사용 금지** | OpenAI 가격 |
| 27 | rag | 논문 4건 | **신규 연결** | 텍스트 언급만 | RAG(Lewis 2020)·HNSW·M3-Embedding·MTEB **제목 대조 후 링크화** | arXiv |
| 28 | rag | MTEB 순위·차원 수 | **해석 주의 신설** | "MTEB 상위권" | 리더보드는 **수시 갱신되는 스냅샷**. Matryoshka "축소 가능"은 성능이 아니라 **정확도↔비용 트레이드오프** | MTEB 공식 |
| 29 | 전 파일 | URL 이전 | **수정** | `docs.claude.com`, `anthropic.com/pricing`, `platform.openai.com` | 각각 `platform.claude.com`(302), `claude.com/pricing`(301), `developers.openai.com`(301)로 교체 | 실측 |
| 30 | 전 파일 | 내부 실측 구간 | **라벨링** | 외부 인용과 혼재 | AADS 내부 DB·코드 구간은 **외부 대조 불가**로 명시하고, 본 검수에서 재조회하지 않았음을 고지 | — |

---

## 4. 링크 클릭 가능화 (검수 기준 핵심 항목)

**초판 8개 파일의 `href="http…"` 는 전부 0건이었습니다.** 모든 출처가 본문에 평문으로만 적혀 있어 독자가 원문을 대조할 수 없는 상태였습니다.

| 파일 | 초판 | 검수 후 |
|------|------|------|
| rag_vectordb_embedding | 0 | **22** |
| mcp_tool_calling | 0 | **19** |
| ai_cost_token_economics | 0 | **19** |
| finetuning_lora_practice | 0 | **17** |
| llmops_observability | 0 | **17** |
| ai_product_prd_ax | 0 | **11** |
| ai_coding_agent_practice | 0 | **9** |
| multimodal_ai | 0 | **7** |
| **합계** | **0** | **121** |

모든 인용 링크는 `class="src"` + `target="_blank"` + `rel="noopener"` 로 통일했고, sources_p0(4건)에서 확정된 마크업 규약을 그대로 재사용해 **12건 전체의 표기가 동일**하게 보이도록 했습니다.

### 링크 실측 (2026-09-13 09:40~10:20 KST, 서버 68 런타임)

고유 URL **66개 전수 점검**:

| 응답 | 건수 | 비고 |
|---|---|---|
| HTTP 200 | **61** | 정상 |
| HTTP 403 | 3 | Gartner ×2, BCG — **자동화 차단(WAF)**, 브라우저 정상 열람. 본문에 `http-waf` 표기로 명시 |
| HTTP 302 | 1 | Milvus 문서 자기 리다이렉트 — 브라우저 정상. 본문에 명시 |
| HTTP 429 | 1 | GitHub 레이트리밋(본 검수의 반복 요청 탓). **15초 후 재요청 시 200 확인** → 죽은 링크 아님 |

> 초안에서 범례·부록에 "전부 HTTP 200"이라고 적었으나 403/302 사례가 있어 **사실과 달라 8개 파일 전부 문구를 교정**했습니다. 현재 문구는 "61/66이 200이며 일부 기관은 자동화 차단"으로 정확히 기술합니다.

---

## 5. 검증 체크리스트 (카드 요구 항목)

- **구현 목표**: 교육자료 8건의 가변·현재형 주장에 1차 출처 클릭 링크를 부착하고, 근거 없는 버전·가격·성능·프로토콜·제품 주장을 원문 기준으로 교정
- **검증 방법**: ①HTML 구조 자동 검증 스크립트 ②Playwright/Chromium 실제 렌더링(데스크톱 1440·모바일 390) ③URL 66개 HTTP 전수 점검
- **완료 기준**: 8개 파일 전부 구조 검증 통과 + 렌더링 실패 0 + 인용 링크 클릭 가능
- **실패 기준**: 중복 id / 깨진 앵커 / 태그 불균형 / 콘솔 에러 / 가로 오버플로 발생 시 실패

| 검증 항목 | 결과 |
|---|---|
| 고유 id (중복 없음) | ✅ 8/8 통과 |
| 앵커 무결성 (`href="#…"` → 실제 id) | ✅ 8/8 **깨진 앵커 0건** (목차 → `#source-audit-20260913` 실제 스크롤 동작 확인) |
| 태그 균형 (HTMLParser 스택) | ✅ 8/8 통과 — **초판에 있던 결함 4건 교정 후 0건** |
| `</html>` 정상 종료 | ✅ 8/8 |
| 충돌 마커 | ✅ 0건 |
| 초판 섹션 보존 (`ch1`~`chN`) | ✅ 8/8 소실 0건 |
| 범례 / 검수 부록 각 1회 | ✅ 8/8 (중복 삽입 없음) |
| 데스크톱 렌더 (1440×900) | ✅ 8/8 |
| 모바일 렌더 (390×844) | ✅ 8/8 |
| **콘솔 에러 / JS 에러** | ✅ **0건** (16개 뷰포트 조합 전부) |
| **가로 오버플로** | ✅ **0px** — 초판에는 MCP 481px·코딩 39px·파인튜닝 7px 오버플로가 있었고 본 검수 CSS로 **전부 해소** |
| 인용 링크 렌더 | ✅ 121개 전부 `<a href>` + `target="_blank"` 로 렌더 |
| 출처 등급 표기 | ✅ 1차/2차·검증/예시/제안/미검증 태그 렌더 확인 |

### 서비스 영향 확인

| 항목 | 결과 |
|---|---|
| 서비스 재시작 | **미실행** (카드: No deployment/restart/DB change) |
| DB 변경 | **없음** |
| 배포 | **없음** — 정적 문서만 수정, 빌드 산출물 무관 |
| `docker ps` | aads-server 등 **기존 상태 그대로 유지**(본 작업이 건드리지 않음) |

### 브라우저 E2E

카드 절차의 `/e2e/credentials/e2e-login-url/AADS` → `browser_navigate` 경로는 **MCP 브라우저 도구 권한이 승인되지 않아 실행하지 못했습니다.** 다만 카드가 요구하는 검증 목적(렌더링·콘솔 에러·오버플로·캡처)은 **로컬 Playwright/Chromium 헤드리스 실제 렌더링으로 동등 이상 수행**했습니다.

- 대상 문서는 **로그인이 필요 없는 정적 HTML**이라 자동 로그인 단계 자체가 적용 대상이 아닙니다.
- 변경분이 아직 main에 없어 `fb.newtalk.kr` 실주소로는 검증 불가 → 워크트리 사본을 `127.0.0.1:8099`로 서빙해 **실제 브라우저 엔진으로 렌더**했습니다.
- 캡처 산출물: `/tmp/edu-shots/` (8개 문서 부록 섹션 + 범례 + 교정 박스 + 모바일 상단, 총 11장)

---

## 6. 테스트

| 항목 | 결과 |
|---|---|
| 구조 검증 스크립트 (`/tmp/edu_p1_validate.py`) | **8/8 통과, 실패 0** |
| 렌더링 검증 스크립트 (`/tmp/edu_p1_render.py`) | **16개 뷰포트 조합 전부 통과, 실패 0** |
| 기존 단위 테스트 | **미실행 — 사유: 본 작업은 `app/static/reports/*.html` 정적 문서와 `docs/` 문서만 변경했고 Python 코드·테스트를 일절 건드리지 않음.** 커밋 시 pre-commit hook의 단위 테스트 단계가 자동 실행되어 회귀 여부를 검증함 |

---

## 7. 비용

| 항목 | 내용 |
|---|---|
| LLM 호출 | 본 세션 단일 에이전트. 서브에이전트·병렬 워크플로 **미사용** (CEO 규칙: 비용 효율 최우선) |
| 외부 조회 | WebFetch/WebSearch 약 20회 + curl HTTP 점검 약 110회(초기 38 + 최종 66 + 재확인) — **과금 없는 공개 문서 조회** |
| 별도 유료 API | **없음** — 이미지·영상·음성 생성 도구 미사용 |

---

## 8. 사람·법적 검증 리스크 (인계 필수)

> 아래는 **본 검수로 해소되지 않았고, 사람이 판단해야 하는 항목**입니다.

1. **[높음] 가격·요금은 전부 스냅샷입니다.** Anthropic 단가, Cursor·Copilot 요금, OpenAI 임베딩 단가 모두 2026-09-13 KST 기준입니다. **예산 승인·계약 체결 전 반드시 링크 원문 재확인이 필요**하며, 본 문서를 단독 근거로 삼으면 안 됩니다.
2. **[높음] RAG 임베딩 단가 5개사(gemini-embedding-001·voyage·Cohere embed-v4·jina-v4·Solar)는 여전히 미검증 추정치입니다.** 표에 "추정 $0.06~0.18" 형태로 남아 있으므로 **계약 근거로 사용 금지**. 조달 담당자가 각 사 콘솔에서 확정해야 합니다.
3. **[중간] Copilot "2026-06-01 사용량 과금 전환" 시행일의 근거가 언론 보도뿐입니다.** 공식 문서에서 확인되지 않았습니다. 이 날짜를 근거로 예산을 확정하면 안 되며, GitHub 영업/공식 청구 문서로 확인이 필요합니다.
4. **[중간] Gartner·BCG 원문은 자동화 접근이 차단(403)되어 본 검수는 URL 유효성만 확인했습니다.** 수치 자체는 각 기관 공개 요약으로 대조했으나, **보고서 전문을 사람이 열람해 맥락을 확인**하는 편이 안전합니다. 특히 Gartner 30%는 **이미 기간이 지난 예측**이므로 실제 결과와 다를 수 있습니다.
5. **[중간] 산업 통계를 경영 보고에 옮길 때 정의 왜곡 위험이 있습니다.** MIT 95%는 "P&L 효과 미확인"이지 "AI가 작동하지 않는다"가 아닙니다. 부록에 가드레일을 넣었으나 **인용자가 이를 생략할 위험**은 남습니다.
6. **[중간] 저작권·인용 범위.** 본 검수는 공식 문서의 **짧은 문구 인용 + 원문 링크** 방식만 사용했고 보고서 본문을 복제하지 않았습니다. 다만 Gartner·BCG 등 **상용 조사기관 자료를 사내 교육에 재배포**할 때는 라이선스 확인이 필요할 수 있습니다(법무 검토 권장).
7. **[낮음] AADS 내부 실측 구간은 재검증하지 않았습니다.** cost_tracking(2026-03 관측 공백 포함), memory_facts, media_generation_jobs, pipeline_jobs 등의 수치는 초판 조회값 그대로입니다. 본문에 "외부 대조 불가" 라벨을 달았습니다.
8. **[낮음] OTel GenAI 규격은 상태가 Development(Stable 아님)입니다.** `gen_ai.provider.name`도 다시 바뀔 수 있으므로, 계측 표준 확정 전 재확인이 필요합니다. 기존 `gen_ai.system` 계측 대시보드는 **조용히 빈 값이 될 수 있어** dual-write 전환을 권고했습니다.
9. **[낮음] MCP Roots·Sampling은 2027-07-28 이후 제거 대상입니다.** 현재 AADS 연동이 이들을 사용하는지는 **확인하지 못했습니다**. 사용 중이라면 마이그레이션 계획이 필요합니다.

---

## 9. 동시 작업 세션 충돌 (CEO 판단 필요)

작업 중 타 세션(`aads-server-a6`)에서 "동일 카드·동일 워크트리 충돌" 메시지를 받았습니다. 확인 결과:

- **파일 충돌은 없습니다.** 상대는 `/root/aads/.worktrees/edusrc-p1-iso1006172`(브랜치 `docs/edu-src-p1-iso1006172`), 본 세션은 `/tmp/aads-wt-edu-p1-audit`(브랜치 `docs/education-sources-p1-audit-20260913`)로 **서로 다른 워크트리·브랜치**입니다. 상대 워크트리에 쓰기를 수행한 적이 없습니다.
- 상대가 "내 커밋이 당신의 RAG 편집을 쓸어담았다"며 지목한 내용(`voyage-4`, `gemini-embedding-2`, `Upstage Embed 2`)은 **본 세션 작업물이 아닙니다** — 본 세션의 작업본과 편집 전 원본 사본 모두에서 해당 문자열 **0건**입니다. 제3 세션의 작업물로 보입니다.
- 상대가 제안한 분담(본 세션이 rag·multimodal만 담당)은 **수용하지 않았습니다.** 카드가 8건 전부를 본 세션에 배정했고, 이미 6건이 완료 상태여서 검증 완료분을 폐기할 이유가 없기 때문입니다. 사실관계 정정과 함께 회신했습니다.

> **CEO 조치 요청**: 동일 카드가 최소 2~3개 세션에 중복 배포되었습니다. 현재 `docs/edu-src-p1-iso1006172`(상대, 커밋 완료)와 본 브랜치가 **같은 8개 파일을 각각 수정한 상태**입니다. **어느 브랜치를 main에 반영할지 결정이 필요**하며, 양쪽 모두 푸시하지 않은 상태로 대기 중입니다. `app/static/reports`는 **main에서 곧바로 공개**되므로 두 브랜치를 임의로 병합하면 범례·부록이 중복 삽입되어 `id="source-audit-20260913"`가 중복될 위험이 있습니다.

---

## 10. 다음 단계

1. **CEO 승인** — 반영할 브랜치 선택(본 브랜치 권장 근거: 8건 전부 완료 + 렌더링·링크 전수 검증 + 초판 HTML 결함 4건 교정).
2. 승인 후 **푸시** (현재 미푸시).
3. main 반영 시 `app/static/reports`가 즉시 공개되므로, 반영 직후 `fb.newtalk.kr`에서 8건의 부록 앵커와 인용 링크를 육안 확인.
4. 8절 리스크 1·2·3번(가격·임베딩 단가·Copilot 시행일)을 담당자에게 전달해 확정 작업 지시.
