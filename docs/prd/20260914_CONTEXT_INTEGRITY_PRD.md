# 컨텍스트 무결성 — PRD·설계

- 작성 2026-09-14 KST · 배경은 [기획서](../plans/20260914_CONTEXT_INTEGRITY_기획서.md)
- 범위: 임베딩 저장·검색 규격 + 프롬프트 조립 경로. 화면 변경 없음.
- 커밋: `bae08907` `a1ab0c94` `e1d62fd4` `05ddf909` `b657727d`

## A. 임베딩 세대

### A-1. 스키마

    ALTER TABLE chat_messages ADD COLUMN embedding_ver smallint;
    ALTER TABLE memory_facts  ADD COLUMN embedding_ver smallint;

    CREATE INDEX idx_chat_messages_embed_ver ON chat_messages (session_id)
        WHERE embedding_ver IS DISTINCT FROM 2;

| 값 | 뜻 |
|---|---|
| `NULL` | 세대 미상 (2026-09-14 이전). **신뢰하지 않는다** |
| `1` | nomic-embed-text, 접두어 없음 |
| `2` | nomic-embed-text, `search_document:` / `search_query:` 접두어 |

마이그레이션: `migrations/20260914_embedding_version.sql`

### A-2. 저장 규격

    CHAT_DOC_PREFIX = "search_document: "
    CHAT_QUERY_PREFIX = "search_query: "
    CHAT_EMBED_VER = 2

저장 본문은 **세션 제목을 앞에 붙인다.**

    search_document: [파동엔진 관리자] assistant
    (본문 2,000자까지)

본문만 넣으면 "어느 담당의 말인지" 가 벡터에 안 들어간다. 담당이 8명으로
늘어난 지금 크로스 세션 검색에서 엉뚱한 쪽이 잡힌다.

**저장은 `embed_texts_strict()` 로만 한다.** 진짜 경로가 없으면 예외를
던진다. 더미를 저장하면 나중에 진짜와 구분할 방법이 없다.

    embed_and_store_message()   → strict, 접두어, embedding_ver=2
    backfill_embeddings()       → strict, 접두어, embedding_ver=2

### A-3. 검색 규격

질문 벡터는 **두 벌**이다. `build_auto_rag_context` 에서 한 번씩 만들어
아래로 넘긴다. 각자 만들면 CPU Ollama 에 같은 문장을 두 번 태운다.

| 벡터 | 접두어 | 쓰는 곳 |
|---|---|---|
| `query_emb` | 없음 | `memory_facts` (저장된 쪽이 아직 옛 세대) |
| `ask_emb` | `search_query: ` | `doc_chunks`, 세대 2 `chat_messages`, 재질문 판정 |

**세대가 다른 벡터끼리 비교하지 않는다.** 유사도 숫자는 나오는데 뜻이 없다.

크로스 세션 검색 3경로(오케스트레이터 / 프로젝트 / 워크스페이스) **전부**에
`AND m.embedding_ver = 2` 를 건다. 하나만 빠져도 그 경로가 더미를 읽는다.
재질문 판정(`_detect_reask`)도 같다.

`ask_emb` 가 없으면 검색을 **건너뛴다.** 접두어 없는 벡터로 찾느니 안 찾는
편이 낫다.

### A-4. 백필 — `scripts/backfill_chat_embeddings.py`

    backfill_chat_embeddings.py status
    backfill_chat_embeddings.py run --roster        #310 담당 8명
    backfill_chat_embeddings.py run --workspace GO100
    backfill_chat_embeddings.py run --session <id>
    backfill_chat_embeddings.py run --all           34시간

규약 넷.

1. **범위를 안 주면 거절한다.** 전체는 33,000건 = 34시간이다. 끝날 시점을
   모르는 작업을 띄우지 않는다(R-BG).
2. **시간 상한이 기본값으로 있다** (`--max-seconds`, 기본 7200).
3. **중단해도 안전하다.** 채운 것만 `embedding_ver=2` 로 남고 다음 실행이
   이어간다.
4. **실패하면 None 을 돌려주고 그 회차를 건너뛴다.** 한 회차도 못 채우면
   중단한다 — 임베딩 경로가 죽었다는 뜻이므로 계속 돌면 DB 만 두드린다.

접속은 psql 경유다 — 원격 서버에서 앱 패키지 없이 PGHOST 터널로 같은 DB 를
본다. `error_book.py` · `index_docs.py` 와 같은 규약. **서버별 사본 금지.**

### A-5. 대상 규모 (2026-09-14 실측)

| 담당 | 대상 |
|---|---:|
| 파동엔진 | 739 |
| 전략카드(주도) | 693 |
| 데이터엔진 | 563 |
| 실매매엔진 | 301 |
| 학습모델 | 82 |
| 백테스트엔진 | 79 |
| 운영인프라 | 65 |
| 발굴/선정 | 27 |
| **합계** | **2,546** |

처리량 실측(8코어·GPU 없음, 동시 3): 채팅 메시지 1,500~2,800건/시.
문서 조각(1,600자)은 963건/시.

## B. 프롬프트 조립

### B-1. 템플릿 렌더

`LAYER4_SELF_AWARENESS_TEMPLATE` 은 `str.format()` 으로 렌더된다.
본문에 중괄호를 글자로 쓰려면 `{{ }}` 로 escape 한다.

치환 필드는 진화 통계 **5개뿐**이다: `fact_count` `obs_count`
`avg_quality` `quality_count` `error_pattern_count`.

필드 목록은 눈으로 세지 않고 `string.Formatter().parse()` 로 확인한다 —
정규식으로 세면 escape 된 `{{...}}` 안쪽을 필드로 잘못 읽는다.

### B-2. 컴파일러에 넘기는 값

`PromptCompiler.compile()` 은 **정규화하지 않는다.** 받은 문자열을 그대로
`= ANY(scope)` 로 비교한다. 그러므로 호출자가 맞춰서 넘겨야 한다.

| 인자 | 넘길 것 | 넘기면 안 되는 것 |
|---|---|---|
| `workspace_name` | 정규화된 프로젝트 키 `GO100` | 표시명 `[GO100] 백억이` |
| `role` | 세션의 `role_key` | 빈 문자열 |

실측 (주도 세션, 운영 이미지):

    ws='[GO100] 백억이'  12,656자  팀 명단 없음
    ws='GO100'          15,459자  팀 명단 있음

    build_messages_context  고치기 전 34,109자  목표 문구·팀 명단 없음
                            고친 후  36,926자  둘 다 있음

`build_messages_context` 는 맨 위에서 `ws_key = _normalize_workspace(...)` 를
이미 계산한다. 그것을 넘긴다. `role` 은 `chat_sessions.role_key` 를 조회해
넘긴다 — `db_conn` 이 있으면 그것을 쓰고 없으면 풀에서 한 번 조회한다.
조회가 실패해도 예전 동작(역할 없음)으로 떨어질 뿐 조립을 막지 않는다.

### B-3. 0건 경고

역할을 줬는데 자산이 한 건도 안 걸리면 경고를 남긴다.

    logger.warning("prompt_assets_none_selected",
                   workspace=..., role=..., intent=...,
                   hint="workspace_scope/role_scope 와 넘긴 값이 어긋났을 수 있다")

같은 함정에 하루 두 번 빠졌다. 세 번째가 없으리란 보장이 없다.

### B-4. 조용한 fallback 에 로그

`build_messages_context` 가 실패하면 호출부는 기본 프롬프트 + 최근 20개
메시지로 떨어진다. 이 경로가 **세 자리**에 있는데 한 자리(이어쓰기)는 로그를
남기지 않았다. 그래서 3시간 동안 아무도 몰랐다. 세 자리 모두 로그를 남긴다.

## C. 회귀 테스트

`tests/unit/test_doc_index_pipeline.py`

| 테스트 | 무엇을 막나 |
|---|---|
| `test_prompt_layer_templates_render` | 템플릿 escape 누락 — 렌더를 실제로 돌린다 |
| `test_chat_message_search_only_reads_current_generation` | 세대 조건이 3경로 중 하나라도 빠지는 것 |
| `test_index_and_query_prefixes_stay_paired` | 저장/질문 접두어가 한쪽만 바뀌는 것 |
| `test_query_prefix_applied_in_one_place` | 질문 벡터를 두 번 만드는 것 |
| `test_context_builder_passes_session_role_to_compiler` | `role=""` · 표시명 전달 |
| `test_prompt_compiler_warns_when_role_selects_nothing` | 0건 경고 제거 |

`tests/unit/test_memory_context_regression.py`

| 테스트 | 무엇을 막나 |
|---|---|
| `test_backfill_chat_embeddings_requires_explicit_scope` | 범위 없이 34시간짜리 작업 기동 |
| `test_backfill_chat_embeddings_never_writes_dummy` | 더미 저장 · 세대 미기록 |

## D. 합격 기준

1. `build_layer4()` 가 예외 없이 렌더된다.
2. 주도 세션의 `build_messages_context` 결과에 목표 문구와 팀 명단이 있다.
3. `chat_messages` 에 새로 저장되는 벡터는 전부 `embedding_ver=2` 다.
4. 담당 8명 2,546건의 백필이 끝난다.
5. 크로스 세션 검색이 질문과 **주제가 맞는 담당**의 메시지를 상위로 올린다.
6. 운영 로그에 `context_builder failed` 와 `prompt_assets_none_selected`
   가 나오지 않는다.

## E. 배포

| # | 내용 | 상태 |
|---|---|---|
| 454 | 릴레이 / `ask_session` (`cc2a1937`) | 성공 |
| 457 | 임베딩 세대 + 템플릿 escape (`404fbde1`) | 성공 |
| 460 | 역할 키 + 워크스페이스 키 (`b657727d`) | 진행 |

배포 중 백필은 멈춘다 — 이미지 빌드와 Ollama 가 같은 8코어를 다툰다.

## F. 남은 것

- `memory_facts` 75,153건 재임베딩 (약 50시간). 별건 계획.
- 문서 조각 임베딩 4,616/7,847 — 채팅 백필 다음 순서.
- `memory_facts` 가 세대 2로 넘어가면 `query_emb`(접두어 없는 벡터)를
  없앨 수 있다. 그러면 질문당 임베딩 호출이 2회에서 1회로 준다.
