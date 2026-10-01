# 검색 근거·현재 권위 계약 v1.0 (2026-10-01)

이 문서는 **지금 코드에 있는 것**만 적는다. 설계 희망이나 추정 승격 규칙은 적지 않는다.
운영 안내는 [현재 정본과 서버 원장 조회 안내](../operations/CURRENT_AUTHORITY_AND_SERVER_LEDGER.md)가,
요구사항은 [PRD v1.0.0](../plans/AADS-CURRENT-AUTHORITY-CONTEXT-PRD-v1.0.0-20261001.md)이 원본이다.
이 문서는 그 둘이 쓰는 **조회·검색 계약의 소스 위치와 필드**를 고정한다.

## 1. 승인 정본(approved pointer) 조회 계약

정본 판정의 유일한 소스는 아래 코드다. 파일 작성일·파일명·`label`·검색 순위는 판정 근거가 아니다.

| 계약 | 현재 소스 | 확인된 동작 |
|---|---|---|
| 단건 승인본 조회 | `app/api/canonical_documents.py:255` `get_document(approved_only=True)` | `revision_id = head["approved_revision_id"]`. 응답에 `authoritative`(= revision이 승인 포인터와 동일한가), `status`(`project_document_events`의 마지막 `review|approved|archived`, 없으면 `draft`/`missing`) |
| 승인 포인터 | `project_document_heads.approved_revision_id` | `approve` 시 `generation+1`과 함께 UPDATE, `unapprove` 시 NULL (`:302`, `:322`) |
| 리비전 사실 | `project_document_revisions` | `revision`, `version`, `title`, `content_hash`, `source_path`, `change_summary`, `author_id`, `created_at` (이력 API `:300` 기준) |
| 세션 문맥용 brief | `app/api/canonical_documents.py:332` `approved_brief()` | 승인 포인터로 INNER JOIN. head/tenant/project 동일성까지 JOIN 조건에 포함. draft는 구조적으로 들어올 수 없다 |
| brief 문자열화 | 같은 파일 `:346` `format_approved_brief()` | 최대 8건·3,000자. 시크릿 정규식 매치 항목은 제외. 비면 `승인된 정본 문서 없음/조회 불가` |
| 채팅 주입 | `app/services/context_builder.py:631` `_build_approved_document_layer()` | 세션 id로 `tenant_id`·`project_key`·`user_id`를 DB에서 확정(표시명 사용 안 함) → grants 확인 → `approved_brief` → 실패 시 fail-closed |
| ACL | `canonical_documents.py:49` `_authorize()` + `project_document_grants` | `read|write|approve` 등급. elevated 아니면 grant 없는 tenant/project는 403 `project_access_denied` |

**계약 공백(미구현, 015 인계).** `approved_brief()`가 돌려주는 필드는
`document_key, kind, title, version, excerpt, content_hash` 여섯 개다 — **`revision`(번호)과
`source_path`는 포함하지 않는다.** 따라서 주입된 brief만으로는 "몇 번째 리비전인가"와
"어느 파일에서 왔나"를 말할 수 없고, 그 둘은 `get_document`/`/history`를 따로 불러야 한다.
이 문서는 그 사실을 공백으로 기록하며, brief 쿼리 변경은 이 작업 범위가 아니다(기존 두 builder 보호).

## 2. 운영 원장 vs 건강 감시 (분리 계약)

| 계약 | 현재 소스 | 값 |
|---|---|---|
| 원장(화면·보고에 노출) | `app/services/server_registry.py:194` `list_ledger_servers()` / `LEDGER_SERVER_IDS`(`:90`) | `CANONICAL_SERVER_IDS` + `jinah244` = **4** |
| 건강 감시 대상 | 같은 파일 `list_servers()` / `CANONICAL_SERVER_IDS` | **3** |
| 상태 API | `app/api/ops.py:3760` `/ops/status` | 상태를 새로 계산하지 않고 `/ops/health-check` 결과와 서킷브레이커를 접어서 반환 |
| 서버 메타 | `app/api/ops.py:3560` `_server_meta_list()` | `LEDGER_SERVER_IDS`를 순회하고 각 항목에 `health_monitored = sid in CANONICAL_SERVER_IDS` 를 붙인다 |

**읽는 규칙.** 원장 등록(구성) 조회시각과 health 관측시각은 다른 값이다.
`health_monitored=false`(현재 `jinah244`)의 `unknown`은 장애 판정이 아니다. 원장 4를 3으로 줄여 보고하지 않는다.

## 3. 검색 row 필수 9종

`app/services/doc_index.py`의 두 검색 경로는 아래 9개를 **반드시** 돌려준다.
하나라도 빠지면 상위 단계에서 살려 보낼 방법이 없다.

`doc_path` · `title` · `heading` · `content` · `similarity` · `doc_sha256` · `mtime` · `indexed_at` · `label`

- qwen3 경로 `search_docs_qwen3()`(`:160`)는 `**dict(r)`로 행을 그대로 펼친다 — SELECT에서 빠지면 끝이다.
- legacy 경로 `search_docs_legacy()`(`:287`)는 컬럼을 화이트리스트로 다시 적으므로 두 곳을 같이 고쳐야 한다.
- 회귀: `tests/unit/test_doc_search_authority_metadata.py` 가 두 경로를 **실제로 호출해** 9종을 확인한다(소스 문자열 검사만으로 판정하지 않는다).

## 4. RAG 최종 문자열 계약

`app/services/auto_rag.py`

| 항목 | 계약 | 소스 |
|---|---|---|
| 날짜 종류 | `파일변경일`·`색인일`을 **각각 이름 붙여** 출력. 서로 대체 금지 | `_doc_evidence_header()` |
| 날짜 검증 | `datetime`/`date`/파싱되는 ISO 문자열만 통과. 그 외는 빈 값 | `_format_doc_stamp()` |
| unknown 표기 | `mtime` 없으면 `작성일 미상(파일변경일 없음)`, `indexed_at` 없으면 `색인일 미상`. 추정값으로 메우지 않는다 | 같음 |
| 승인·현재성 | `authority_status="승인미확인"`, `currency_status="현재상태미검증"` **고정**. 날짜·`label`로 승인을 추론하지 않는다 | `_search_documents()` |
| 출처 보존 | 머리말에 전체 경로와 `hash <앞 12자>` 를 넣는다. 값이 없으면 그 칸을 **생략**한다(빈 값 출력 금지) | `_doc_evidence_header()` |
| 예산 초과 | 본문만 줄이고 머리말은 유지. 머리말조차 못 담으면 그 줄을 버린다 | `_fit_doc_line()` |

## 5. 역사 명시 파일 매핑 (2026-10-01 실측)

`historical` 로 표시할 자격은 **커밋 출처와 색인 hash가 함께 맞을 때만** 생긴다. 파일명 일치는 근거가 아니다.

| 파일 | 색인 `doc_sha256` | 커밋 출처 | 적격 |
|---|---|---|---|
| `aads-docs/reports/GO100-SERVER-MIGRATION-RECORD-20260619.md` | `5aa8ce2a…421f` | `b915d42`(aads-docs) — 작업본 hash 일치 | ✅ |
| `aads-server/docs/SYSTEM_PROMPT_ARCHITECTURE.md` | `4f5d1505…2d77` | `origin/main`(`073e17d5` 시점) 내용과 일치 | ✅ 현재 머리표시·currentguide 매핑 |
| `aads-server/docs/ARCHITECTURE-INDEX.md` | `4abb85eb…8896` | 동일 | ✅ |
| `_remote_docs/contabo14/kis-autotrade-v4/docs/AADS-3SERVER-OPERATING-TOPOLOGY-20260623.md` | `93c9a8d1…dab3` | `954ef9446`(contabo14 `kis-autotrade-v4`) | ✅ 단, 아래 동명 파일과 **다른 내용** |
| `/root/aads/go100/docs/AADS-3SERVER-OPERATING-TOPOLOGY-20260623.md` | `7b98fdfe…1b28` | **없음 — git 미추적(`??`)** | ❌ 커밋 출처 없음 |

- 같은 파일명으로 **두 개의 다른 내용**(`93c9a8d1…` / `7b98fdfe…`)이 색인돼 있다. 매핑은 파일명이 아니라 `doc_path`+`doc_sha256` 쌍으로만 한다.
- 지시에 적힌 `doc14bf770` 은 `aads-docs`·contabo14 `kis-autotrade-v4` 양쪽에서 **해석되지 않았다**(`unknown revision`). 해당 리비전과 위 표의 연결은 **미검증**으로 남긴다.

## 6. 금지

1. 날짜(파일 mtime·색인일)나 `label`로 승인 상태를 추론하지 않는다.
2. 최신 draft를 승인 정본으로 승격하지 않는다. 승격은 `approve` API + grant 검증 경로만이다.
3. 권한 검증 없는 추정 반자동 승격 기능을 추가하지 않는다.
4. 파일명만 같은 문서를 같은 자료로 묶지 않는다.
5. 값이 없으면 `unknown`(미상)으로 출력한다. 추정값으로 메우지 않는다.

## 7. 회귀 실행

```
bash scripts/run_unit_tests.sh tests/unit/test_doc_search_authority_metadata.py
bash scripts/run_unit_tests.sh tests/unit/test_canonical_documents.py tests/unit/test_project_document_m2.py
```

## 8. 미검증·후속 (015 통합 릴리스 인계)

- `approved_brief()`의 `revision`/`source_path` 공백(§1) — 계약 확장 여부는 015 판단.
- `doc14bf770` 리비전 해석(§5) — 명시 manifest가 필요하면 그때 기록한다. 지금은 추정 매핑을 만들지 않는다.
- 이 문서는 파일 저장과 커밋까지다. 문서 API 승인(`approved_revision_id` 변경)과 배포 반영은 별도 확인 대상이다.

## 9. 2026-10-01 16:05 KST 실측 갱신 (§2·§5 보강·정정)

이 절은 위 절을 **대체하지 않고 보강**한다. 날짜가 적힌 사실은 그 시각의 실측이며
재색인·배포로 바뀐다. 그래서 적은 시각을 같이 남긴다.

**계약 문서는 이 파일 1벌이다.** 같은 날 별도로 작성된
`AADS-015-CURRENT-AUTHORITY-SOURCE-CONTRACT-20261001.md`(로컬 커밋 `95f3baa3`)는 범위가
이 문서와 겹쳐 **푸시하지 않고 폐기**했다. 규칙 문서를 두 벌로 두면 한쪽이 반드시 낡는다(R-RELEASE).

### 9-1. 원장 소비 지점 두 곳 추가 (§2 보강)

| 소비 지점 | 현재 소스 | 확인된 동작 |
|---|---|---|
| 배포 화면 서버 그룹 | `app/api/admin.py:86` `_deploy_server_groups()` → `:95` `list_ledger_servers()` | 원장 4대를 그대로 순회 |
| 채팅 문맥 주입 | `app/services/context_builder.py:218` `build_server_ledger_section()` | **60초 레이어 캐시에 넣지 않는다**(`:55` 주석, `_get_cached_or_build` 미경유). 세션 단위 보관소 `_LEDGER_PROVENANCE_BY_SESSION`(`:83`, 상한 초과 시 오래된 8건 축출)을 거쳐 provenance 에 남긴다 |

운영 provenance 실측(16:05 KST 직전 30분 11건, 전건 동일 9키):
`source` · `server_ids` · `available` · `ledger_count` · `health_monitored_count` ·
`observed_at` · `registry_read_at` · `registry_file_mtime` · `snapshot_hash`

`registry_read_at` = `observed_at`(원장 등록 조회시각)이다. `registry_file_mtime` 은 파일
변경일이며 **조회시각도 health 관측시각도 아니다** — §2 분리 규칙의 코드 측 근거다.

### 9-2. §5 역사 매핑 — 재색인으로 바뀐 부분 (정정)

`AADS-3SERVER-OPERATING-TOPOLOGY-20260623.md` 동명 파일의 현재 색인은 **2행뿐이다**
(`doc_chunks` 조회, 16:05 KST).

| doc_path | 현재 `doc_sha256`(앞 16) | 최종 색인(KST) | 판정 |
|---|---|---|---|
| `/root/aads/go100/docs/…` | `7b98fdfe9ae7a7e0` | 2026-10-01 15:13 | ❌ 커밋 출처 없음. go100 저장소에서 여전히 `??`(미추적) — §5 판정 유지 |
| `/root/aads/_remote_docs/cafe24_114/newtalk-v2/docs/operations/…` | `c1f0f25a9f72219a` | 2026-10-01 15:52 | 재동기(파일 mtime 10-01 08:51) 후 재색인. **§5 에 적힌 옛 판 `93c9a8d1…` 은 더 이상 이 경로의 색인 hash 가 아니다** |

- §5 의 `_remote_docs/contabo14/kis-autotrade-v4/…` 행은 지금 `doc_chunks` 에 **없다**
  (동명 파일 조회 2행에 포함되지 않음). 색인에서 빠졌으므로 그 행의 적격 판정은 적용 대상이 없다.
- 그래도 §5 의 규칙 자체는 유지된다: 매핑은 파일명이 아니라 `doc_path` + `doc_sha256` 쌍으로만 한다.

### 9-3. 운영 반영 상태 (§8 세 번째 항목 해소)

`origin/main` 의 §2·§4 코드는 **운영에 반영됐다**(16:05 KST 실측).

- 라이브 슬롯 8100 = `aads-server:f4b0866d64fe` healthy, nginx 기준 8102 는 `backup`
- 컨테이너 내부 `build_server_ledger_section` 2건 · `_doc_evidence_header` 4건 (배포 전 0건)
- `compiled_prompt_provenance` 최근 전건에 `server_ledger` 존재 (배포 전 0/3)
- standby(8102) 동기화는 `deploy_runs 5462`(release `78f297e5a3d1`) 진행 중

### 9-4. 남은 미완료

| # | 항목 | 상태 |
|---|---|---|
| 1 | `approved_brief()` 의 `revision`·`source_path` 반환 | 미구현 — §1 공백 유지 |
| 2 | go100 사본의 커밋 출처 확보 | 미조치 — 미추적 유지 |
| 3 | `doc14bf770` 리비전 해석 | 미검증 — 추정 매핑을 만들지 않는다 |

### 9-5. go100 사본 커밋 출처 확보 — §5·§9-2 의 ❌ 판정 해소 (16:20 KST)

`AADS-3SERVER-OPERATING-TOPOLOGY-20260623.md` 의 historical 머리표시를 contabo14
`kis-autotrade-v4` 에 커밋했다. 이로써 §5 가 요구한 **커밋 출처 + hash 일치** 두 조건이 함께 성립한다.

| 항목 | 값 |
|---|---|
| 커밋 | `85ccc3911` (contabo14 `/root/kis-autotrade-v4`, 1파일 +45줄, Deploy Safety hook 통과) |
| 커밋된 blob | `6015aa7bc0fa56eb34946379da1ebfbfef867f3f` |
| 커밋된 파일 sha256 | `7b98fdfe9ae7a7e0629946e15b440045cba9a44c066373654f7ff7f3b501b28d` |
| 색인 `doc_sha256` (`/root/aads/go100/docs/…`) | `7b98fdfe9ae7a7e0629946e15b440045cba9a44c066373654f7ff7f3b501b28d` — **64자 전체 일치** |

따라서 §5·§9-2 의 `/root/aads/go100/docs/…` 행은 "❌ 커밋 출처 없음" 에서
**✅ historical 태그 적격**으로 바뀐다. 추정이 아니라 전체 hash 일치로 판정했다.

머리표시 본문의 주장은 커밋 전에 소스로 검증했다: `server_registry.py:89` `CANONICAL_SERVER_IDS`(3)
· `:90` `LEDGER_SERVER_IDS`(4) · `:75` jinah244 `http_health_urls: []` · `ops.py:3804` `@router.get("/ops/status")`
존재 / `"/servers"` 0건 · `project_document_heads.approved_revision_id` 여전히 NULL(draft 유지).

**남은 제약.** 이 커밋은 contabo14 **로컬에만** 있다. 해당 저장소 `main` 은 origin 대비
`ahead 16 / behind 81` 이라 push 하려면 무관한 16커밋 rebase 가 선행돼야 하고, rebase 는
pre-commit 서명을 무효화한다(R-PUSH). 그래서 이번 범위에서는 push 하지 않았다 —
출처는 성립하지만 **원격 미반영**이므로 그 서버가 사라지면 출처도 사라진다.
