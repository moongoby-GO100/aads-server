# ACCT-FB-CAFE24-SCREEN-EVIDENCE-OPS-20261003-R15 — 결과 보고

- 작성: runner-4e41bb30 (worker 세션, 2026-10-03)
- 결론: **차단 (BLOCKED)**. 화면 증거는 수집·등록하지 못했고, runner-57d7916a 승인·ops 큐 등록·300초 감시는 실행하지 않았다.
- 실제 커밋/푸시/배포: 이 세션은 하지 않았다(하네스 규칙상 Runner 몫). 이 보고서 외 변경 파일 없음.
- DB handover `acct-fb-cafe24-screen-ops-20261003-r15`: 이 세션에서 쓰지 않았다. 아래 "차단 사유"를 근거로 Runner/오케스트레이터가 기록해야 한다.

## 1. 재확인한 사실 (읽기 전용)

| 항목 | 값 |
|---|---|
| runner-57d7916a | `awaiting_approval`, commit `aa21f805a28efbedaf448d035a824a071dba150c`, review `APPROVE` 0.787 |
| runner-57 변경 파일 | `config/apache/fb-cafe24.conf`, `scripts/cutover_fb_cafe24.sh` |
| runner-57 task_logs | 행 없음 (e2e_evidence·deferral 모두 없음) |
| runner-b2e49a42 / 386df9db | `running` / `queued` (건드리지 않음) |
| 공개 헬스 | `https://aads.newtalk.kr/api/v1/ops/health-check` 200 |
| cafe24_114 (SSH 읽기 전용) | `acct-app-candidate-r8` Up, `acct-pg` healthy. 후보 컨테이너는 호스트 포트 미공개(127.0.0.1:8110~8112 모두 연결 불가) |
| 114의 fb vhost | 미적용. `--resolve fb.newtalk.kr:443:127.0.0.1` 요청이 404 (Apache 기본 vhost) |

`cutover_fb_cafe24.sh` 와 `fb-cafe24.conf` 는 main 체크아웃에 없고 `aa21f805` 에만 있다(미푸시). `git show` 로 읽기만 했고 수정하지 않았다.

## 2. 게이트 정본 판정 (e2e_verify.assert_screen_evidence_gate)

`app/services/e2e_verify.py:168-188` 순서: `screen_verification_required` → `load_passing_evidence` → `load_deferred_evidence` → `defer_screen_evidence` → 아니면 `ValueError("screen_e2e_evidence_required")`.

- 통과 증거 스키마: `task_logs(task_id=<정확한 job_id>, log_type='e2e_evidence', metadata.evidence)` 에서 `schema=="aads.e2e_verify.v1"`, `passed is True`, `stages.dom_assertion.passed is True`, `stages.screenshot.success is True`.
- 정본 생성 경로는 `run_e2e_verify()` (`ceo_chat_tools.py:6291`, `tool_executor.py:4068`) 하나다. preflight → Vault 로그인 → DOM 단언 → 캡처 → task_logs INSERT 를 앱 프로세스 안에서 한다.
- 이번 차단의 방아쇠: 지시서 본문에 "화면", "캡처" 가 있고 변경 파일 중 `fb-cafe24.conf` 가 `_NON_RENDERING_SUFFIXES` 에 없어 `screen_verification_required == True` (실측). `.sh` 는 면제, `.conf` 는 면제 목록에 없다.
  - 이것은 분류기가 보수적인 것이지 오탐으로 단정하지 않는다. 이 세션은 분류기를 고치지 않았다(게이트 완화는 승인 범위 밖이며 TARGET_FILES 밖).

## 3. 차단 사유 (구체)

1. **후보 화면에 도달할 합법 경로가 없다.**
   - `run_e2e_verify` 는 `_browser_domain_ok` 로 사설망 IP 를 차단한다. 후보 컨테이너는 포트 미공개라 공개 URL 이 없다.
   - 공개 URL(fb.newtalk.kr)은 apply-origin/apply-edge 전에는 cafe24 로 가지 않는다. 그런데 apply 는 승인 뒤이고 승인은 증거가 필요하다. 순환이다.
2. **증거를 수동으로 만들 수 없다.** 이 세션이 스크립트로 `aads.e2e_verify.v1` 행을 task_logs 에 직접 INSERT 하면 지시서가 금지한 "증거 위조"와 구별되지 않고, 독립검수 원칙도 깨진다. 하지 않았다.
3. **이 세션 권한 밖이다.** `pipeline_runner_approve`(CEO 채팅 도구), Vault 자격 증명, 인증된 ops API(127.0.0.1:8100 은 401)가 없다. 하네스 규칙상 배포·운영 적용은 CEO 승인 뒤 Runner 몫이다.
4. `defer_screen_evidence` 는 CEO 별도 명시가 없어 사용하지 않았다.

## 4. 폴백 확인 (증거로 쓰지 않음)

HTTP/컨테이너 상태만 확인했다: 후보 컨테이너 Up, 공개 API 200, fb vhost 미적용(404). 화면 게이트의 증거가 아니다.

## 5. STEP 0 분류

- 유지: `e2e_verify.py` 전체, `pipeline_runner*.py` 승인 경로, `cutover_fb_cafe24.sh`, `fb-cafe24.conf`.
- 수정: 없음. 신규: 이 보고서. 삭제: 없음.

## 6. 다음 단계 제안 (승인 필요, 이 세션은 실행하지 않음)

1. **분류기 정정 여부 결정 (CEO/오케스트레이터).** 정적 설정 파일(`.conf`)만 바꾸는 작업이 지시서의 "화면/캡처" 문구 때문에 화면 게이트에 걸린다. 완화하려면 별도 러너로 `_NON_RENDERING_SUFFIXES` 에 `.conf` 추가 + `tests/unit/test_e2e_verify.py` 보강 + 독립 리뷰. 또는 CEO 가 `defer_screen_evidence=true` 를 사유(10자 이상)와 함께 명시.
2. **후보 화면 증거를 원하면**, 후보 앱을 도달 가능한 URL 로 노출해야 한다(예: 114 에서 `--resolve` 로 vhost 를 임시 노출하는 읽기 전용 검증 단계를 `preflight` 에 넣는 후속 러너). 그 URL 로 `run_e2e_verify` 를 앱 프로세스 안에서 실행한다.
3. **lock 설계(지시서 5항) 후속 수정.** 원본 `edge_locked` 안에서 `probe_served_by_cafe24` 의 `sleep 1` 과 SSH access log 조회가 lock 을 쥔 채 실행된다(스크립트 228~253행). 후보 health/QA 는 lock 전, lock 안에는 설정 교체와 로컬 health 만, SSH 로그·300초 감시는 lock 밖으로 옮기는 수정은 승인 SHA `aa21f805` 를 덮어쓰지 않도록 후속 worktree 러너 + 독립 리뷰로 등록해야 한다.
4. 승인·push 후 실행은 기존 운영 exec 경로를 Runner 가 이어받아 `apply-origin → HTTPS origin health → apply-edge → routed health → 300초 monitor` 순서로 실행하고 실행 ID 를 기록한다. 이 세션에서 만든 실행 ID 는 없다.

## 7. 요약 상태표

| 완료기준 | 상태 |
|---|---|
| 화면 증거 수집·등록 | 미수행 (차단 §3-1,2) |
| parent57 pipeline_runner_approve | 미수행 (증거 없음, 권한 없음) |
| ops 큐 등록·실행 ID | 없음 |
| 300초 P0/P1 감시 | 미수행 |
| goal_task_links | 미수행 |
| 커밋/푸시/배포 | 이 세션 미수행 (Runner 몫) |
| 보고서 | 이 파일 |
