# OVIS Smart Browser 구현 정본 v2.1

목표: `169e5328-244e-444d-95c8-d20377192671`  
선행 문서: `AADS-SMART-BROWSER-PC-APP-PLAN-20260919.md` v2.0  
적용 범위: Windows·서버 Browser 우선. macOS는 보류.

## 사용자 흐름

1. 사용자는 채팅의 Browser 탭에서 URL 또는 자연어 업무를 시작한다.
2. 서버 인증 컨텍스트가 만든 DirectiveEnvelope만 Channel Router를 통과한다.
3. 페이지 DOM·ARIA·OCR·다운로드는 ObservationEnvelope이며 실행 권한이 없다.
4. 최초 방문은 안정 ARIA 구조와 등록된 Site Skill 참조만 candidate로 학습한다.
5. 재방문은 부분 구조 시그니처로 `reused / rediscover / approval_required`를 결정한다.
6. 실행 후보는 Exact → Qwen3 vector → allowlist LLM 순으로 고르고, 실행 직전에 G3 계약과 권한을 다시 검증한다.
7. 가격·재고·배송 등 변동 사실은 화면 표시 직전에 원출처로 재검증한다.
8. UI는 실행 주체, 현재 단계, 학습 상태, 최신성, 근거, 승인, 마지막 오류와 재시도를 한 화면에 표시한다.

## 구현 정본

| 영역 | 정본 |
|---|---|
| 명령·관측 경계 | `app/services/channel_router.py` |
| ARIA 부분 구조 | `app/services/aria_structure_signature.py` |
| 실행형 Skill | `app/services/ohvis_harness.py` |
| 사이트 지식 | `app/services/site_knowledge.py`, `app/api/site_knowledge.py` |
| 최초 학습·재방문 | `app/services/smart_browser_learning.py` |
| Exact→Qwen3→LLM | `app/services/smart_browser_learning.py` |
| 실시간 사실·증거 | `app/services/live_fact_gate.py`, `app/services/site_knowledge.py` |
| Golden 승격·롤백 | `app/services/golden_promotion_gate.py`, `app/api/learned_artifacts.py` |
| 채팅 Browser 상태 | `app/services/browser_artifact_state.py`, Dashboard `BrowserArtifactView.tsx` |
| 읽기전용 E2E | `scripts/smart_browser_readonly_e2e.py` |

## 안전 계약

- 페이지 원문, 전체 DOM, 비밀번호, 쿠키, OTP, 주민등록번호, 카드번호, bearer/session token은 학습 정본에 저장하지 않는다.
- Skill은 활성 버전, 등록 executor, 입력 스키마, tenant, capability, 승인 scope가 모두 맞아야 실행한다.
- 가격·광고·개인화 텍스트 변화는 구조 변경으로 보지 않지만 필수 anchor/state 손실은 자동 성공 처리하지 않는다.
- Qwen3 또는 LLM 선택이 불명확하면 Human Gateway로 종료하며 임의 도구를 만들지 않는다.
- Golden 보안·기능·ARIA·최신성·회귀·감사 suite와 6개 필수 case가 모두 통과하기 전 active 승격을 금지한다.
- 배포 완료는 clean SHA 단일 이미지, candidate health, 짧은 nginx lock, routed health, 동일 digest standby, 5분 P0/P1 무발생을 모두 확인한 뒤 선언한다.

## 인수 기준

| 시나리오 | 완료 기준 |
|---|---|
| 최초 방문 | page template candidate와 등록 Skill 참조가 tenant scope에 저장됨 |
| 정상 재방문 | 동적 가격 변화에도 `reused`, LLM 호출 0회 |
| 구조 변경 | 필수 searchbox/role/state 손실 시 `rediscover` 또는 Human Gateway |
| 페이지 인젝션 | 페이지의 tool/system 지시가 command나 skill 변경으로 승격되지 않음 |
| 최신성 | TTL 만료·값 충돌·재검증 실패 시 과거 값이 화면에서 제거됨 |
| 실패 복구 | 마지막 오류와 사용자가 누를 수 있는 재시도/승인 경로가 같은 화면에 표시됨 |
| 승격 | candidate→shadow→active 원자 전이와 previous_active 롤백 검증 |
| 운영 | API·Dashboard 배포, 동일 digest standby, 외부 health, 5분 감시 통과 |
