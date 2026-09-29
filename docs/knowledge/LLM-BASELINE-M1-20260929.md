# M1 LLM 운영 기준선 — 2026-09-29

`python3 scripts/llm_baseline_verify.py`는 인자 없이 읽기 전용 관측을 수행하고 JSON 한 줄을 출력한다. `ok=false`이면 종료코드 1이다. 관측 실패도 미완료로 처리한다.

## 판정

| 경로 | 측정 및 통과 조건 |
| --- | --- |
| 릴레이 계약 | `127.0.0.1:8199/health`의 `claude_model_contract.models`와 파일의 `EXACT_MODEL_IDS`를 대조한다. 파일에만 있는 ID가 0개여야 한다. |
| API 슬롯 | `aads-server`, `aads-server-green`의 Docker 이미지 ID와 번들 `/usr/local/bin/claude-aads --version` 출력이 각각 같아야 한다. |
| 모델 카탈로그 | `is_selectable=true`이면서 `verification_status != 'verified'`인 행, 활성 행 중 `tier` 또는 `fallback_group`이 NULL인 행을 센다. 두 컬럼이 스키마에 존재하는지도 확인한다. 컬럼이 없으면 직접 참조로 SQL을 실패시키지 않고 해당 컬럼의 존재 여부를 `false`로 보고해 미완료로 판정한다. 세 수 모두 0이고 두 컬럼이 모두 있어야 한다. |
| 탐지 신선도 | `llm_model_discovery_runs`에서 공급자별 최신 실행의 status와 오류 유무, 마지막 `ok` 시각을 보고한다. 활성 모델의 공급자도 포함한다. 최신 status가 `ok`이고 마지막 성공이 24시간 이내여야 한다. 오류 문자열은 저장소에 연결 정보나 토큰이 들어갈 수 있어 `[redacted]`로 표시한다. |

실행 시각과 경과 시간은 UTC 기준이다. `verified`는 레지스트리의 검증 완료 상태이며, `discovered`·`review_required`·`unknown` 등은 미검증으로 센다. 수집기는 DB를 읽기만 한다.

## 2026-09-29 실제 실행

작업 호스트에서 `python3 scripts/llm_baseline_verify.py`를 1회 실행했다. 종료코드는 **1**이었다. 아래는 실제 stdout이다.

```json
{"checked_at": "2026-09-29T06:08:45.028513+00:00", "checks": {}, "errors": {"catalog": "CalledProcessError", "discovery": "CalledProcessError", "relay_contract": "URLError", "slots": "CalledProcessError"}, "incomplete": ["catalog", "discovery", "relay_contract", "slots"], "ok": false}
```

릴레이 HTTP 연결과 Docker 컨테이너 접근이 이 작업 환경에서 불가해 모델 목록, 슬롯 이미지·CLI, DB 행 수와 탐지 시각을 실측하지 못했다. 따라서 네 항목은 모두 **미완료**다. `claude-sonnet-5-5`의 릴레이 누락 여부도 이 실행으로 확인되지 않았다.

`bash scripts/run_unit_tests.sh tests/unit/test_llm_baseline_verify.py`는 기준 이미지 부재로 종료코드 **2**였다. 출력은 다음과 같다.

```text
[run_unit_tests] 기준 이미지를 찾지 못했습니다 (5초 간격 6회 재시도) — 시도한 후보와 실패 사유:
  - container:aads-server -> no such container
  - container:aads-server-green -> no such container
[run_unit_tests] docker ps --filter name=aads-server 요약:
```

별도로 `python3 -m pytest -q tests/unit/test_llm_baseline_verify.py`는 **9 passed, 1 warning**이었다. 경고는 `asyncio_mode` 설정을 현재 pytest가 인식하지 못한다는 내용이다.
