# 기존 문서 파일럿 5건 — 정본 가져오기 → 승인 → 연결 RESULT

TASK_ID: AADS-PROJECT-DOCUMENTS-M3-LEGACY-PILOT5-R2-20261002
SUPERSEDES: runner-53cbe5d1

## 결과 요약

- 5건 모두 성공했다. 실패·되돌림 **0건**. 5건 외 연결·가져오기 **없음**.
- 모두 운영 API 로만 썼다(직접 SQL 쓰기 없음). 코드·마이그레이션·배포·재시작 변경 없음. 이 리포트만 신규 파일이다.
- 연결 시각(KST): 92 → 19:17:54, 109 → 19:18:06, 110 → 19:18:15, 74 → 19:18:24, 119 → 19:18:28.

## STEP 0 선행 게이트

| 게이트 | 결과 |
|---|---|
| 1. 운영에 `DELETE .../legacy-links/{goal_document_id}` 노출 | 통과. 단, 아래 "지시서와 다르게 된 점" 1번 참고 — openapi 파일이 아니라 동작 확인 |
| 2-a. legacy_links 0건 | 통과 (시작 시 0건) |
| 2-b. 5건 active·is_latest·파일 존재·비밀값 0 재판정 | 통과. `scripts/project_document_m3_inventory.py --rows` 재실행, 5건 모두 `needs_canonical_import`, `secret_detected=false`, `file_exists=true` |
| 2-c. 운영 컨테이너 파일 = 저장소 파일 | 통과. 5건 모두 sha256 앞 16자 일치 |

## 지시서와 다르게 된 점

1. **openapi.json 으로 확인하지 못했다.** `/api/v1/openapi.json` 은 인증 없이 401, 인증 후 404 였고 `/openapi.json` 은 307 이었다. 대신 존재하지 않는 문서 키로 `DELETE`/`GET legacy-links` 를 호출해, 라우터가 직접 내는 404 `document_not_found`(라우트가 없으면 일반 `Not Found`)를 받아 라우트 존재를 확인했다. 이 호출은 쓰기가 없다.
2. **인증.** 저장소의 기존 QA 스크립트(`scripts/refresh_qa_storage_state.py`)와 같은 방식으로 `aads-server` 컨테이너 안에서 관리자 JWT 를 발급해 썼다. 별도 서비스 계정이 없었다. 따라서 이벤트의 `actor_id` 는 그 QA 관리자 사용자다(러너 전용 계정 아님). 값은 로그·리포트에 남기지 않았다. 파이썬 기본 User-Agent 는 Cloudflare 가 1010 으로 막아서 식별 가능한 UA(`aads-runner-pilot5/1.0`)를 지정했다.
3. **`link_id`.** `project_document_legacy_links` 에는 별도 id 컬럼이 없고 `goal_document_id` 가 키다. 표의 link_id 는 그 값이다.
4. **document_key 선택.** 정본 document_key 로 각 `goal_documents.document_key`(예: `prd:848c81d57565`)를 그대로 썼다. 110 이 기존 `aads-current-authority-context` 헤드와 섞이지 않게 하려는 의도다.

## 5건 결과

호출 순서: 운영 API 로 가져오기(`POST /projects/{P}/documents`, `source_path` 지정, `expected_generation=0`) → 응답 본문 sha256 과 파일 sha256 대조 → `approve` → `POST .../legacy-links` → `GET .../legacy-links` 로 확인.

| goal_documents.id | 프로젝트 | document_key | head_id | revision_id | content_hash (= 파일 sha256 = 본문 sha256 = validated_hash) | link_id |
|---:|---|---|---|---|---|---:|
| 92 | AADS | `prd:e677db2cb301` | 39974168-c7cc-4195-8439-1b991fdbe8ea | 79944e05-9c4a-4bd8-9e31-a81a8d017299 | ac94fa364775d6b18b356a1162339202d519cbcd861b729bbc7d567e20c0d62f | 92 |
| 109 | AADS | `plan:7d9f483b5dba` | 679ce7d2-b686-4669-a96e-618f230500a6 | 6dfc2221-6219-4f75-bc11-47e505dd2b4c | b0a54223627600eb18b549dd18cbf9627e4e8f31fca098f6fb4c7785bef750df | 109 |
| 110 | AADS | `prd:848c81d57565` | 2c4e95fb-e221-426a-93be-9fef83a9235e | 686c817c-c69e-41d3-a89a-0cba301e447b | 8cacc274a62a1108001211109139c32c13dbd1b57e4d09f594d344b011ab1d7f | 110 |
| 74 | AADS | `contract:b4ad294d9869` | bb9e8e7e-06c3-409a-8c63-80dd38a4f621 | ebda53f5-91b9-4517-a3c3-40c76540fb30 | 2651d430221d2540d645469b9b4e66e1fb52335070a6f5779cd820ada9d90df7 | 74 |
| 119 | ACCT | `spec:304b66102a6f` | 7992513d-2358-402d-b1da-636896ae4dcd | c1082af2-a3ee-4222-a0c6-666502dc60ef | 71f34cab5354deaf85de0df53647931ea7feedb5b23525f92e7dc48856fe53fa | 119 |

### 전후 상태

| id | 전 | 후 |
|---:|---|---|
| 92 | head·revision·link 없음 | head generation 2(가져오기 1 + 승인 1), 승인 revision = 위 revision, link 1건 |
| 109 | 동일 | 동일하게 generation 2, 승인됨, link 1건 |
| 110 | 동일 | 동일하게 generation 2, 승인됨, link 1건 |
| 74 | 동일 | 동일하게 generation 2, 승인됨, link 1건 |
| 119 | 동일 | 동일하게 generation 2, 승인됨, link 1건 |

- 이벤트(5개 헤드 합계): `created` 5, `approved` 5, `legacy_linked` 5.
- 전체: `project_document_legacy_links` 0 → 5건, `project_document_heads` 7 → 12.

### 건별 확인 포인트

- **92 — 이전 버전 미연결.** 같은 계보(`prd:e677db2cb301`)의 goal_documents 4행(81 v1.0.0, 85 v1.1.0, 89 v1.2.0 = superseded, 92 v1.3.0 = active)의 행 전체 해시를 전후 비교했다. 81·85·89 는 상태·행 해시 모두 **변경 0건**, 92 도 불변. 연결된 goal_document_id 에 81/85/89 는 없다.
- **110 — 헤드 분리.** 새 head `2c4e95fb…` 는 기존 `aads-current-authority-context` head `1e4290e6…` 와 id 가 다르다. 기존 head 는 generation 1, latest revision `1aed18f5…`, 승인 revision 없음 그대로다. 헤드가 섞이지 않았다.
- **74, 109, 119.** 가져오기 응답이 `idempotent=false`(신규 생성), 연결 응답이 `idempotent=false` 였다.

## 검증 기준 대조

| 기준 | 결과 |
|---|---|
| legacy_links 정확히 5건, 다른 goal_document_id 0건 | 충족. 연결된 id = {74, 92, 109, 110, 119} (되돌린 건 없음) |
| 5건 각각 revision.content_hash == 파일 sha256 | 충족. 가져오기 직후 API 응답의 content_hash·본문 sha256, 연결 후 DB 의 content_hash·validated_hash 모두 파일 sha256 과 일치 |
| 92 이전 버전 3행 상태 변경 0건 | 충족 |
| goal_documents 변경 0건 | 충족. 204행 전체 행 해시를 전후 비교해 동일 |

## 실패·되돌림 내역

없음. 어느 단계도 오류 없이 통과했다. DELETE 호출은 STEP 0 의 존재 확인용 프로브(없는 문서 키) 외에는 하지 않았다.

## 롤백 절차 (1건당 1줄)

각 줄은 해당 건의 연결을 해제한다. `$JWT` 는 관리자 권한 Bearer 값이고 `-H "Authorization: Bearer $JWT"` 를 붙인다. 마지막 연결을 지우면서 head 의 승인을 풀려면 `?archive_head=true` 를 붙인다(해당 head 의 연결이 0건이 되고 승인본이 있을 때 보관 처리).

```
curl -sS -X DELETE -H "Authorization: Bearer $JWT" "https://aads.newtalk.kr/api/v1/projects/AADS/documents/prd:e677db2cb301/legacy-links/92?archive_head=true"
curl -sS -X DELETE -H "Authorization: Bearer $JWT" "https://aads.newtalk.kr/api/v1/projects/AADS/documents/plan:7d9f483b5dba/legacy-links/109?archive_head=true"
curl -sS -X DELETE -H "Authorization: Bearer $JWT" "https://aads.newtalk.kr/api/v1/projects/AADS/documents/prd:848c81d57565/legacy-links/110?archive_head=true"
curl -sS -X DELETE -H "Authorization: Bearer $JWT" "https://aads.newtalk.kr/api/v1/projects/AADS/documents/contract:b4ad294d9869/legacy-links/74?archive_head=true"
curl -sS -X DELETE -H "Authorization: Bearer $JWT" "https://aads.newtalk.kr/api/v1/projects/ACCT/documents/spec:304b66102a6f/legacy-links/119?archive_head=true"
```

- 해제해도 head·revision·이벤트는 append-only 라 남는다. `project_document_goal_links`(head↔goal) 행도 지워지지 않는다(unlink API 설계, `reports/20261002_project_documents_m3_legacy_unlink_api_RESULT.md`).
- 해제 뒤 같은 문서를 다시 연결해도 된다.

## 한계

- 운영 API 쓰기는 이미 반영됐다. 이 리포트의 커밋·푸시와 무관하게 DB 상태가 현재 기준이다.
- 이벤트 `actor_id` 가 QA 관리자 사용자다. 감사 시 "러너가 수행한 작업"으로 직접 식별되지 않는다 — 이 작업의 근거는 이 리포트와 CEO 승인 이력(18:4x, 19:1x KST)이다.
- 5건 외 문서의 판정은 바꾸지 않았다(재판정 결과 `needs_canonical_import` 23건 AADS / 102건 ACCT 등은 그대로).
- 재현: 연결 확인은 `GET /api/v1/projects/{project}/documents/{document_key}/legacy-links`.
