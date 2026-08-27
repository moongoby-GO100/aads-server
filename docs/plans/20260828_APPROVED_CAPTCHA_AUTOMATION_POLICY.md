# Approved CAPTCHA Automation Policy

기준 시각: 2026-08-28 07:43 KST

## CEO 지시 정정

CAPTCHA/OTP 우회는 금지한다. 다만 우회 금지는 자동화를 전면 차단한다는 뜻이 아니다.
CEO 또는 권한 있는 운영자가 특정 페이지, 업무, 실행 범위, 만료 조건을 승인하면 AADS는 그 범위 안에서 LLM/비전 모델 분석, 자동 입력, 자동 제출, 결과 재판정을 수행할 수 있다.

이전 표현 중 "CAPTCHA 숫자를 비전 모델로 읽는 fallback 즉시 차단"은 부정확하다. 정확한 정책은 다음과 같다.

| 구분 | 정책 | 예시 |
|---|---|---|
| 승인 없음 | 자동 판독/입력 금지, 승인 요청으로 전환 | 땡겨요 로그인 CAPTCHA 감지 후 대기 |
| 승인 있음 | 승인된 origin/work_key/session/run 안에서 모델 판독 후 자동입력 허용 | `boss.ddangyo.com` 로그인 페이지 1회 승인 |
| 범위 초과 | 즉시 중단, 재승인 요청 | 승인 페이지와 다른 origin, 실행횟수 초과 |
| 우회 금지 | 보안장치 회피, CAPTCHA 회피 요청, OTP 무단 생성/읽기 금지 | `bypass captcha`, OTP 앱/문자 무단 대리 처리 |

## 운영 계약

승인 레코드에는 값 자체를 저장하지 않는다. 반드시 아래 메타데이터만 남긴다.

| 필드 | 목적 |
|---|---|
| `approved_by` | 누가 승인했는지 |
| `approved_at` | 언제 승인했는지 |
| `origin`/`origins` | 어떤 페이지/도메인 범위인지 |
| `work_key`/`session_id`/`run_id` | 어떤 브라우저 세션과 실행인지 |
| `challenge_kind` | CAPTCHA/OTP 등 챌린지 종류 |
| `automation` | 허용된 자동화 방식 |
| `max_executions` | 반복 실행 허용 횟수 |

CAPTCHA 값, OTP 값, 비밀번호, 토큰은 원장/로그/DB에 저장하지 않는다.

## 구현 반영

1. `app/services/browser_permission_policy.py`
   - `solve/read/vision captcha`를 전면 deny에서 ask로 변경한다.
   - `bypass captcha`, OTP 모델 생성/읽기, 명시적 우회 payload는 계속 deny한다.

2. `app/services/browser_task_gateway.py`
   - 승인 토큰 scope에 `allow_model_challenge_analysis=true`가 있을 때만 CAPTCHA 모델 분석을 허용한다.
   - origin, selector, challenge kind, 실행횟수를 검증한다.

3. `app/services/yeoljeong_finance_service.py`
   - 땡겨요 CAPTCHA 감지 시 승인 scope가 없으면 대기한다.
   - 승인 scope가 있으면 비전 모델 판독, 입력, 제출, 재판정을 자동 수행한다.
   - `delivery_browser_session_events.jsonl`에는 승인자/승인시각/페이지/scope만 기록하고 값은 기록하지 않는다.

4. `app/services/captcha_vision_solver.py`
   - 승인 컨텍스트 없이는 실행하지 않는다.
   - 모델 raw 응답과 CAPTCHA 값은 로그에 남기지 않는다.

5. `migrations/131_browser_approval_tokens.sql`
   - 범용 Browser Task 승인 토큰 테이블과 permission request scope 컬럼을 추가한다.

## 완료 기준

- 승인 없는 땡겨요 CAPTCHA 모델 판독은 실행되지 않는다.
- 승인된 땡겨요 CAPTCHA 자동화는 같은 페이지 origin에서만 실행된다.
- 승인 이벤트에는 누가/언제/어떤 페이지를 승인했는지가 남는다.
- CAPTCHA 값 자체는 로그와 원장에 남지 않는다.
- 회귀 테스트는 정책 분류, 승인 scope 소비, 땡겨요 자동입력 흐름을 검증한다.
