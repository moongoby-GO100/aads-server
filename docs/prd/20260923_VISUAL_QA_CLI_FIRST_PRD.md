# AADS 배포 QA 재설계 — 브라우저 우선·저비용 CLI 디자인 감리 PRD v1.0

- 작성일: 2026-09-23 (UTC) / 상태: **CEO 확정 PRD, 구현·운영 적용은 후속 작업**
- 범위: AADS dashboard 배포 QA와 server `visual-qa/full-qa`의 판정 계약
- 목적: 변경 기능을 빠르고 재현 가능하게 확인하고, LLM 감리 장애를 제품 결함으로 오판하지 않는다.
- 선행 규칙: `/root/aads/AGENTS.md`의 immutable blue/green, 동일 이미지 standby, 배포 후 5분 P0/P1 관측은 유지한다.
- 오류 사전: `visual_qa.llm_error_false_auto_fail` (원인 등록, 코드 수정은 아직 없음).

## 1. 결정 요약

1. 기능·접근성의 필수 배포 판정은 **인증된 실제 브라우저의 변경 범위별 단언(assertion)**, API 계약, 관련 실행 경로 canary, 양쪽 슬롯 health/로그로 수행한다. 모델 선택기 변경이라면 해당 옵션의 존재·활성·선택 후 값과 실제 모델 registry/relay 계약을 검사한다.
2. 픽셀 비교는 승인된 baseline이 있는 화면에서만 실행한다. baseline 부재는 `INCONCLUSIVE`, 캡처·인증·브라우저 실패는 `QA_INFRA_ERROR`다. 둘 다 제품 `FAIL`이나 `PASS`로 변환하지 않는다.
3. 디자인 LLM은 시각 변경을 포함한 PR, 실제 diff, 또는 운영자가 명시한 경우에만 **비동기·권고(advisory)**로 실행한다. 단순 모델 목록/문자열 변경에는 호출하지 않는다.
4. 디자인 LLM이 필요할 때 1차 후보는 호스트 **Codex CLI `gpt-6-luna`**, 공급자 독립 폴백 후보는 호스트 **Claude CLI `claude-haiku` → `claude-haiku-4-5-20251001`**다. 이는 가격·경량 등급에 따른 *후보*이지 이미지 감리 품질 동등성 또는 계정 사용 가능성의 확정이 아니다. 양쪽 실제 CLI 이미지 canary와 동일 평가세트가 통과하기 전 자동 전환을 켜지 않는다.
5. 기존 `AUTO PASS / AUTO FAIL` 단일 문자열을 기능 판정, 시각 비교, 디자인 조언, QA 인프라 상태로 분리한다. LLM이 500/429/인증/파싱 실패하면 점수 `0`이 아니라 `null`, 상태 `UNAVAILABLE`과 원인 코드를 기록한다.

## 2. 현황과 확인된 문제

| 항목 | 확인된 상태 (2026-09-23) | 설계상 문제 |
|---|---|---|
| dashboard `deploy.sh` Step 7 | `/`, `/chat`, `/ops` 전체에 동기 `POST /visual-qa/full-qa`; `AUTO FAIL`은 경고만 남기고 배포 유지 | 작은 변경에도 무관한 페이지·LLM 평가를 실행하고, 실패 문구와 릴리스 판정의 의미가 다르다. |
| server `app/api/visual_qa.py` | API 호출은 `existing_test_results=None` | `test_status=SKIP`인데 `qa_pipeline.py`가 판정 시 `PASS`로 간주한다. |
| `app/services/qa_pipeline.py` | baseline이 없으면 `NO_BASELINE`; 유효한 디자인 응답이 없으면 `design_score=0`; `<=24`는 `AUTO FAIL` | 검증 미실행과 실제 디자인 불합격을 구별하지 못한다. |
| `app/services/design_auditor.py` | Gemini 2.5 Flash → Claude Haiku 4.5 폴백을 모두 동일 LiteLLM 프록시로 호출 | 모델 공급자는 달라도 프록시 장애는 공동 실패다. 주석의 “Sonnet”과 실제 Haiku 호출도 불일치한다. |
| 배포 #5186 재검사 | `test_status=SKIP`, `visual_status=NO_BASELINE`, `design_verdict=ERROR`, `design_score=0`, `AUTO FAIL`; 보고서에는 LiteLLM 500 | 기능 불합격의 증거가 아니다. 앞선 Groq 429 로그만으로 해당 500의 직접 원인을 확정하지 않는다. |
| 동일 배포의 직접 검증 | 인증된 `/chat`에서 Opus 5.5 옵션의 존재·활성 확인, 두 dashboard 슬롯 동일 digest·healthy, 5분 관측 통과 | 변경 목적에 맞는 브라우저 검증이 실제로 가능하다. |

현재 화면 촬영물은 API 컨테이너의 `/tmp/aads_workspace/screenshots`에 놓이며 호스트에 동일 경로로 공유되지 않는다. 호스트 CLI를 쓰려면 **호스트 브라우저에서 직접 캡처**하거나 검증된 단일 아티팩트 반출 경로가 필요하다. API 컨테이너에 호스트 CLI 바이너리·자격증명·Docker socket을 마운트하는 방안은 채택하지 않는다.

## 3. 목표·비목표

### 목표

- 변경된 기능을 브라우저에서 직접 확인하고 실패 증거(단언, URL, 스크린샷, console/network)를 남긴다.
- 제품 실패와 테스트 환경/모델 공급자 실패를 분리해 거짓 합격·거짓 불합격을 막는다.
- LLM 호출 수·비용·대기시간을 최소화하고, 필요 시 다른 회사의 독립 CLI로 한 번만 폴백한다.
- 운영자가 릴리스별로 어떤 검사가 `PASS`, `FAIL`, `SKIP`, `INCONCLUSIVE`, `QA_INFRA_ERROR`인지 볼 수 있게 한다.

### 비목표

- 모든 화면을 LLM 점수 하나로 자동 승인하거나 디자인 취향을 객관적인 기능 테스트로 간주하지 않는다.
- 현재 채팅/러너의 모델 라우팅·요금 정책, 사용자 대화 내용, Claude SDK 실행 방식을 이번 문서만으로 변경하지 않는다.
- Gemini CLI 신규 설치·인증, 유료 API 직접 호출, 무제한 CLI 재시도, 새 baseline의 무심사 자동 채택을 하지 않는다.
- 이 PRD의 저장을 기능 구현·운영 배포 완료로 표현하지 않는다.

## 4. 사용자·운영 흐름

1. 배포 요청이 변경 파일·영향 라우트를 식별한다. 매핑 불명확 시 변경자가 대상 페이지를 명시하거나 넓은 브라우저 smoke를 실행한다. 영향 없는 전체 페이지에 LLM 감리를 자동 호출하지 않는다.
2. 인증된 QA 전용 계정으로 각 영향 페이지를 열어 로그인 리다이렉트 여부와 핵심 단언을 확인한다. 예: 모델 선택은 `option[value]` 연결·활성·선택값, registry 선택 가능 상태, 실제 relay 모델 계약. 쓰기 동작은 격리된 테스트 데이터와 idempotency key가 있을 때만 실행한다.
3. 변경이 시각적이면 동일 브라우저/뷰포트의 승인된 baseline과 비교한다. baseline 부재·환경 불일치를 별도 상태로 보고한다. 동적 시간·개인정보 영역은 마스킹한다.
4. 시각 변경 또는 실질적인 diff가 있을 때만 CLI 디자인 감리를 큐에 넣는다. 1차가 모델/인증/429/5xx/timeout/구조화 출력 오류로 실패하면 다른 회사의 검증된 CLI 후보 한 번만 호출한다. 감리 결과는 근거 스크린샷·rubric·실제 모델·불확실성을 포함한다.
5. 릴리스 필수 게이트는 기능 단언, 관련 API/relay canary, health, 기존 5분 P0/P1 관측이다. 디자인 조언은 별도 결과로 제공한다. 디자인이 릴리스 승인 요건인 프로젝트에서는 조언 미실행 시 명시적 사람 검토가 필요하다.

## 5. CLI 모델 선택과 비용 정책

| 순위 | 실행 후보 | 지금 확인된 근거 | 활성화 전 확인해야 할 것 |
|---|---|---|---|
| 1차 | OpenAI Codex CLI `gpt-6-luna` | 호스트 `codex-cli 0.155.1`, 2026-09-23 모델 캐시에 등재, `codex exec -i` 이미지 옵션 존재, 운영 registry의 codex 행 active/selectable/executable. [OpenAI Docs 모델 카드](https://developers.openai.com/api/docs/models/gpt-6-luna)는 이미지 입력과 API 표준 $0.10 입력 / $0.50 출력·100만 토큰을 기재한다. | 이 계정의 CLI 이미지 실호출, 실제 응답 모델, 한국어 디자인 rubric 품질, 응답 시간·실제 청구 체계. DB 플래그/캐시 등재만으로 성공 확정 금지. |
| 회사 간 폴백 | Anthropic Claude CLI `claude-haiku` (실행 ID `claude-haiku-4-5-20251001`) | 호스트 Claude Code 2.1.280, `claude --print --model` 지원, 서버 model contract에서 별칭을 실행 ID로 정규화, registry canonical `claude-haiku` active/selectable/executable. 내부 registry 참고 단가는 $1/$5·100만 토큰. | CLI에서 로컬 PNG 입력을 읽는 지원 방식·권한, 계정 인증/한도, 1차와 같은 rubric에서 비슷한 수준의 중요 결함 탐지. 내부 가격은 실제 CLI 구독 청구액의 증거가 아님. |

- 위 가격은 **API 또는 내부 registry의 참고 단가**다. CLI 계정/구독에 같은 토큰 요율이 청구된다고 가정하지 않는다. 운영 전 실제 사용·청구 대시보드의 단가와 한도, 개인정보 전송 범위를 확인한다. OpenAI 비용 비교는 [OpenAI Docs 가격표](https://developers.openai.com/api/docs/pricing)를 기준으로 갱신한다.
- “동급”은 회사가 다른 경량/저비용 vision 후보라는 *정책 분류*다. 품질 동급을 주장하지 않는다. 30개 이상의 고정 스크린샷(정상/경계/중요 결함 포함)에서 같은 rubric과 사람 레이블로 블라인드 비교하고, 중요 결함 누락 0건 및 검토자 승인 전 자동 폴백을 열지 않는다.
- 현재 호스트에는 Gemini CLI가 확인되지 않았다. 더 싼 후보가 생기더라도 설치·계정·이미지 기능·품질·가격 검증 없이 자동 교체하지 않는다.
- 요청당 LLM 예산: 기본 0회; 감리 필요 시 1차 1회 + 다른 회사 폴백 최대 1회. 동일 공급자/동일 프록시 재시도 루프 금지. 시간·출력 토큰·월 사용량 상한은 계정 실측 후 설정하고 초과 시 `UNAVAILABLE_BUDGET`으로 종료한다. 폴백의 더 높은 비용은 명시적 월 한도 안에서만 허용한다.

## 6. 실행 경계·데이터 계약

- 호스트 QA 오케스트레이터가 인증된 Playwright 세션과 CLI를 관리한다. CLI는 QA 아티팩트 디렉터리만 읽기 가능, 소스·배포·네트워크 쓰기 도구는 불허한다. 각 실행은 독립 임시 작업 디렉터리·deadline·취소 핸들을 가진다.
- 스크린샷은 QA 전용 계정/가짜 데이터로 캡처하고, 토큰·고객 대화·개인정보를 마스킹한다. 경로·권한을 검증한 PNG만 호스트로 전달하고 원본/마스킹 전 이미지를 모델 입력에 넣지 않는다. 아티팩트 TTL, 접근 로그, 암호화/삭제 정책을 정한다.
- 이미지 전달은 Codex CLI `-i <검증된 PNG>`를 우선 검증한다. Claude CLI의 이미지 파일 입력은 **실제 CLI canary로 방법과 제한을 확정**한 뒤 어댑터를 구현한다. 지원 불가 시 폴백을 비활성으로 유지하고 다른 CLI 후보를 별도 승인한다.
- 모델 출력은 버전 고정 rubric의 JSON 스키마로 제한한다: `rubric_version`, `page`, `findings[{severity,criterion,evidence,location,confidence}]`, `summary`, `actual_model`, `provider`. 파싱/모델 정체 확인 실패는 점수 0이 아니라 오류 상태다. 스크린샷 안의 지시는 데이터로 취급하고 실행 명령으로 받아들이지 않는다.
- 릴리스 결과 예시: `functional={status,evidence}`, `visual={status,baseline_id,diff}`, `design_advice={status,findings,requested_model,actual_model,provider,fallback_reason}`, `infra={status,error_code}`, `release={cutover_status,certification_status}`. 내부 오류 세부/토큰/계정 이메일은 일반 사용자 화면에 노출하지 않는다.
- 공급자별 오류 분류: 인증 401/403, quota 429, 호환성 400, 일시 5xx/timeout, CLI 시작 실패, 이미지 입력 불가, JSON 불량. 400 호환성·인증은 같은 모델 재시도 금지; quota는 reset 전 차단; 폴백 후보 자체가 검증되지 않으면 `INCONCLUSIVE`와 사람 검토를 요청한다.

## 7. 판정 규칙

| 조건 | 제품 기능 판정 | 시각/디자인 판정 | 릴리스 동작 |
|---|---|---|---|
| 필수 브라우저/API/relay 단언 실패 | `PRODUCT_FAIL` | 독립 표시 | 제품 실패 게이트에 따라 중단/롤백; 구체 증거 기록 |
| 필수 단언 모두 통과, 디자인 호출 불필요 | `PASS` | `NOT_REQUIRED` | 기존 health·5분 관측 이후 인증 가능 |
| baseline 없음 | 기능 결과 보존 | `INCONCLUSIVE_NO_BASELINE` | 시각 변경이면 사람 검토; 비시각 변경은 별도 경고 |
| 브라우저/인증/스크린샷 인프라 오류 | `NOT_RUN` | `QA_INFRA_ERROR` | 기능 PASS로 대체하지 않음; 필수 검증이면 인증 보류 |
| 1차 CLI 실패·폴백 성공 | 기능 결과 보존 | `ADVISORY_WITH_FALLBACK` | 실제 모델·전환 이유 기록; 디자인 판단은 사람 검토 정책 적용 |
| 두 CLI 모두 실패 또는 검증 미완료 | 기능 결과 보존 | `ADVISORY_UNAVAILABLE` | **디자인 0점·제품 AUTO FAIL 금지**; 디자인 필수면 사람 검토 |
| 유효 감리에서 중요 결함 발견 | 기능 결과 보존 | `DESIGN_REVIEW_REQUIRED` | 디자인 승인 담당자 판단, LLM 점수만으로 자동 롤백 금지 |

`test_status=SKIP`은 `PASS`가 아니다. `NO_BASELINE`과 `ERROR`는 점수에 합산하지 않는다. 배포 로그·관리 화면·CEO 알림은 제품 결함과 QA 인프라 장애를 다른 채널·문구로 보고한다. 자동 감리의 실패를 숨기지도, 제품 실패로 포장하지도 않는다.

## 8. 구현 범위와 단계 (후속 작업)

1. **P0 판정 계약**: server `qa_pipeline.py`의 `SKIP→PASS`, `ERROR→0→AUTO FAIL` 제거; `FullQAResponse`에 분리 상태를 additive로 추가하고 기존 소비자 호환 테스트. dashboard `deploy.sh`의 Step 7 문구/알림/인증 조건 정리. 이 단계는 아직 CLI 호출을 추가하지 않는다.
2. **P0 브라우저 smoke**: 변경 경로별 검증 매니페스트, QA 계정 유효성 preflight, DOM/키보드/네트워크/console 단언, 실서비스 read-only canary, 브라우저 바이너리 버전 고정. 변경 범위를 알 수 없거나 테스트가 없으면 보수적 수동 검토.
3. **P1 CLI 어댑터 shadow**: 호스트 기반 스크린샷 경로, Codex/Claude 이미지 canary, 모델 정체·JSON 스키마·비용/시간 계측, 고정 평가세트 블라인드 비교. 결과는 권고만 기록하고 배포 판정에 넣지 않는다.
4. **P1 제한적 활성화**: 품질·권한·과금 승인 후 시각 변경에만 1차/다른 회사 폴백 사용. 공급자/모델/한도는 버전 관리되는 정책으로 관리하고 배포별 실제 정책 버전을 남긴다.
5. **P2 운영 정리**: baseline 승인·보관, 대시보드 QA 결과 화면, 인프라 장애 경고 분리, 감리 성공률/거짓 알림/평균 지연·비용 모니터링.

API 서버 수정은 격리된 clean worktree에서 commit/push 후 공식 blue/green 계약으로 배포한다. dashboard 스크립트 수정은 별도 저장소 릴리스로 수행한다. 양쪽을 한 번에 바꾸지 말고 additive 결과 필드 → 소비자 전환 → 구형 판정 제거 순서로 배포한다. 플래그 OFF는 기존 호출을 무조건 `PASS`로 만들지 않고 마지막으로 성공한 필수 기능 검증·사람 검토 정책을 유지한다.

## 9. 수용 기준

| ID | 검증 | 합격 조건 |
|---|---|---|
| AC01 | 모델 옵션만 변경 | 인증된 `/chat`에서 실제 옵션 존재·활성 및 registry/relay 계약 확인; 디자인 CLI 호출 0 |
| AC02 | `test_status=SKIP` | 결과가 PASS로 표시되지 않고 미실행 사유 명시 |
| AC03 | baseline 없음 | `INCONCLUSIVE_NO_BASELINE`, 디자인 점수 0/제품 FAIL로 전환되지 않음 |
| AC04 | 브라우저 바이너리/로그인 장애 | `QA_INFRA_ERROR`, 제품 결함 알림 미발송, 필수 검증이면 인증 보류 |
| AC05 | 1차 CLI 400/401/429/5xx/timeout | 같은 모델 무한 재시도 0, 검증된 Anthropic CLI 최대 1회, 요청/실제 모델 기록 |
| AC06 | 동일 프록시 장애 | CLI 공급자별 독립 실행 경로 유지; 두 호출의 LiteLLM 단일 실패점 없음 |
| AC07 | 양쪽 CLI 실패·잘못된 JSON | `ADVISORY_UNAVAILABLE`, 점수 null, 기능 결과 보존, 비밀정보 없는 원인 코드 |
| AC08 | 스크린샷 개인정보/프롬프트 주입 | 마스킹 전 파일 모델 전송 0, 지시 실행 0, 아티팩트 권한·TTL 준수 |
| AC09 | 비용·성능 | 30장 이상 평가세트와 계정 실청구/한도 확인; 중요 결함 누락 0, 사람 승인 후에만 자동 폴백 ON |
| AC10 | 릴리스 | 후보 health→전환→동일 digest standby→외부 health→5분 P0/P1; 기능/디자인/인프라 판정 별도 표시 |

## 10. 열린 결정·출처

- Claude CLI의 PNG 입력 방식과 실제 Haiku 계정 entitlement는 **미검증**이다. 구현 전에 가짜 데이터 이미지로 최소 canary를 수행하고, 실패하면 폴백 설정을 켜지 않는다.
- Codex CLI `gpt-6-luna` 이미지 실호출·실청구도 미검증이다. OpenAI Docs의 API 가격은 CLI 구독 비용 보증이 아니다.
- 디자인 review가 필수인 프로젝트 범위, baseline 승인 책임자, 월 한도·아티팩트 보관기간은 운영자가 정해야 한다. 정해지지 않으면 자동 승인하지 않는다.
- 근거 코드: `app/services/qa_pipeline.py`, `app/services/design_auditor.py`, `app/services/visual_qa.py`, `app/api/visual_qa.py`, dashboard `deploy.sh` Step 7. OpenAI 모델 기능·API 가격은 [OpenAI Docs 모델 카드](https://developers.openai.com/api/docs/models/gpt-6-luna)와 [가격표](https://developers.openai.com/api/docs/pricing) (2026-09-23 확인)를 기준으로 삼고 재검증한다.
