# M1 LLM 운영 기준선 — 2026-09-29

`python3 scripts/llm_baseline_verify.py`는 인자 없이 읽기 전용 관측을 수행하고 JSON 한 줄을 출력한다. `ok=false`이면 종료코드 1이다. 관측 실패도 미완료로 처리한다.

## 판정

| 경로 | 측정 및 통과 조건 |
| --- | --- |
| 릴레이 계약 | `127.0.0.1:8199/health`의 `claude_model_contract.models`와 파일의 `EXACT_MODEL_IDS`를 대조한다. 파일에만 있는 ID가 0개여야 한다. |
| API 슬롯 | `aads-server`, `aads-server-green`의 Docker 이미지 ID와 번들 `/usr/local/bin/claude-aads --version` 출력이 각각 같아야 한다. |
| 모델 카탈로그 | `information_schema.columns`에서 `tier`, `fallback_group`을 먼저 확인한다. 없으면 집계 SQL을 실행하지 않고 `status=column_missing`과 누락 컬럼명을 보고한다. 있으면 선택 가능하지만 미검증인 행과 활성 행의 두 컬럼별 NULL 건수를 센다. 세 수가 모두 0이어야 한다. |
| 탐지 신선도 | `llm_model_discovery_runs`에서 공급자별 최신 실행의 status와 마지막 `ok` 시각을 보고한다. 활성 모델의 공급자도 포함한다. 최신 status가 `ok`이고 마지막 성공이 24시간 이내여야 한다. 오류 원문 대신 `models_api_unavailable`, `provider_disabled`, `http_4xx`, `http_5xx`, `timeout`, `unclassified` 중 안전 분류만 출력한다. |

실행 시각과 경과 시간은 UTC 기준이다. `verified`는 레지스트리의 검증 완료 상태이며, `discovered`·`review_required`·`unknown` 등은 미검증으로 센다. 수집기는 DB를 읽기만 한다.

## 2026-09-29 실제 실행

작업 호스트 `vmi3267555`에서 수정된 `python3 scripts/llm_baseline_verify.py`를 1회 실행했다. 2026-09-29 06:26:19 UTC, 종료코드는 **1**이었다. 아래는 실제 stdout이다.

```json
{"checked_at": "2026-09-29T06:26:19.429413+00:00", "checks": {}, "errors": {"catalog": "CalledProcessError", "discovery": "CalledProcessError", "relay_contract": "URLError", "slots": "CalledProcessError"}, "incomplete": ["catalog", "discovery", "relay_contract", "slots"], "ok": false}
```

`server-116`에 읽기 전용 SSH 접속도 시도했으나 `socket: Operation not permitted`로 차단됐다. 이 실행은 운영 호스트에 도달하지 못했으며, 다음 값은 추정하지 않았다.

| 항목 | 이번 실행에서 확인한 값 | 운영 실측 상태 |
| --- | --- | --- |
| 릴레이 `/health` 대 파일 계약 | 작업 트리 `EXACT_MODEL_IDS`는 20개이며 `claude-sonnet-5-5`를 포함한다. | 실행 릴레이 목록은 **미측정** (`URLError`). 착수 브리프에는 오늘 14:5x KST 관측으로 해당 ID가 릴레이에 없는 stale 상태라고 기록돼 있으나 이 실행으로 재확인하지 못했다. |
| 양 슬롯 이미지 digest | 없음 | **미측정** (`CalledProcessError`; 작업 호스트에 대상 컨테이너 없음). 같음/다름 판정 불가. |
| 카탈로그 활성 NULL `tier` / `fallback_group` 건수 | 없음 | 두 건수 모두 **미측정** (`CalledProcessError`). 착수 브리프의 오늘 14:5x KST 운영 DB 조회에는 두 컬럼이 존재한다고 기록돼 있으나 NULL 건수는 포함되지 않았다. |
| 공급자별 마지막 탐지 성공 경과 | 없음 | 공급자 목록과 각 경과 분 모두 **미측정** (`CalledProcessError`). |

따라서 네 경로 모두 이번 실행의 판정은 **미완료**다. 운영 실측값을 채우려면 운영 컨테이너와 릴레이에 접근 가능한 호스트에서 같은 수집기를 실행해야 한다.

`bash scripts/run_unit_tests.sh tests/unit/test_llm_baseline_verify.py`는 기준 이미지 부재로 종료코드 **2**였다. 출력은 다음과 같다.

```text
[run_unit_tests] 기준 이미지를 찾지 못했습니다 (5초 간격 6회 재시도) — 시도한 후보와 실패 사유:
  - container:aads-server -> no such container
[run_unit_tests] docker ps --filter name=aads-server 요약:
```

별도로 `python3 -m pytest -q tests/unit/test_llm_baseline_verify.py`는 **14 passed, 1 warning**이었다. 경고는 `asyncio_mode` 설정을 현재 pytest가 인식하지 못한다는 내용이다.
