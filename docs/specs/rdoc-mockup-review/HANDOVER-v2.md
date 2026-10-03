# R-DOC 목업 검토 v2 — 핸드오버

- 작업: AADS-RDOC-CHAT-MOCKUP-REVISION-20261003 (Runner runner-21d2ffb5) · 2026-10-03 KST
- 상위 목표: 0361c451-cc03-4bd1-a423-76051b0546b2 (연결 유지)
- 상태: plan/prd/spec **2.0.0 draft(미승인)**. 기존 승인본 지정은 바꾸지 않았다.

## 의존 정정 — 공통 HANDOVER 미병합
공통 `HANDOVER.md`·`docs/HANDOVER.md` 는 **runner-ff043498 완료 전까지 수정하지 않았다.** 이번 기록은 이 파일과 DB(project_handover_entries, project=AADS, entry_type=status, entry_key=rdoc-chat-mockup-revision-20261003)에만 있다. **미완은 공통 파일 병합 하나뿐**이며 runner-ff043498 이 끝난 뒤 이 문서의 요약을 공통 파일에 옮기면 된다.

## 이번에 한 일
- 주 경로를 “채팅 메시지 목업 카드 → 같은 채팅 아티팩트 패널(모바일 전체화면)”로 바꾸고 별도 페이지는 확대·공유용 보조로 낮췄다.
- “방금 것 수정” 대상 결정(답장·선택 문맥, 후보 여럿이면 1회 질문), change_request 기록(change_request_id + source_message_id + base_revision), 수정중→새 불변 버전→요청별 반영/미반영·전후 비교→재검토, 새 버전 미승인·승계 금지, 같은 변경건 작업 보류·체크포인트 재검토(kill 금지), 예외 6종 계약, 신뢰 영역 승인(iframe 밖, 샌드박스 완화 금지)을 plan/prd/spec 에 반영했다(PRD M13~M18, AC-M13~M22).
- v2 목업(`mockup-v2.html`), CSS 전용 정적 아티팩트(`artifact-v2.html`), 생성기(`build_v2.py`), 증거·검증(`evidence-v2/`, `VERIFICATION-v2.md`, `manifest-v2.json`)을 만들었다. v1 산출물은 그대로다.

## 정본 문서 등록(초안)
| document_key | revision | version | content_hash(sha256) | revision_id |
|---|---|---|---|---|
| rdoc-mockup-review-plan | 3 | 2.0.0 | 4d56465d584125dca92b4ea0abbc7b5fe7e0f35a1602aeed351fa6f7b80cf234 | bd8aa614-019f-46c9-adf0-e960572083e4 |
| rdoc-mockup-review-prd | 2 | 2.0.0 | 07c8fd9f350e2b8024e9b7f06175a096fb3f03eaab7f28ee0a27d16b4b767892 | d98a04e0-bb89-439d-82e8-505030c00698 |
| rdoc-mockup-review-spec | 2 | 2.0.0 | 6c1d251bf6c299b75280f2a6844c60b340f9ce8a3aea531c0808c1acbeae0462 | 230fa6b9-2f26-485d-bcef-a4da74557892 |

- expected_generation 2/1/1 → 등록 후 generation 3/2/2. idempotency_key `<document_key>-v200-20261003`. DB 본문 sha256 = 파일 sha256 을 SQL 로 확인했다. goal 링크 유지, approved_revision_id 변경 없음.
- **등록 경로 공개:** 이 러너에는 정본 API 용 테넌트 JWT 도 MCP 도구도 없어서 HTTP 경로를 쓰지 못했다. `canonical_documents.create_revision` 의 검사(goal 존재, head FOR UPDATE, kind·version·해시·idempotency 충돌, expected_generation, 이벤트·goal 링크)를 한 트랜잭션 SQL 로 그대로 복제해 실행했다. 따라서 HTTP 인증·권한(grant) 검사는 거치지 않았고 author_id 는 `runner:21d2ffb5`, 본문은 v1 때와 같이 인라인(source_path 없음)이다.

## 채팅 아티팩트
- artifact_id `ba75b010-af90-5c98-b96d-8e71aaa0327a`, type html_preview, session 8bf0405a-1f22-4ad9-bb09-6e0fce8c6339, 본문 sha256 `74cd890d7b54e18c40c3d3a0ff8fff850810a7464eaba65932f5cf09acbd2e4a`.
- 방식: chat_artifacts 직접 INSERT(선례: ohvis_task_manager task_card). 사용자 메시지는 삽입하지 않았고 인증 비밀도 쓰지 않았다.
- 확인: 서비스 읽기 경로로 get_artifact 해시 일치, list_artifacts 첫 위치. **브라우저에서 실제 패널을 여는 검증은 로그인이 필요해 하지 못했다.**

## 게시
`/var/www/certbot/screenshots/rdoc-mockup-review-v2/`(신규 디렉터리). index.html = mockup-v2.html(해시 동일). 공개 URL: https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v2/index.html (Cloudflare 가 스크립트를 덧붙여 응답 바이트는 달라짐).

## 남은 것
1. **실제 /chat Before**: blocked_evidence. 마스킹된 로그인 세션에서 같은 route·viewport·fixture 로 전체 캡처 필요. 그 전에는 합성 화면을 Before 로 쓰지 않는다.
2. **공통 HANDOVER.md·docs/HANDOVER.md 병합**: runner-ff043498 완료 후.
3. **운영 강제 미구현**: 서버 승인 게이트, 실행 직전 검증, 채팅 UI 코드 변경, 신뢰 영역 승인 버튼 구현은 이번 범위가 아니다.
4. **실제 패널 열림 확인**: 로그인된 브라우저에서 아티팩트 탭 확인 필요.
5. 이 변경분은 워크트리의 미커밋 상태이며 커밋·푸시는 Runner 가 CEO 승인 후 처리한다.
