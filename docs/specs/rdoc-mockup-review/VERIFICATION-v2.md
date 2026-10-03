# R-DOC 목업 v2 검증 기록

- 작성: 2026-10-03 KST · TASK_ID AADS-RDOC-CHAT-MOCKUP-REVISION-20261003 · 문서 버전 2.0.0(draft, 미승인)
- 범위: v2 시안·정적 아티팩트·문서 3종의 **시연 수준 검증**이다. 운영 승인·서버 강제 게이트는 구현하지 않았고 검증 대상도 아니다.
- 비용: $ 미측정(외부 LLM 추가 호출 없음).

## 1. 실제 /chat Before — blocked_evidence
- 시도: 헤드리스 Chromium 으로 https://aads.newtalk.kr/chat 을 데스크톱 1440×900·모바일 390×844 에서 열었다.
- 결과: 두 viewport 모두 `https://aads.newtalk.kr/login?redirect=%2Fchat` 로 이동했다. 로그인 자격증명·토큰은 사용하지 않았다.
- 판정: **실제 Before 확보 실패(blocked_evidence).** `evidence-v2/blocked-before-login-{desktop,mobile}.png` 는 “접근이 막혔다는 사실”의 증거일 뿐이며 Before 로 쓰지 않는다. 합성 화면도 Before 로 표기하지 않았다.
- 남은 일: 마스킹된 실제 /chat 전체 캡처(같은 route·viewport·fixture)가 있어야 이 변경의 승인 요청이 완결된다.

## 2. After(v2 목업) 동작 검증
- 도구: 로컬 Playwright Chromium(file URL), fixture `fx-rdoc-chat-01`, 실행 시각 2026-10-03T12:50:05+09:00. 스크립트 `evidence-v2/verify_v2.py`, 결과 `evidence-v2/verification-v2.json`.
- 결과: **82개 검사 모두 통과, 브라우저 콘솔/페이지 오류 0건, 목업·아티팩트 네트워크 요청 0건.**

| 영역 | 확인한 것 |
|---|---|
| 흐름(데스크톱·모바일 각각) | 보고된 v1 카드(message/artifact/review/screen/revision/해시 표기) → “승인 버튼을 아래로 옮겨줘” 전송(빈 요청 거절, 답장 대상 칩) → 대상 v1 확정·접수 카드(승인·구현 명령 아님, 원본 보존) → 수정중(승인 버튼 비활성) → v2 재보고(요청별 반영/미반영, 미승인, 승계 없음) → v1↔v2 비교 → 승인 확인 대화상자가 정확한 버전을 명시 → 승인(시연) 면책 문구 / 재수정 |
| 신뢰 영역 | 승인·수정 요청 버튼이 미리보기 iframe(sandbox=\"\") 밖에 있다. 승인 결과·거절이 패널 안에서도 보인다(모바일 전체화면 포함) |
| 예외 | 오래된 카드 승인 → 409 stale_revision 거절, 저장 실패 시 오류 표시·입력 유지·재시도가 같은 idempotency_key 사용, 중복 전송은 “이미 접수됨”, 늦게 도착한 구버전 결과는 덮어쓰지 않음, 대상 선택 질문은 1회, 취소해도 입력 보존, 재접속 후 초안 복원 |
| 가로 넘침 | 1440·768·390·360 폭에서 모두 없음 |
| 정적 아티팩트 | 스크립트 태그 없음, 50000자 이하(8531자), 패널과 같은 래퍼(iframe sandbox=\"\" + CSP script-src 'none')에서 5개 탭 모두 렌더(데스크톱·모바일), 가로 넘침 없음 |

대표 캡처: `evidence-v2/after-{desktop,mobile}-1~10-*.png`(흐름 단계별), `artifact-panel-desktop-tab1~5.png`, `artifact-panel-mobile-tab1.png`.

## 3. 한계(주장하지 않는 것)
- 이 검증은 **실제 채팅 패널 안에서 열어 본 것이 아니다.** 실제 /chat 은 로그인이 필요해 접근하지 못했다. 패널 렌더는 같은 sandbox·CSP 를 재현한 래퍼로 확인했다.
- 시연의 “승인”은 이 페이지 메모리에서만 일어나며 운영 승인·DB 저장·작업 제출이 아니다.
- 접근성(WCAG 2.2 AA) 인증, 실제 서버 게이트, 채팅 UI 코드 변경은 이번 범위가 아니다.
- 게시본을 URL 로 받으면 Cloudflare 가 챌린지 스크립트를 덧붙여 바이트가 달라진다(v1 도 동일). 호스트 파일의 sha256 은 로컬 원본과 같다(manifest 참조).

## 4. 실행한 검증 명령과 결과
| 명령 | 결과 |
|---|---|
| `PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright python3 evidence-v2/verify_v2.py` | pass, 82 checks, console/page errors [] |
| `python3 -m py_compile build_v2.py evidence-v2/verify_v2.py` | 성공 |
| `ruff check --select F build_v2.py evidence-v2/verify_v2.py` | All checks passed |
| `python3 build_v2.py` 재실행 후 artifact-v2.html·mockup-v2.html·preview-v*.html sha256 비교 | 이전 출력과 동일(결정적) |
| plan/prd/spec 본문 비밀 패턴(canonical_documents.SECRET) 검사 | 일치 0건 |
| 게시 파일 대조: `sha256sum mockup-v2.html /var/www/certbot/screenshots/rdoc-mockup-review-v2/index.html` | 동일 |

## 5. 정본 문서 등록(초안)
같은 document_key 에 version 2.0.0 새 revision 으로 등록했다. 기존 승인본 지정(approved_revision_id)은 건드리지 않았고 goal 연결을 유지했다. 등록 방식·해시·revision id 는 HANDOVER-v2.md 에 있다.

## 6. 채팅 아티팩트 연결
v2 정적 아티팩트를 세션 8bf0405a 에 html_preview 로 저장했고 서비스 읽기 경로(get_artifact·list_artifacts)로 읽어 해시 일치와 목록 첫 위치를 확인했다. **브라우저에서 실제 패널을 여는 검증은 로그인 필요로 하지 못했다.**
