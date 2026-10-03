# 정본 검색 tenant·문서 권한 격리 선행 보강 — 결과

TASK_ID: AADS-DOC-SEARCH-TENANT-GUARD-20261003 · P0-CRITICAL · SIZE S · MODE PUSH_ONLY
승인: CEO 2026-10-03 '보류건 권장안으로 진행해'

## 요약

`GET /api/v1/project-docs/search`·Auto-RAG 의 문서 검색이 이제 **신뢰된 tenant 컨텍스트 + 프로젝트/문서 grant** 를 SQL WHERE 에서
(`ORDER BY … LIMIT` 앞에서) 강제한다. 요청자가 준 tenant·URI 는 쓰지 않는다. 미인증은 401/403, tenant 가 비었거나 맞지 않는 정본 청크는
fail-closed 로 보이지 않는다. 파일 문서 검색과 응답 키 계약(path/name/project/server/title/heading/snippet/similarity/chunks, approved/latest 라벨)은 그대로다.

닫은 유출 경로 3개.
1. `search_docs_semantic` 에 테넌트 의존성이 없었다 → `require_tenant_member` 추가, scope 는 인증된 컨텍스트에서만 만든다.
2. `doc_chunks` 에 tenant 칸이 없어 SQL 이 tenant 를 못 걸렀다 → 마이그레이션으로 3칸 추가, 색인기가 채우고 검색 SQL 이 건다.
3. `scripts/build_kg.py` 가 정본 청크 경로를 KG 로 만들어 모든 tenant 의 Auto-RAG 에 섞였다 → 정본 제외(아래 STEP0).

## STEP0 분류

| 파일 | 분류 | 사유 |
|---|---|---|
| `app/api/project_docs.py` | 수정 | 범위 내. 엔드포인트에 tenant 의존성·scope 전달·batched visible 카운트 |
| `app/services/doc_index.py` | 수정 | 범위 내. `DocSearchScope`, `_visible_sql`(두 임베딩 경로 공통), `visible_chunk_counts`. `canonical_state` 조회는 응답 계약 유지를 위해 제거 |
| `scripts/index_docs.py` | 수정 | 범위 내. 정본 청크에 tenant/head/revision 기록, 칸 없으면 중단, 칸이 빈 옛 행 재작성 |
| `migrations/20261003_doc_chunks_tenant_scope.sql` | 신규 | 범위 내(최소 스키마). nullable 3칸 + 부분 인덱스 2개, FK 없음 |
| `migrations/rollback/…down.sql` | 신규 | 롤백 |
| `app/services/auto_rag.py` | 수정 | **범위 밖.** 이 파일이 `_search_documents` 로 같은 SQL 을 직접 호출하므로 scope 없이 두면 우회로가 된다. scope 는 DB 에 저장된 `chat_sessions(tenant_id,user_id)` 에서만 구하고 elevated=False, scope 가 없으면 빈 결과 |
| `scripts/build_kg.py` | 수정 | **범위 밖.** `collect_doc_mentions` 가 `doc_chunks` 를 tenant 구분 없이 읽어 정본 본문이 KG→Auto-RAG 로 전 tenant 에 노출. `doc_path NOT LIKE 'canonical://%' AND coalesce(label,'')<>'정본'` 두 조건만 추가(4줄) |
| `tests/unit/test_doc_index_canonical.py`, `test_doc_search_authority_metadata.py`, `test_qwen3_doc_index.py`, `test_qwen3_shadow_bounds.py` | 수정 | 시그니처(scope)·색인 칸 변경에 맞춤. 삭제한 테스트 없음 |
| `tests/unit/test_doc_search_tenant_guard.py` | 신규 | fake 전용 |
| `tests/integration/test_doc_search_tenant_guard_postgres.py` | 신규 | 격리 DB 전용 |
| `scripts/migrations_auto_apply_baseline.txt` | 유지 | 새 마이그레이션은 baseline 에 넣지 않는다(넣으면 HOLD 로 자동 적용 안 됨) |

삭제한 파일 없음.

## 가시성 규칙 (SQL, LIMIT 앞)

- **파일 청크**: `tenant_id IS NULL`, 정본 head/revision 없음, `doc_path NOT LIKE 'canonical://%'`, `label IS DISTINCT FROM '정본'` — 기존 동작 유지.
- **정본 청크**: `tenant_id = 요청 tenant`, 정본 경로, head·revision id 모두 not null, head 를 실시간 조인해 승인/최신 revision 이 청크의 revision 과 같고, archived 이벤트가 없고, 그리고 (elevated 이면서 user 식별이 있음) 또는 `project_document_grants` 의 read/write/approve 행.
- elevated = `is_internal_admin` 또는 role admin/owner. user 식별이 없으면 elevated 여도 grant 경로만(= 격리 우선). tenant 가 UUID 가 아니면 `DocScopeError` → 403 `user_identity_required`.
- `scope=None` 은 DB 를 건드리지 않고 빈 결과.
- head 를 실시간 조인하므로 revision 교체·archive·grant 회수가 **재색인 없이 즉시** 반영된다.
- `x-monitor-key` 점검: 전역 미들웨어는 헤더만 있으면 통과시키지만, `get_current_user` 는 `internal-pipeline-call` 을 `/pipeline/` 경로에서만, 그 외엔 `AADS_MONITOR_KEY` 와 일치할 때만 인정한다. `project-docs/search` 는 해당 경로가 아니므로 임의 헤더로는 401 이다(코드 확인, 운영 호출은 하지 않음).

## 실행한 테스트 (두 결과를 분리 보고)

### A. fake 전용 단위 테스트 (DB 없음)
`bash scripts/run_unit_tests.sh <6개 파일>` — **186 passed**, 종료코드 0.
대상: `test_doc_search_tenant_guard.py`(신규 20건), `test_doc_index_canonical.py`, `test_doc_search_authority_metadata.py`, `test_qwen3_doc_index.py`, `test_qwen3_shadow_bounds.py`, `test_tools_and_pipeline.py`.
신규 20건은 SQL 문자열 형태(두 임베딩 경로 모두 tenant 조건이 `LIMIT $2` 앞), scope 없음 → DB 미접촉 빈 결과, Auto-RAG 세션 scope, 미인증 401/403, 엔드포인트가 scope 를 넘기고 응답 키가 불변인지, KG 정본 제외를 본다. **fake 는 SQL 이 실제로 행을 거르는지를 증명하지 못한다.**

### B. 격리 PostgreSQL 통합 테스트 (일회용 DB, 운영 아님)
`tests/integration/test_doc_search_tenant_guard_postgres.py` — **31 passed** (legacy/qwen3 두 임베딩 경로 파라미터화 포함).
방식: 격리 컨테이너 `aads-doc-m1-pg-b749` 의 템플릿 `aads_doc_m1_final` 을 테스트마다 임시 DB 로 복제하고 끝에 삭제. 운영 DB 접속 없음.
검증한 것:
- 마이그레이션 멱등 + 롤백으로 스키마 복원
- 같은 질의로 tenant A 는 자기 승인본·허용된 초안을 보고 tenant B 는 못 봄(양방향), 파일 문서는 공통
- project grant 없음 → 정본 못 봄, grant 는 프로젝트별
- elevated admin 은 자기 tenant 만(다른 tenant 불가), user 식별 없는 elevated 는 elevated 아님
- 소유자 불명/불일치 정본 청크는 fail-closed, revision 교체·grant 회수 즉시 반영
- **LIMIT 앞 필터**: 다른 tenant 청크로 top-k 를 채워도 허용 문서가 나옴
- 파일 검색 회귀 없음, legacy/shadow/qwen3/hybrid 모드, 결과 행 계약, `index_status`·카운트에서 숨김 문서 제외
- 색인기가 쓴 청크의 가시성, 옛 정본 행(tenant 칸 없음) 재색인 복구
- API end-to-end tenant 격리, Auto-RAG 가 저장된 세션 scope 를 사용

### C. 변이 검사 (테스트가 실제로 잡는지)
복사본에서 정본 분기의 `tenant_id = $T` 조건을 무력화(`$T IS NOT NULL`)한 뒤 B 를 다시 돌렸다: **2 failed, 29 passed** —
`test_elevated_admin_sees_all_own_tenant_active_but_never_other_tenant[legacy|qwen3]` 가 tenant B 의 `b-secret` 유출로 실패.
- 해석: tenant 조건이 없어져도 non-elevated 경로는 grant 조건이 단독으로 막아 통과한다(**다층 방어가 의도대로 동작**). tenant 조건 단독 회귀는 elevated 케이스가 잡는다.
- 복사본은 삭제했다. 저장소에는 변이가 남지 않았다.

### D. 정적 검사
`ruff check --select F821,F811`(변경 11개 파일) 통과, `py_compile` 5개 소스 통과.

## 실행하지 않은 것 (사유)
- 운영 DB 조회·마이그레이션 적용·`index-canonical` 실행: 금지(운영 DB 색인·재색인·schema 적용 금지).
- 파일럿 74/92/109/110/119 재실행·재연결, 일괄 정본등록/승인/연결, L1 enabled·enforce 변경: 금지, 하지 않았다.
- 운영 `/api/v1/project-docs/search` HTTP 호출: 운영 접근 금지. 엔드포인트 동작은 격리 DB 위 end-to-end 로만 확인.
- `scripts/dup_guard.py`·pre-commit/pre-push 훅: 커밋하지 않으므로 실행 대상 아님(Runner 승인 단계에서 돈다).
- 전체 단위 테스트 스위트: 변경 영역 6개 파일만 실행. 나머지는 이 변경이 건드리지 않는 영역.
- 잔여 위험(유출 아님): HNSW 인덱스는 후필터라 tenant 필터 뒤 결과가 top_k 보다 적을 수 있다(재현율 문제). 정본 문서가 많아지면 tenant 별 부분 인덱스/`hnsw.iterative_scan` 을 후속 검토.

## 변경 파일 / SHA / URL
- 변경 파일: 위 STEP0 표 (수정 9, 신규 4: 마이그레이션 2, 테스트 2) + 이 보고서 + HANDOVER.md
- SHA: **없음 (미커밋 — Runner 승인 단계에서 commit/push)**
- GitHub: https://github.com/moongoby-GO100/aads-server/blob/main/reports/20261003_doc_search_tenant_guard_RESULT.md (push 이후 열림)
- 비용: $ 미측정

## 운영 후속 (이 세션은 실행하지 않음 — 순서 중요)

**마이그레이션이 코드보다 먼저여야 한다.** 새 코드는 `doc_chunks.tenant_id` 등 3칸을 SELECT/WHERE 에 쓰므로 칸이 없으면 검색이 오류로 실패한다(fail-closed, 유출은 아님).

1. 계획 확인(적용 없음)
   `bash scripts/apply_release_migrations.sh --plan --only migrations/20261003_doc_chunks_tenant_scope.sql`
   → `PENDING`, 파괴적 SQL 게이트 통과(순수 ADD COLUMN/CREATE INDEX).
2. 적용
   `bash scripts/apply_release_migrations.sh --only migrations/20261003_doc_chunks_tenant_scope.sql`
   (또는 `bash scripts/apply_migration.sh migrations/20261003_doc_chunks_tenant_scope.sql`). 릴리스 배포 시 자동 적용 경로가 같은 파일을 집는다.
3. 코드 릴리스는 `/root/aads/AGENTS.md` 계약대로 클린 릴리스 SHA 로, P0/P1 5분 모니터링.
4. 정본 재색인(내용 변경 없이 소유 칸만 채움): 먼저 `python3 scripts/index_docs.py index-canonical --dry-run`, 계획이 맞으면 `python3 scripts/index_docs.py index-canonical`.
   - 재색인 전에는 기존 정본 청크가 **보이지 않는다**(fail-closed). 정본 검색이 비는 것은 정상이며 재색인으로 복구된다.
   - 이 재색인은 CEO/운영 승인 사항이다. 이 작업에서는 하지 않았다.
5. 확인: 서로 다른 tenant 두 계정으로 같은 질의를 보내 상대 tenant 정본이 없는지 본다.

**롤백**: 코드를 이전 릴리스로 먼저 되돌린 뒤 `migrations/rollback/20261003_doc_chunks_tenant_scope.down.sql` 실행
(정본 청크 삭제 → 인덱스 2개·칸 3개 삭제). 파일 청크는 영향 없고 정본 청크는 다음 `index-canonical` 이 다시 만든다.
코드만 되돌리고 칸을 남겨도 안전하다(nullable 추가 칸).

## 결정 사항 / 한계
- 정본 청크는 tenant 칸이 채워진 것만 노출한다(fail-closed). 승인 정본이라도 grant 가 없는 일반 멤버에게는 보이지 않는다 — 기존 "내부 tenant 전원 공개" 에서 의도적으로 좁혔다. 공개 범위를 넓히려면 grant 를 부여하는 별도 결정이 필요하다.
- Auto-RAG 는 elevated=False 로 고정(채팅 세션에서 admin 권한을 추정하지 않음).
- 미결: `index-canonical` 운영 실행 시점·대상 tenant 범위는 CEO 승인 필요(내부 tenant 만 읽는 기존 색인기 동작은 그대로).

## 오류 사전 (R-ERRBOOK)
등록하지 않았다. 이번 건은 새 장애 원인 규명이 아니라 설계 보강이며, 앞선 보고(RECOVER-R2)의 "테넌트 위험" 은 이 변경으로 코드에서 닫혔으나 운영 재현·확인은 하지 않았다 — 추측은 넣지 않는다.

## 자동 재작업 라운드 1/2 — 리뷰 지적 처리 (runner-d8de94d0)

1. **배포 게이트 `deploy_isolated_push_state: stale_base`** — 처리함.
   - 원인: 반려 산출물 runner-e1828cce(커밋 23c35e08)의 부모가 ad3a81d6 인데, origin/main 은 그 뒤로 b6122893·f42d8a08·98ead1c9 세 커밋이 더 있었다. 코드 결함이 아니라 base 가 낡은 것이다.
   - 조치: 최신 origin/main(98ead1c9, 이 워크트리의 HEAD 와 동일) 위에서 산출물 diff 를 재구현 없이 그대로 적용했다. 두 쪽이 모두 건드린 파일은 `HANDOVER.md` 하나뿐이어서 그것만 최신 파일 맨 위에 항목을 수동으로 얹었고, 나머지는 충돌 없이 적용됐다.
   - 의미 충돌 점검: 그 사이 main 에서 바뀐 `canonical_documents.py`·`canonical_document_tools.py`·`tool_registry.py`·`tool_executor.py`·`ceo_chat_tools.py`·`e2e_verify.py` 에서 `doc_index`/`search_docs`/`doc_chunks`/`project_docs` 참조를 grep 했고 0건이다. 전체 `app/`·`scripts/` 에서도 `search_docs*`/`index_status`/`visible_chunk_counts` 호출처는 `project_docs.py`·`auto_rag.py` 뿐이라 우회로가 새로 생기지 않았다.
   - 재검증(최신 base 위): `bash scripts/run_unit_tests.sh <6개 파일>` **186 passed**(exit 0); 격리 PG 통합 `tests/integration/test_doc_search_tenant_guard_postgres.py` **31 passed**(격리 컨테이너 aads-doc-m1-pg-b749 의 템플릿 복제 DB, 운영 DB 접속 없음, `AADS_DOC_GUARD_DB_REQUIRED=1`); `ruff --select F821,F811` 통과; `compileall` 통과; `scripts/dup_guard.py` rc=0.
   - 변경 범위는 직전과 동일(코드 변경 없음, base 만 교정). 코드·단위 통과와 운영 반영은 여전히 분리: 운영 DB 마이그레이션·재색인·배포·브라우저 검증은 하지 않았다.
   - SHA: 없음(미커밋, Runner 승인 단계에서 생성). 비용: $ 미측정.
