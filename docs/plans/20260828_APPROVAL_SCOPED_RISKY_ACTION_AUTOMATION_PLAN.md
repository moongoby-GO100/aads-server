# Approval-Scoped Risky Action Automation Plan

기준 시각: 2026-08-28 08:09:42 KST

## 1. CEO 지시 해석

OTP/CAPTCHA 우회 금지는 자동화를 막는 정책이 아니다. 금지 대상은 승인 없는 해독, 보안장치 회피, 권한 없는 접근, 몰래 실행이다. CEO 또는 권한 있는 운영자가 특정 사이트, 페이지, 업무, 실행 횟수, 만료시간을 승인하면 AADS는 그 범위 안에서 LLM/비전 모델 분석, 입력, 클릭, 제출, 업로드, 결과 확인을 자동으로 수행할 수 있어야 한다.

핵심 계약은 다음과 같다.

| 항목 | 정책 | 자동화 가능 여부 |
|---|---|---|
| CAPTCHA | 승인 없는 모델 판독 금지. 승인된 페이지/업무 범위 안에서는 모델 판독, 입력, 제출, 재판정 허용 | 가능 |
| OTP | 승인 없는 생성/읽기/탈취 금지. 사용자가 제공한 값, 공식 TOTP/API, 푸시 승인 polling, 이미 완료된 승인 감지만 자동화 | 제한 가능 |
| 인증서 | 승인된 인증서 별칭 선택, Vault write-only 비밀번호 입력, 결과 확인 자동화 | 가능 |
| 결제/이체/환불 | 금액, 수취인, 건수, 만료, 미리보기 승인 범위 안에서만 실행 | 가능 |
| 게시/전송 | 대상 채널, 본문 해시, 첨부 파일, 실행 건수 승인 범위 안에서만 실행 | 가능 |
| 삭제/취소 | 대상 ID 목록, dry-run 미리보기, rollback 가능성 확인 후 승인 범위 안에서만 실행 | 제한 가능 |
| 파일 업로드 | 대상 사이트, 파일 경로/해시, 폼 selector, 건수 승인 범위 안에서만 실행 | 가능 |

## 2. 오비스/AADS 현재 기반

| 기반 | 현재 상태 | 코드/DB 근거 |
|---|---|---|
| Browser Task | 업무별 브라우저 작업 생성/상태/승인 대기 API 존재 | `app/api/browser_tasks.py` |
| 위험 액션 분류 | 결제/삭제/게시/업로드/CAPTCHA/OTP/인증서는 승인 필요 또는 차단 분류 | `app/services/browser_permission_policy.py` |
| 승인 요청 | `agent_permission_requests`에 origin/work_key/action/scope 저장 | `migrations/131_browser_approval_tokens.sql` |
| 승인 토큰 | `browser_approval_tokens`로 token hash, scope, 실행 횟수, 만료 저장 | `migrations/131_browser_approval_tokens.sql` |
| 승인 후 자동화 | 승인 토큰 소비 시 scope, origin, selector, challenge kind, 실행 횟수 검증 | `app/services/browser_task_gateway.py` |
| 감사 로그 | `browser_task_events`와 땡겨요 `delivery_browser_session_events.jsonl` 기록 | `app/services/browser_task_gateway.py`, `app/services/yeoljeong_finance_service.py` |
| Agent Vault | 계정/비밀번호 write-only 입력 기반 존재 | `app/services/agent_vault_service.py` |

## 3. 목표 구조

승인 기반 자동화는 6단계 상태 머신으로 운영한다.

| 단계 | 설명 | 실패 시 처리 |
|---|---|---|
| Detect | 페이지/DOM/스크린샷/업무 상태를 읽어 위험 액션 필요 여부 감지 | 상태 로그 후 대기 |
| Preview | 실행 전 대상, 금액, 파일, selector, 예상 결과를 요약 | 미리보기 불가 시 승인 요청 중단 |
| Request | `agent_permission_requests`에 승인 요청 저장 | 승인 만료 시 자동 실패 |
| Approve | CEO/운영자가 scope와 max_executions를 명시 승인 | 반려 시 task failed |
| Execute | 승인 토큰을 소비하며 자동 입력/클릭/제출/업로드 수행 | 범위 초과 시 즉시 중단 |
| Verify | 결과 화면/DB/API/다운로드 파일로 성공 여부 확인 | 실패 이벤트와 재시도 정책 기록 |

## 4. 승인 토큰 스코프 필드

| 필드 | 필수 | 목적 |
|---|---|---|
| `origin` 또는 `origins` | 예 | 승인된 사이트/페이지 도메인 고정 |
| `work_key` | 예 | 배달수집/은행/홈택스/게시 등 업무 단위 고정 |
| `task_id` | 예 | 승인된 Browser Task 고정 |
| `action_types` | 권장 | `captcha_model_analysis`, `upload`, `payment`, `delete` 등 허용 액션 제한 |
| `selectors` | 권장 | 입력/클릭 가능한 selector 제한 |
| `challenge_kinds` | 조건부 | CAPTCHA/OTP/인증서 같은 챌린지 종류 제한 |
| `allow_model_challenge_analysis` | CAPTCHA 필수 | 승인된 CAPTCHA 모델 판독 허용 플래그 |
| `amount_limit` | 결제/이체 필수 | 건별/총액 한도 |
| `target_ids` | 삭제/취소 필수 | 대상 ID allowlist |
| `file_hashes` | 업로드 권장 | 업로드 가능한 파일 allowlist |
| `recipe_id`, `recipe_hash` | 반복업무 권장 | 승인받은 자동화 레시피 고정 |
| `max_executions` | 예 | 반복 실행 횟수 제한 |
| `expires_at` | 예 | 자동 승인 만료 |

값 자체는 저장하지 않는다. CAPTCHA 정답, OTP, 비밀번호, 토큰, 인증서 비밀번호는 transient input으로만 쓰고 로그/DB에는 남기지 않는다.

## 5. 액션별 구현 정책

| 액션 | 승인 전 | 승인 후 자동화 | 금지선 |
|---|---|---|---|
| CAPTCHA | 감지, 스크린샷 저장, 승인 요청 | 승인 scope 안에서 모델 판독, 입력, 제출, 재시도 | 승인 없는 판독, 회피, 외부 solver |
| OTP | 입력칸 감지, 사용자 입력 대기, 푸시 화면 감지 | 사용자가 제공한 값 주입, 푸시 승인 완료 polling, 공식 TOTP/API 사용 | 문자/앱 무단 읽기, LLM 임의 생성 |
| 인증서 | 인증서 선택 화면 감지 | 승인된 별칭 선택, Vault write-only 비밀번호 입력 | 인증서 파일/비밀번호 노출 |
| 결제/이체 | 금액/수취인/건수 preview 생성 | 승인 범위 내 제출, 결과 영수증 저장 | 한도 초과, 대상 변경 |
| 게시/전송 | 본문/대상/첨부 preview 생성 | 승인된 content hash와 채널에 게시 | 승인 후 본문 변경 |
| 삭제/취소 | 대상 ID 목록과 영향 preview 생성 | 승인된 ID만 처리, 가능 시 rollback 메타 기록 | 전체 삭제, wildcard 삭제 |
| 업로드 | 파일 path/hash/폼 selector preview 생성 | 승인된 파일만 업로드 후 결과 검증 | 파일 변경 후 재사용 |

## 6. 구현 상태와 이번 보강

이번 구현은 기존 승인 토큰 기반 위에 감사 로그를 강화한다.

| 항목 | 반영 내용 | 완료 기준 |
|---|---|---|
| 승인 로그 | `permission:approved/rejected` 이벤트에 승인자, 승인시각, origin, work_key, action, scope, max_executions 기록 | `browser_task_events`에서 누가/언제/어떤페이지 확인 |
| 토큰 소비 로그 | `approval_token:consumed/denied` 이벤트에 승인자, 승인시각, 승인 origin, 실제 origin, selector, 실행횟수 기록 | 토큰 값 없이 실행 추적 가능 |
| 민감값 마스킹 | `captcha_value` 포함 CAPTCHA 관련 키를 마스킹 | 로그/DB에 CAPTCHA 값 미노출 |
| 땡겨요 경로 | 승인 scope 안의 CAPTCHA 모델 판독/자동입력 허용 유지 | 단위 테스트 통과 |

## 7. 운영 정책

1. 승인 없는 위험 액션은 실행하지 않고 승인 요청으로 전환한다.
2. 승인 토큰은 사이트, 업무, task, action, origin, selector, 실행횟수, 만료시간을 벗어나면 즉시 거부한다.
3. 반복 작업은 `max_executions`와 `recipe_hash`로 제한한다.
4. CAPTCHA는 승인 범위 안에서만 모델 판독 자동입력을 허용한다.
5. OTP는 우회하지 않는다. 사용자가 제공한 값, 공식 인증수단, 푸시 승인 완료 감지만 자동화한다.
6. 결제/삭제/게시/업로드는 실행 전 preview가 없으면 승인 요청을 만들지 않는다.
7. 모든 승인과 실행은 감사 로그에 남긴다. 토큰 원문과 인증값은 남기지 않는다.

## 8. 다음 구현 과제

| 우선순위 | 과제 | 설명 |
|---|---|---|
| P0 | Browser Task 실행기에서 모든 risky action 전 `approval-token/consume` 강제 | 도구별 우회 차단 |
| P0 | Preview schema 표준화 | 결제/삭제/게시/업로드 승인 전 표시값 통일 |
| P1 | 반복 레시피 registry | `recipe_id/recipe_hash`로 승인된 반복업무만 재사용 |
| P1 | 승인 토큰 revoke API/UI | 승인 취소와 즉시 중단 |
| P2 | 실행 후 증빙 자동 수집 | 영수증, 게시 URL, 업로드 결과, 삭제 결과 파일화 |

## 9. 검증 기준

| 검증 | 성공 기준 |
|---|---|
| 정책 분류 | 결제/게시/삭제/업로드/CAPTCHA는 ask 또는 승인 토큰 필요 |
| CAPTCHA | 승인 없이는 모델 판독 차단, 승인 있으면 같은 origin에서 자동입력 |
| OTP | LLM/vision/solver source는 차단 |
| 감사 로그 | 승인자/승인시각/origin/action/scope 기록, 인증값 미기록 |
| 실행 제한 | max_executions 초과, origin 불일치, selector 불일치 시 403/denied |
