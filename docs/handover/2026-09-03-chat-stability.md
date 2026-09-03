# 2026-09-03 11:50 KST — 채팅 응답 중단 / 스크롤 점프 후속 검증

## Blue/Green 정합성
- `.active_port=8100`, `.active_container=aads-server`
- nginx `/etc/nginx/conf.d/aads-upstream.conf`: 8100 primary, 8102 backup → 파일/컨테이너/nginx 3자 일치 확인
- `execution_lease_owned_elsewhere` 최근 3시간 로그 발생 0건 (기존 최다 중단 사유 해소)

## 중단율 추이 (chat_turn_executions, KST 시간대별)
| 시각 | completed | interrupted | 중단율 |
|---|---|---|---|
| 06시 | 12 | 4 | 25.0% |
| 07시 | 20 | 1 | 4.8% |
| 08시 | 14 | 5 | 26.3% |
| 09시 | 15 | 1 | 6.3% |
| 10시 | 18 | 1 | 5.3% |
| 11시 | 11 | 1 | 8.3% |

## 12시간 interrupt_category 분포
superseded 5 / NULL 5 / resume_no_response 3 / unknown 2 / producer_incomplete 1

## 조치 (커밋 ea8bcc5b)
- 원인: `app/routers/chat.py` 세션 폴링 정합화 경로에서
  `error_message='assistant message already terminal'`만 기록하고 `interrupt_category`를 비워둠 → NULL 5건
- 수정: 해당 UPDATE에 `interrupt_category = COALESCE(interrupt_category,'assistant_terminal_reconcile')` 추가
- 검증: `python3 -m py_compile` 통과 → `reload-api.sh` 핫리로드(74모듈) → commit `ea8bcc5b` → origin/main 푸시
- 자동복구 영향 없음: `_should_auto_resume_interrupted_reason`의 blocked_tokens에 이미 동일 문구가 있어 재개 루프 유발 없음

## 대시보드 스크롤 점프
- 패치 커밋 `06f9279 fix(chat): prevent recovery scroll jump` → origin/main 반영 확인
- 이미지 `aads-dashboard:352dc4e5476f` 로 blue/green 양쪽 기동 (2026-09-03 11:19 KST)

## 검증 결과
- `https://aads.newtalk.kr/api/v1/ops/health-check` → HTTP 200
- `https://aads.newtalk.kr/chat` → HTTP 307 (미인증 리다이렉트, 정상)
