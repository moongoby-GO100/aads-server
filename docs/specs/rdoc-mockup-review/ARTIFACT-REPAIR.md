# R-DOC 기획·설계·PRD 아티팩트 누락 복구 기록

- 작성: 2026-10-03 KST · TASK_ID AADS-RDOC-ARTIFACT-OPEN-REPAIR-20261003 · runner-c8b74f6e
- 대상 세션: `8bf0405a-1f22-4ad9-bb09-6e0fce8c6339` (워크스페이스 `48cb8821-…` "[CEO] 통합지시", tenant `2d701a8c-…`)
- 비용: $ 미측정(외부 LLM 호출 없음).

## 1. 결론 — 등록 성공과 화면 성공을 구분한다

| 구분 | 판정 | 근거 |
|---|---|---|
| 문서 전문 3종 등록 | **성공** | `chat_artifacts` 에 3건 삽입, 본문 sha256 == 정본 sha256 (§3) |
| 테넌트 접근 | **성공** | 서비스 계층 `get_artifact`/`list_artifacts` 가 세션 tenant 로 3건을 돌려주고, 다른 tenant 로는 `None` (§5) |
| HTML 목업 데이터 경로 | **성공** | 목록 12번째, 본문 sha256 == 메타데이터 sha256 (§5) |
| HTML 렌더 | **재현 성공(운영 E2E 아님)** | 실제 패널과 같은 이중 래퍼를 로컬 Chromium 에서 렌더, CSP 오류 0건 (§6) |
| **실제 /chat 화면 열람** | **미완료 (blocked)** | 로그인이 필요하고 사용 가능한 인증 경로가 없었다 (§7). 화면 캡처 없음 |

"사용자 화면에서 열린다" 는 **아직 확인되지 않았다.** 등록 성공만으로 화면 성공을 주장하지 않는다.

## 2. 원인 (확인한 것만)

- 이 세션에는 기획/설계/PRD 제목의 전문 레코드가 없었다. 세션 아티팩트는 812건이었고, 2026-10-03 03:00 KST 이후 112건을 제목·타입으로 전수 확인했을 때 R-DOC 문서 본문은 없고 보고·작업알림·표·HTML 목업만 있었다. 해당 `document_key` 메타데이터를 가진 레코드도 0건이었다. 이전 러너(runner-21d2ffb5, commit 5c88c56b)는 정본(`project_document_revisions`)과 파일만 저장하고 `chat_artifacts` 등록을 하지 않았다. 오류 사전 `chat.document_body_not_registered` 와 같은 원인이다.
- 정본 v2.0.0 은 모두 `source_kind=api` 이다(저장소 파일이 아니라 API 로 올린 revision). 승인본(`approved_revision_id`)은 3건 모두 비어 있다 → 제목에 "미승인" 을 붙인 근거.

## 3. 등록한 문서 (정본 → 아티팩트)

정본은 AADS head 의 `latest_revision_id`. 본문은 DB 안에서 정본 `content` 를 그대로 복사했다(요약·변형 없음). 삽입 전 git `5c88c56b` 의 파일 해시와 정본 해시도 일치함을 확인했다.

| 탭에 보이는 제목 | document_key | revision / version | sha256 | 크기 | 아티팩트 ID |
|---|---|---|---|---|---|
| `[기획] R-DOC 목업 필수 제출·승인 — 기획 v2.0.0 (미승인)` | rdoc-mockup-review-plan | 3 / 2.0.0 | `4d56465d584125dca92b4ea0abbc7b5fe7e0f35a1602aeed351fa6f7b80cf234` | 6319자 · 11682B | `68a898e7-a80d-5551-a110-022411bea57b` |
| `[설계] R-DOC 목업 필수 제출·승인 — 설계 명세 v2.0.0 (미승인)` | rdoc-mockup-review-spec | 2 / 2.0.0 | `6c1d251bf6c299b75280f2a6844c60b340f9ce8a3aea531c0808c1acbeae0462` | 10212자 · 17219B | `37fb8077-0ca3-53cc-a912-9c83372b9970` |
| `[PRD] R-DOC 목업 필수 제출·승인 — PRD v2.0.0 (미승인)` | rdoc-mockup-review-prd | 2 / 2.0.0 | `07c8fd9f350e2b8024e9b7f06175a096fb3f03eaab7f28ee0a27d16b4b767892` | 6850자 · 12555B | `4c16be6b-538f-5082-b124-bf8627cbd1a9` |

- 저장소 원본: `docs/plans/20261003_AADS_RDOC_MOCKUP_REVIEW_PLAN.md`, `docs/specs/rdoc-mockup-review/spec.md`, `docs/prd/20261003_AADS_RDOC_MOCKUP_REVIEW_PRD.md` @ `5c88c56b260b9a972ac0c29a6d0df3c40ef92a29`.
- 레코드: `type=report`, `workspace_id`/`tenant_id` 는 세션 값. 메타데이터에 `status=draft`, `approval=unapproved`, `subtype`, `document_key`, `document_id`, `revision_id`, `revision`, `version`, `content_sha256`, `canonical_source`, `source_path`, `source_commit`, `registered_by` 를 담았다.
- 선례(27e63320…, 같은 오류의 이전 복구)와 같은 형식이다. 패널은 이 메타데이터 필드를 읽지 않으므로 화면 동작에 영향이 없다.
- 등록 방식: 생성 API(`POST /chat/artifacts`)가 없어 `register_doc_artifacts.py apply` 의 단일 트랜잭션 INSERT 를 썼다. 세션 tenant == 정본 tenant, 최신 revision·version·해시 일치, 미승인 조건을 SQL 에서 다시 검사하고, 같은 `document_key`+`revision_id` 가 세션에 있으면 삽입하지 않으며, 트랜잭션 끝에서 3건의 본문 해시를 재검증해 어긋나면 롤백한다.
- 멱등: ID 는 `uuid5(세션:document_key:revision_id)` 로 결정된다. `apply` 를 두 번 실행해 세션의 등록 건수가 3건에서 늘지 않음을 확인했다.
- 기존 문서·목업·승인 이력은 건드리지 않았다. `chat_artifacts` 에 INSERT 만 했고(UPDATE/DELETE 없음), `project_document_*`·승인 이력 변경 없음. 목업 `ba75b010…` 의 sha256 은 등록 전후 동일.

## 4. CEO 가 찾을 위치 — 탭마다 데이터 소스가 다르다

소스는 `ChatArtifactPanel.tsx` · `app/chat/page.tsx` · `directiveArtifacts.ts` 를 읽어 확인했다(읽기 전용).

| 탭 | 데이터 소스 | 여기에 보이는 것 |
|---|---|---|
| **📄 보고서** | `GET /chat/artifacts?session_id=` 중 type 이 `report/text/file/table/task_card` | **이번에 등록한 기획·설계·PRD 3종** |
| **🖼️ 미리보기** | 같은 목록 중 type `html_preview` 만 | **HTML 목업 v2** `R-DOC 목업 v2 — 채팅 중심 수정 흐름(정적 시안)` (`ba75b010…`) |
| **🗂 문서파일** | `GET /chat/sessions/{id}/documents` — 이 대화가 **파일로 쓴** 경로 목록(도구 입력·러너 산출물 등). 아티팩트가 아니다 | 파일명과 `/docs?…` 링크. 본문은 컨테이너 `/app/docs` 의 파일을 읽는다 |

- 마크다운 문서는 "미리보기" 탭에 나오지 않는다(HTML 전용). "문서/HTML 미리보기" 라는 이름의 탭은 없다.
- 위치: 사이드바에서 세션 "오비스"(워크스페이스 "[CEO] 통합지시") 선택 → 오른쪽 아티팩트 패널. 주소 `https://aads.newtalk.kr/chat#8bf0405a-1f22-4ad9-bb09-6e0fce8c6339` 의 해시 라우팅 동작은 **검증하지 못했다.**
- 이 세션의 "문서파일" 탭 링크는 **믿을 수 없다** (측정, 컨테이너 `aads-server` 의 `/app/docs`):
  - `plans/…MOCKUP_REVIEW_PLAN.md` 의 sha256 앞 12자 `d3f016d31b53` = 정본 revision 2(v1.0.1). v2.0.0(`4d56465d5841…`)이 아니다.
  - `prd/…MOCKUP_REVIEW_PRD.md` `33929843b1b6` = v1.0.1, `specs/rdoc-mockup-review/spec.md` `4563a3b64ccd` = v1.0.1.
  - `specs/rdoc-mockup-review/mockup-v2.html` 은 **파일이 없다**(404 가 된다).
  - 즉 그 탭은 v2.0.0 문서 이름을 보여주면서 구판 본문을 열거나 404 를 낸다. 이것이 "문서가 안 열린다" 의 한 원인이다. 컨테이너 파일은 변경하지 않았다. 그 탭 대신 "보고서" 탭의 등록본을 보라.
- 상대 경로 `.md` 링크는 쓰지 않았다. 본문은 아티팩트 `content` 로 직접 조회된다.

## 5. 데이터 경로 검증 (실제 실행)

| 명령/방법 | 결과 |
|---|---|
| `register_doc_artifacts.py check` | git `5c88c56b` == 정본 해시, 3종 모두 미승인·미등록 확인 |
| `register_doc_artifacts.py apply` ×2 | 1회차 삽입 + 사후 해시 검증 통과, 2회차 삽입 0 (등록 건수 3 유지) |
| `register_doc_artifacts.py verify` | 3종 `OK`: type=report, 본문 sha256 == 정본, tenant 일치, 임의의 다른 tenant 조회 0건, workspace `48cb8821` |
| `aads-server` 컨테이너에서 `svc.list_artifacts(session, tenant, limit=61)` / `svc.get_artifact` | 목록 61건 안에 3종+목업이 모두 포함, 본문 sha256 4건 일치, `freshness_gate` 변형 없음, 다른 tenant 로 `get_artifact` → `None` |
| 목록 위치(created_at 내림차순) | 문서 3종 1~3번째, 목업 12번째 |
| `python3 -m py_compile`, `ruff check --select F821,F811,F401` | 통과 |
| `curl` 상태 | `/chat` 307(비로그인), `/api/v1/health` 200, `/api/v1/chat/artifacts` 비로그인 401 |

이 서비스 읽기는 HTTP API 가 호출하는 함수와 같지만 **HTTP 인증 경로를 거치지 않았다.** 인증 API GET 은 수행하지 못했다.

## 6. HTML 목업 렌더 — 로컬 Chromium 재현 (운영 E2E 아님)

- 방법: 아티팩트 본문(sha256 `74cd890d…` 일치)을 DB 에서 꺼내, `htmlPolicy.ts` 의 `createIsolatedHtmlPreviewDocument` 와 같은 문자열(CSP `default-src 'none'; … script-src 'none'` + `iframe sandbox="" srcdoc`)로 만들고, 패널의 `<iframe srcDoc={staticArtifactHtml(…)} sandbox="">` 와 같은 구조로 한 번 더 감싸 Playwright Chromium 에서 열었다(데스크톱 1440×900, 모바일 390×844).
- 결과: 안쪽 srcdoc 까지 본문이 렌더되고(텍스트 1117자, 탭 5개 시안 카드 표시), 콘솔·페이지 오류 0건, 가로 넘침 없음. **중첩 srcdoc 가 CSP 에 막히지 않는다**(추측이 아니라 이 재현의 실측). sandbox/CSP 를 완화하지 않았다.
- 한계: 호스트 페이지는 `file://` 이고 실제 /chat 페이지·인증·Next 렌더가 아니다. 호스트 `/login` 응답에는 CSP 헤더가 없음을 확인했지만 `/chat` 응답 헤더는 인증 없이 볼 수 없었다. 위 결과를 운영 화면 성공으로 읽지 마라.
- 목록 → 선택 → content → iframe 중 **목록/선택/content 는 §5(서비스), iframe 은 이 재현**으로 분리 검증했다. 마크다운 3종이 "보고서" 탭 렌더러에서 전문으로 보이는지는 **브라우저로 확인하지 못했다.**

## 7. 미완료 — 실제 /chat 화면 열람

- 시도/제약: 이 러너 세션에는 브라우저 업무키·Vault 도구가 없다. 지시서에 적힌 Vault 자격증명 E2E 는 `credential_test_login failed` 였고 `ensure_work_session` 은 `no online PC agent` 였다. 비밀을 출력하거나 관리자 토큰을 만들거나 테넌트 권한을 우회하지 않았다. 로그인 없이 /chat 은 307 로 로그인 화면으로 간다(이전 VERIFICATION-v2.md 와 같다).
- 따라서 데스크톱/모바일 실제 캡처(기획·설계·PRD 각각 선택·전문 노출, HTML 시안 선택)는 **없다.** 합성 화면은 캡처로 쓰지 않았다.
- 남은 일: 로그인된 세션에서 `/chat` → "오비스" 세션 → "📄 보고서" 탭에서 위 제목 3건을 선택해 전문(끝부분까지)과 "🖼️ 미리보기" 탭의 목업을 desktop/mobile 로 캡처한다. 통과 기준은 본문 끝 문장 노출과 제목의 "미승인" 표기다.

## 8. 렌더러/패널 코드에서 확인한 한계 (수정하지 않음, 후속 범위 후보)

수정은 이번 범위가 아니다(앱 코드 변경 없음).

1. **최근 60건 창**: 패널은 `limit=61` 로 세션 아티팩트를 `created_at DESC` 로 받고 지시 초안(`directive_draft`)만 창 밖에서도 유지한다(`retainArtifactWindow`). 이 세션은 812건이 넘고 러너 알림·표가 계속 쌓이므로, 등록한 문서 3종과 목업은 새 아티팩트 약 60건이 쌓이면 목록에서 밀려난다(현재 1~3번째/12번째). 최소 수정안: 메타데이터 `document_key` 가 있는 아티팩트도 `retainArtifactWindow` 에서 유지.
2. **"문서파일" 탭 링크 신뢰성**: 위 §4. 최소 수정안은 그 탭 항목이 정본 최신 revision 을 가리키게 하거나, 컨테이너에 없는 파일은 링크하지 않는 것.
3. 최신 approved 목업 강제 기능은 구현 범위 아님.

## 9. 롤백

이 작업이 만든 것은 `chat_artifacts` 3행뿐이다. 아래 ID 로만 삭제한다(`registered_by` 도 일치해야 삭제).

```
python3 docs/specs/rdoc-mockup-review/register_doc_artifacts.py rollback
-- 동등 SQL: DELETE FROM chat_artifacts
--   WHERE id IN ('68a898e7-a80d-5551-a110-022411bea57b','37fb8077-0ca3-53cc-a912-9c83372b9970','4c16be6b-538f-5082-b124-bf8627cbd1a9')
--     AND session_id='8bf0405a-1f22-4ad9-bb09-6e0fce8c6339' AND metadata->>'registered_by'='AADS-RDOC-ARTIFACT-OPEN-REPAIR-20261003';
```

`chat_artifacts` 의 변경은 트리거 `trg_chat_artifact_revision` 이 리비전으로 남긴다.

## 10. 변경 파일

- `docs/specs/rdoc-mockup-review/ARTIFACT-REPAIR.md` (신규, 이 문서)
- `docs/specs/rdoc-mockup-review/register_doc_artifacts.py` (신규, 멱등 등록·검증·롤백 스크립트; DB 접속은 `scripts/error_book.py` 의 규약을 재사용)
- 지시서에 없는 파일 변경 없음. 공통 `HANDOVER.md` 는 다른 ACCT 작업과의 충돌 때문에 건드리지 않고 DB `project_handover_entries`(entry_key `rdoc-artifact-open-repair-20261003`)에 기록했다.
- 운영 DB 변경: `chat_artifacts` INSERT 3행, HANDOVER 항목 갱신 1건, 오류 사전 `chat.document_body_not_registered` 재발 기록 1건.
