# R-DOC 목업 필수 제출·승인 — 기획
- 프로젝트: AADS · 버전: 2.0.0 · 작성일: 2026-10-03 KST (v1.0.1 → 2.0.0 개정, 같은 날)
- 상태: 검토용 초안(draft). 문서 작성 지시는 승인됐지만 이 설계·시안의 구현 승인은 아직 없다. 기존 승인본 지정은 바꾸지 않았다.
- 상위 목표: 0361c451-cc03-4bd1-a423-76051b0546b2 (기존 R-DOC 목표, 조회 확인)
- 근거: CEO의 “화면 구현이 필요한 기획·설계·PRD는 목업까지, 기존 화면 수정은 전후 페이지까지 보고” 지시.
- 이 패키지는 R-DOC의 목업 제출·승인 기능에 대한 독립 승인 단위다. 기존 저장 규칙 문서를 복제하거나 기존 document_key/승인본을 교체하지 않는다.

- document_key: rdoc-mockup-review-plan
- 연결: [PRD](../../docs/prd/20261003_AADS_RDOC_MOCKUP_REVIEW_PRD.md), [설계](../specs/rdoc-mockup-review/spec.md), [v2 인수·검증 기록](../specs/rdoc-mockup-review/VERIFICATION-v2.md)
- 상위 명세: rdoc-storage-rule-spec 1.2.0. 이 문서의 신규 목업 의무가 해당 기능의 세부 제출 기준이다.

## v2 개정 요약 (2.0.0)
- 계기: CEO가 “보고된 목업을 보고 ‘방금 것’ 수정을 요청하는” 실제 사용 방식을 고려하라고 지시. v1은 채팅 → 별도 페이지 링크가 주 경로였고 채팅 UI 변경을 비범위로 두었다.
- 변경: **주 경로 = 채팅 메시지의 목업 카드 → 같은 채팅 아티팩트 패널(모바일은 같은 대화 문맥의 전체화면 패널)**. 별도 페이지 /design/reviews/{review_id} 는 확대·공유용 보조 경로다.
- 변경: “방금 것 수정”을 최신성이 아니라 실제 답장·선택 문맥으로 해석하고, 수정 요청을 change_request_id 로 기록한다(spec §채팅 중심 검토 경로).
- 변경: 동시·오래된 카드·중복·연속·재접속·승인 저장 실패의 계약을 추가(spec §예외 계약, PRD M13~M18/AC-M13~M22).
- 유지: 불변 패키지, 서버 승인 게이트, 문서 3종 승인·구현·배포 승인 분리, 운영 강제 미구현 표기, v1 산출물 보존.
- 삭제: v1 문구 “기존 채팅 UI 변경 비범위”와 “채팅에는 기존 일반 링크를 사용한다”(사유: 주 경로가 채팅 패널로 바뀜).

## 문제와 확인 근거
기존 R-DOC spec §6~12에는 화면 분류, 디자인 조사, 시안, 승인 기준이 있으나 모든 UI 변경의 전체 페이지 목업 제출을 확인하는 구현은 확인되지 않았다. origin/main의 canonical_gate.py는 shadow/fail-open이며 enforce를 설정하는 것만으로 차단 기능이 생기지 않는다. 기존 Workbench는 before/after 이미지와 QA 채점 중심이다. 이것을 버전 고정 승인 화면으로 간주하지 않는다. [코드 확인]
프로젝트 진행 표준(app/static/reports/aads-project-lifecycle-guide.html)의 Layout은 텍스트 와이어프레임을 허용하고 XS/S 변경은 텍스트 Before/After로 보고할 수 있다. UI 변경에는 이 완화 규정을 적용하지 않는 보완이 필요하다. 같은 URL 내용 덮어쓰기 대신 목업 리비전별 불변 주소를 제공해야 한다. [코드 확인]

## 대상 사용자와 목표
대표님/승인권자는 문서와 실제 시안을 한 화면에서 보고 승인하거나 수정 요청한다. PM은 화면 목록과 제출 누락을 확인한다. 디자이너·개발자는 어떤 버전으로 만들어야 하는지 확인한다. QA는 승인 시안과 구현 화면의 차이를 검증한다.
첫 진입: 채팅에 보고된 목업 카드(메시지·아티팩트·review_id·screen_id·revision·해시 표기) → 같은 채팅의 아티팩트 패널(모바일은 전체화면, 같은 대화 문맥) → 대상 화면·버전·차이를 보고 수정 요청 또는 승인 확인. 별도 페이지는 확대·공유용 보조 경로. 반복 사용: 수정 요청 → 수정중 → 새 불변 버전(v2) 재보고 → 요청별 반영/미반영 사유와 v1↔v2 비교 → 재검토. 같은 카드에서 최신 초안과 승인본을 구분한다. 실패 복구: 로그인 복귀 경로 보존, 권한 없음은 권한 요청 안내, 네트워크 실패는 입력 보존·재시도, 오래된 카드로 승인하면 409(stale_revision)로 거절하고 최신 버전 열기를 안내한다. 재접속하면 서버 기준 대상 선택·미전송 입력·승인 상태를 복원한다.

## 즉시 적용할 제출 규칙
- 신규 UI: 전체 페이지 목업, 열리는 미리보기 링크, 데스크톱/모바일, 주요 상태와 핵심 흐름을 기획·설계·PRD에 함께 보고한다.
- 기존 UI 수정: 같은 route·viewport·fixture 조건의 실제 변경 전 전체 페이지 캡처 + 변경 후 전체 페이지 목업 + 변경점 목록을 보고한다. 변경 전 캡처를 재구성 그림으로 대체하지 않는다.
- 작은 UI 변경도 전후 비교는 필수다. 승인된 컴포넌트를 재사용할 수 있지만 변경 페이지 목업 생략 사유는 되지 않는다.
- backend-only: 사용자에게 보이는 화면·문구·순서·상호작용이 전혀 변하지 않음을 PM이 근거로 기록한다. 불확실하면 UI 필요로 분류한다.
- 설계 초안 저장은 허용한다. 제출물이 빠지면 “검토 준비 미완료”로 보고하고 검토 요청/구현 착수 단계로 올리지 않는다.
- 목업 검토 제출에는 승인이 필요하지 않다. 대표님이 목업을 확인한 뒤 승인한다. 승인된 동일 버전만 구현에 사용할 수 있다.
- 문서 승인, 목업 승인, 착수·예산 승인, 코드 검수, 배포 승인은 별개의 범위다. 채팅의 포괄적 “진행”을 특정 목업 버전 승인으로 추정하지 않는다.

## 이번 패키지의 자기 적용 (v2)
이 변경은 **기존 /chat 화면 수정**이므로 실제 Before 전체 캡처가 필요하다. 로그인 없이는 /chat 이 로그인 화면으로 이동했고 비밀번호·토큰을 쓰지 않았으므로 **실제 Before 는 blocked_evidence** 다. 로그인 화면 캡처는 증거 사실로만 보존했고 Before 로 쓰지 않았다. 마스킹된 실제 /chat 캡처가 확보되기 전에는 이 변경의 승인 요청이 완결되지 않는다.
After(제안)는 v2 목업이다: 같은 fixture(fx-rdoc-chat-01)·같은 viewport(1440×900, 390×844)에서 채팅 + 패널 + 신뢰 영역(승인 바)을 보여 준다. 합성 화면이며 실제 Before 로 표기하지 않는다.
시안의 보고·대상 확정·수정중·재보고·비교·승인 확인(시연)·재수정과 오래된 승인 거절·입력 보존은 데모다. 버튼을 눌러도 운영 승인·작업 제출·DB 저장·네트워크 요청이 일어나지 않는다.
v1 목업(mockup-v1.html, 별도 페이지 중심)은 보존한다. 같은 변경건의 새 버전은 v2 이며 v1 승인이 v2 로 승계되지 않는다.
실제 아티팩트 패널은 정적 HTML 미리보기(iframe sandbox="" + CSP script-src 'none')라서 패널 안 버튼은 동작하지 않는다. 그래서 패널용 아티팩트는 CSS 전용 정적 문서로 따로 만들고, 승인·수정 결정 버튼은 iframe 밖 신뢰 영역(채팅 UI/서버 렌더)에 둔다. 샌드박스는 느슨하게 하지 않는다.

## 범위·담당·진행 표준 연결
| 단계 | 필수 산출물/게이트 | 담당 |
|---|---|---|
| Find | 실제 현재 화면과 사용자 흐름, 디자인 조사 출처·확인일·채택/제외 이유 | PM·디자인 |
| Layout | plan/prd/spec + 화면 인벤토리 + 필수 목업 패키지 | PM·디자인·아키텍트 |
| 검토 | 누락 검사 → 버전별 수정 요청/승인 → 문서·목업 revision 고정 | 승인권자·정본관리자 |
| 작업 분해 | 승인 번들 참조 + 예산·착수 승인; 미승인 UI 작업은 실행 차단 | CTO·Runner |
| 구현/검수 | 같은 viewport·fixture의 승인 목업 ↔ 구현 캡처; 기능·시각·접근성 검증 | 개발·QA |
| 배포/마무리 | 별도 배포 권한, 운영 검증, 증거·핸드오버 | Ops |

새 담당 계정/목표/마일스톤을 임의 생성하지 않는다. 현재 목표와 문서만 연결한다.
MVP는 채팅 카드·패널 검토 경로, 수정 요청(change_request), 불변 리비전, 서버 승인 게이트(+별도 확대 페이지). 좌표 주석·다중 안 동시 비교·자동 디자인 점수는 후속이며 제출 필수 조건을 대체하지 않는다.

## 구현 순서와 완료 판정
1. 현재 DB/권한·AAG 대조 후 불변 패키지·감사 모델 추가. 재사용 후보(실재 확인): 채팅 전송 payload 의 reply_to_id/idempotency_key(app/models/chat.py:128-129), 아티팩트 저장·조회 GET /chat/artifacts·chat_artifacts(app/routers/chat.py:4590, chat_service.py:11106·16248), 패널 렌더 ChatArtifactPanel.tsx:2621-2622 와 htmlPolicy.ts staticArtifactHtml, 문서 정본 canonical_documents.py 의 expected_generation/idempotency_key.
2. 검토·승인 API와 구현 실행 게이트부터 구현; 같은 계약을 사용하는 채팅 카드/패널과 신뢰 영역 연결, 이어서 보조 확대 페이지.
3. 격리 환경에서 PRD AC-M01~22 검증. 기존 업무를 끊지 않도록 승인된 대상 범위로 단계적 활성화.
4. 승인된 릴리스 절차 후 실제 화면 검증. L1 주입·모든 세션 코드 강제는 별도 적용 확인 없이는 완료라 하지 않는다.
이번 산출물의 완료는 문서 초안 등록·v2 목업 제시·목업 동작 검증까지다. 문서 의무(이 문서), 시연(목업), 운영 강제(서버 차단)는 서로 다른 층이며 **운영 강제는 구현하지 않았다**. 서버 차단 구현·활성화는 완료 대상이 아니다.
의존 정정: 공통 HANDOVER.md·docs/HANDOVER.md 는 runner-ff043498 완료 전까지 수정하지 않는다. 이번 기록은 DB 핸드오버와 docs/specs/rdoc-mockup-review/HANDOVER-v2.md 에 있고, 공통 파일 병합만 미완이다.
비용: 외부 디자인/LLM 추가 호출 없음. 전체 세션 비용 $ 미측정. 구현 예산·일정은 승인 범위 확정 뒤 산정한다.


## 검토 패키지 v1
- [클릭 가능한 목업](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/index.html) · [데스크톱 전체 캡처](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/evidence-v1/desktop.png) · [모바일 전체 캡처](https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v1/evidence-v1/mobile.png)
- 원본 HTML: `docs/specs/rdoc-mockup-review/mockup-v1.html`. 게시 사본은 같은 바이트로 보존하고 manifest로 대조한다.
- 시연 승인과 실제 승인은 분리한다. 이 문서와 시안은 초안이며 승인본 자동 지정·운영 강제 게이트 활성화를 수행하지 않았다.

## 검토 패키지 v2 (채팅 중심 개정)
- 동작 시안(별도 페이지, 보조): https://aads.newtalk.kr/screenshots/rdoc-mockup-review-v2/index.html · 원본 `docs/specs/rdoc-mockup-review/mockup-v2.html`
- 채팅 아티팩트용 정적 문서(CSS 전용): `docs/specs/rdoc-mockup-review/artifact-v2.html` — 패널 안에서는 스크립트가 없어 버튼이 동작하지 않는다.
- 증거: `docs/specs/rdoc-mockup-review/evidence-v2/` (데스크톱 1440×900·모바일 390×844 흐름 캡처, 패널 렌더 캡처, blocked Before 캡처, verification-v2.json), 검증 요약 `VERIFICATION-v2.md`, 해시 manifest `manifest-v2.json`.
- 재생성: `python3 docs/specs/rdoc-mockup-review/build_v2.py` (결정적 출력).
- v1 패키지(mockup-v1.html, manifest-v1.json, VERIFICATION.md, evidence-v1/)는 변경하지 않았다.
