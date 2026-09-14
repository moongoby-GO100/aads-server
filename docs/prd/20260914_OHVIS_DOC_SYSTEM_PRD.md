# OHVIS 문서 체계 — PRD·설계

- 작성 2026-09-14 KST · 배경과 실측은 [기획서](../plans/20260914_OHVIS_DOC_SYSTEM_기획서.md)
- 범위: S1~S5. 각 단계는 독립 배포 가능하고 앞 단계 없이도 되돌릴 수 있다.

## 공통 원칙

- **기존 것을 지우지 않는다.** `/docs` 문서함, `memory_facts`, 파일 트리는 그대로 둔다.
  색인과 검색을 더할 뿐이다.
- **손으로 적는 숫자를 만들지 않는다.** 이 작업이 만드는 문서도 같은 규칙을 지킨다.
- 실패는 조용히 넘긴다. 문서 검색이 안 된다고 채팅이 멈추면 안 된다.

---

## S1. 중복·비문서 제외

### 문제
`/docs` 스캐너(`app/api/project_docs.py: SERVER_CONFIG`)가 워크트리·릴리스 사본·
소스코드·이미지를 전부 긁는다. 4,876건 중 `.md` 는 1,674건이다.

### 요구사항

| ID | 요구 |
|---|---|
| S1-1 | 스캔 제외 경로: `.worktrees/`, `*-releases/`, `go100-*`(클론), `claude-model-release-*`, `*-unified-p0`, `aads-dashboard-unni`, `.venvs`, `node_modules` |
| S1-2 | 색인 대상 확장자: `.md` 만. 이미지·소스·json 은 문서가 아니다 |
| S1-3 | 내용 해시(sha256)로 중복 제거. 같은 내용이면 **정본 1개**만 색인 |
| S1-4 | 정본 선택 규칙: `aads-server` > `aads-docs` > `aads-dashboard` > `go100` 순, 같으면 경로가 짧은 것 |
| S1-5 | 제외는 **색인에서만**. 파일은 지우지 않는다 |

### 수용 기준
- 색인 대상이 **1,185건 / 24.0MB** 근처 (실측 기준값)
- `/docs` 문서함 동작은 그대로 (기존 스캔 결과 계약 유지)

---

## S2. 문서 임베딩 + Auto-RAG 연결

### 문제
채팅은 `memory_facts` 와 채팅 기록만 검색한다(`auto_rag._search_relevant`).
문서 1,185건은 검색 대상이 아니다. `ohvis_wiki_pages` 가 그 자리여야 했는데
2026-09-07 덤프 이후 멈췄고 `slug` 가 전부 `memory-fact-<uuid>` 다.

### 설계

**저장소**: 새 테이블 `doc_chunks`. `ohvis_wiki_pages` 는 건드리지 않는다 —
성격이 다르고(그건 memory_fact 덤프) 섞으면 둘 다 못 믿게 된다.

    doc_chunks
      id            uuid pk
      doc_path      text      정본 경로
      doc_sha256    text      문서 내용 해시 (변경 감지)
      project       text      AADS/GO100/KIS/...
      title         text      첫 H1 또는 파일명
      heading       text      이 청크가 속한 최근 제목
      chunk_index   int
      content       text
      embedding     vector(768)
      mtime         timestamptz
      indexed_at    timestamptz
      UNIQUE (doc_path, chunk_index)

**청크**: 1,400자 / 겹침 200자. 제목 경계를 우선해 자른다. 문서당 최대 60청크
(약 84KB)로 제한한다 — `HANDOVER.md` 1.67MB 한 건이 1,200청크를 만들면
검색 결과를 그 문서가 독점한다.

**증분**: `doc_sha256` 이 같으면 건너뛴다. 바뀐 문서만 지우고 다시 넣는다.

**임베딩**: `chat_embedding_service.embed_texts` 재사용(768차원, 로컬 Ollama →
Gemini 폴백). 배치 20건. 실패는 건너뛰고 다음 주기에 재시도한다.

**검색 연결**: `auto_rag._search_relevant` 에 세 번째 소스로 추가.
`memory_facts` / `chat_messages` / `doc_chunks` 를 병렬 검색하고 유사도로 합쳐
Top-K 를 낸다. 기존 두 소스의 동작은 바꾸지 않는다.

### 요구사항

| ID | 요구 |
|---|---|
| S2-1 | `doc_chunks` 테이블 + `ivfflat`/`hnsw` 코사인 인덱스 |
| S2-2 | 색인 스크립트: 증분, 재실행 안전, 진행 로그 |
| S2-3 | `auto_rag` 가 문서를 검색하고 결과에 **출처 경로**를 포함 |
| S2-4 | 문서 출처는 답변에서 구분 가능해야 한다 (`kind="doc"`) |
| S2-5 | 임베딩·검색 실패는 채팅을 막지 않는다 |
| S2-6 | 주기 색인: 하루 1회 또는 수동 트리거 |

### 수용 기준
- "채팅 응답이 왜 느려졌지" 같은 질문에 감사 보고서가 근거로 잡힌다
- 색인 후 `doc_chunks` 행 수 > 10,000
- Auto-RAG 경로에 회귀 없음 (기존 테스트 통과)

---

## S3. "지금 상태" 한 장

### 문제
`CHAT-SYSTEM-OVERVIEW.md` 의 아키텍처 그림은 손으로 그린 ASCII 이고
2026-06-02 기준이다. `page.tsx (4501L)` 처럼 적힌 숫자가 지금은 12,865줄이다.

### 설계
살아 있는 것에서 뽑는다. 손으로 적는 칸이 없다.

| 절 | 출처 |
|---|---|
| 돌고 있는 서비스 | `docker ps` — 이름·이미지·상태·가동시간 |
| 공개 경로 | nginx 설정의 `location` + 업스트림 |
| API 규모 | 라우터 파일의 `@router.` 개수 |
| 데이터 | 상위 테이블 크기·행 수 |
| 최근 배포 | `deploy_runs` 최근 5건 |
| 오늘 바뀐 것 | 당일 커밋 요약 (저장소별) |

출력은 `docs/generated/NOW.md` 하나. **생성물이므로 손으로 고치지 않는다**는
표시를 머리에 박는다.

### 요구사항

| ID | 요구 |
|---|---|
| S3-1 | 한 번 실행으로 `docs/generated/NOW.md` 갱신 |
| S3-2 | 전부 실측값. 하드코딩된 숫자 0 |
| S3-3 | 비전문가가 읽는다 — 전문용어 옆에 한 줄 설명 |
| S3-4 | 실패한 항목은 "확인 불가"로 적고 나머지를 낸다 (전부 실패시키지 않는다) |

---

## S4. 문서 표류 게이트

### 문제
문서의 숫자가 코드와 갈라져도 아무도 모른다. 오늘 감사에서 7건 확인됐다.

### 설계
코드에서 사실을 뽑아 문서가 주장하는 값과 대조한다.

| 검사 | 대상 |
|---|---|
| 파일 줄 수 | `docs/chat/CHAT-FRONTEND-SPEC.md` 의 `(NNNN줄)` 표기 |
| 엔드포인트 수 | `"30+ endpoints"` 류 표기 vs `grep -c '@router\.'` |
| 타임아웃 상수 | 문서 표의 초 단위 값 vs 코드 리터럴 |

**시행 방식**: 먼저 **경고**로 돌린다. 기존 표류가 이미 7건이라 곧바로 차단하면
무관한 커밋이 전부 막힌다. 기존 항목을 고친 뒤 차단으로 올린다.

### 요구사항

| ID | 요구 |
|---|---|
| S4-1 | `scripts/doc_drift_check.py` — 표류 목록과 실제값 출력 |
| S4-2 | 종료코드: 표류 있으면 1 (CI 에서 쓸 수 있게) |
| S4-3 | 1단계는 pre-commit 경고, 기존 표류 해소 후 차단 |
| S4-4 | 검사 대상 문서를 코드에 하드코딩하지 않고 문서 안 표기에서 찾는다 |

---

## S5. 교훈 체계 일원화

### 문제
`docs/shared-lessons/` 8건은 2026-03-06 정지. `ohvis_wiki_error_book` 12건은
이번 주에도 채워진다. 그런데 `.claude/rules/flow-rules.md` 는 죽은 쪽을 가리킨다.

### 요구사항

| ID | 요구 |
|---|---|
| S5-1 | `shared-lessons` 8건을 `ohvis_wiki_error_book` 으로 이관 (L-001~L-008) |
| S5-2 | 이관 시 `prevention` 과 `fix` 를 구분해 넣는다 (R-ERRBOOK) |
| S5-3 | `flow-rules.md` 의 지시를 `error_book.py match` 로 바꾼다 |
| S5-4 | `docs/shared-lessons/INDEX.md` 는 이관 사실과 새 위치를 적고 남긴다 (삭제 금지) |

---

## 위험과 대응

| 위험 | 대응 |
|---|---|
| 임베딩 1만+ 건 생성 부하 | 배치 20건, 백그라운드, 실패는 다음 주기 |
| 문서 검색이 채팅 답변 품질을 떨어뜨림 | Top-K 는 기존 5 유지. 문서가 더 유사하면 밀어내는 게 맞다 |
| 로컬 임베딩 경로(Ollama/PC Agent) 장애 | 이미 Gemini 폴백으로 동작 중. 색인은 실패분만 재시도 |
| 제외 규칙이 필요한 문서를 빼버림 | 색인에서만 빼고 파일·`/docs` 문서함은 그대로 |
| 표류 게이트가 무관한 커밋을 막음 | 1단계는 경고 |
