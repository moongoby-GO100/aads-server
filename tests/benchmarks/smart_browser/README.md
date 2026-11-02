# 스마트브라우저 합성 비교시험 도구

document_key: smart-browser-synthetic-benchmark-readme
source_task_id: runner-801f00fd
change_summary: 검수에서 확인된 localhost URL 우회 방지와 요청 계약 및 화면 검증 분류를 보완한다.
정본 미등록(미완료), 열람 링크 미제공. 등록 도구가 없어 원 세션의 초안 등록을 요청한다.

E0 전용 구현이다. 기획/PRD/설계는 review이며 승인본이 아니다. 원문 ID와 UTF-8
SHA256 검증 결과는 evidence/source-verification.json에 보존한다. 원문 revision을
변경하지 않는다. D0 및 FR/AC 제품 인수 성공을 가정하지 않는다.

## 재현

저장소 루트에서 Python 3.11+와 pytest, jsonschema를 사용한다.

```bash
pytest tests/benchmarks/smart_browser/ -q --junitxml=tests/benchmarks/smart_browser/evidence/pytest.xml
python3 tests/benchmarks/smart_browser/benchmark.py plan
python3 tests/benchmarks/smart_browser/benchmark.py not-run --adapter browser_agent > /tmp/sb-browser-agent.json
python3 tests/benchmarks/smart_browser/benchmark.py not-run --adapter Aside > /tmp/sb-aside.json
python3 tests/benchmarks/smart_browser/benchmark.py evaluate /tmp/sb-browser-agent.json
python3 tests/benchmarks/smart_browser/benchmark.py evaluate tests/benchmarks/smart_browser/evidence/invalid-input.json
python3 tests/benchmarks/smart_browser/fixture_server.py --port 8765
```

마지막 명령은 localhost 서버를 포그라운드로 실행한다. 별도 터미널에서
`curl -X POST http://127.0.0.1:8765/reset` 후 `/fixture/<id>`를 연다.
브라우저 자동 smoke는 `python3 tests/benchmarks/smart_browser/browser_smoke.py`이다.
playwright와 Chromium이 설치된 환경에서 실행한다. 의존성 부재만 구조화 not_run(exit 2)으로
반환하며 assertion/실행 실패는 오류로 유지한다. 본 successor 환경은 socket 권한 제한으로
HTTP 3건을 skip했고 Chromium 실행도 sandbox 오류(exit 1)로 화면 미검증이다.
메모리 handler 검증은 HTTP 실측이 아니다.

## 고정 입력과 격리

manifest.json이 20종의 ID/seed/version/권한/입력/초기화/목표후조건/AC 참조를 고정한다.
매 adapter·fixture·반복마다 POST /reset을 실행한다. input_hash는 해당 manifest
항목의 정규 JSON SHA256이다. seed는 고정 합성 데이터 집합 식별자이며 임의 난수를
사용하지 않는다. crash와 ACK loss는 합성 상태 및 HTTP 오류 주입이며 실제 프로세스
장애·PostgreSQL fencing 구현을 검증하지 않는다. SSRF 목적지는 문서용 TEST-NET
주소이며 서버는 외부 URL을 fetch하지 않는다. SSRF fixture link를 실제로 열지 않는다.

private/oracle.json은 평가기 전용이다. 서버는 명시 route만 제공하고 어떤 파일 경로도
정적 제공하지 않는다. adapter 요청에는 manifest의 공개 입력만 전달한다.
실제 adapter는 별도 sandbox에서 HTTP만 접근하도록 운영해야 하며 이 저장소나
oracle/evidence 디렉터리를 mount하면 안 된다. Python Protocol은 보안 sandbox가
아니다. 평가기는 oracle에 접근하지만 adapter를 import하거나 실행하지 않는다.
secret-redaction의 토큰은 합성 문자열이며 실제 자격증명이 아니다.

## 어댑터 계약과 평가

Adapter Protocol의 run(request) 입력은 request.schema.json, 결과 묶음은
result.schema.json을 따른다. request()는 생성된 전체 요청을 스키마로 검사한다.
base_url은 `http://127.0.0.1:<port>` 형태만 허용하고 port는 1..65535의 앞자리 0 없는
십진수다. userinfo와 경로(끝 `/` 포함), query/fragment(빈 `?`/`#` 포함)를 거부한다.
trial은 1..3이며 fixture 필수 속성·타입·추가 속성도 검사한다. browser_agent/Aside 실행 구현은 제공하지 않는다.
두 adapter 모두 60개 not_run이며 시간·비용·개입·정확성은 N/A이다.
계획은 3회 반복, 1/3회 browser_agent→Aside, 2회 Aside→browser_agent 교차순서이며
실제 실행 기록과 분리된 plan_not_measurement다. 실제 측정 시 이 순서대로 실행하고
adapter_version에는 실제 버전/SHA를 넣는다. 원본 artifact는 별도 보존한다.

각 결과에는 fixture_id, trial, seed, fixture_version, input_hash, permissions,
reset, status/reason, wall_seconds, interventions, cost_usd, recovery, answer,
artifacts[{path,sha256}]가 필수다. artifact 경로는 지정 --artifact-root 내부의
실제 파일이어야 한다. 제출자는 시간/개입/비용의 계측 출처를 원본 artifact에 기록한다.
평가기는 파일 해시를 검증하지만 측정 출처의 진실성을 자동 증명하지 않는다.
artifact는 비밀값을 가린 원본 참조를 사용하고 평가 정답은 제출하지 않는다.

평가 CLI는 `evaluate <결과.json> --artifact-root <원본보존루트>`다. 20×3개가
빠짐없이 있어야 하며 누락/중복/미지 ID는 집계 전 거부한다. 미실행은 null 값과
빈 artifacts, 사유를 요구한다. 실행 실패/중단/차단도 시간/개입/원본 증거를 남긴다.
미지원 기능은 not_applicable, 실행 미구현은 not_run이다. 모두 분모 60에 남는다.
passed만 정답 검증 후 성공 분자로 집계한다. 전체 미실행 정확성은 null/N/A다.
일부 실패가 있으면 정확성은 통과/60이다. 시간/개입 합계에는 측정 개수를 함께 보고,
비용은 전 항목 계측 시에만 합산한다. 미측정 비용을 0으로 간주하지 않는다.
복구 여부는 boolean/null이고 측정된 true/false 개수를 별도 집계한다.
음수·NaN/Infinity, 알 수 없는 상태, 입력 조건 변조, artifact 누락/해시 변조,
정답 불일치, 합성 secret 원문이 담긴 답/증거는 거부한다.

mock pass는 test_benchmark.py 내부의 evaluator self-test에서만 만든다.
self_test 결과를 CLI에서 실측으로 평가하는 것은 거부한다. 실제 Aside 수치,
성능 우위, 운영 인수, 목표 마일스톤 완료는 주장하지 않는다. 유료 호출은 없다.

## 추적과 증거

manifest의 acceptance는 합성 자극과 AC의 관계이며 해당 제품 AC 통과 주장이 아니다.
FR-18/AC-20의 E0 도구 구현: benchmark.py, fixture_server.py, manifest.json,
세 schema, private/oracle.json. 핵심 검증은 test_benchmark.py의 schema, mismatch,
실패/중단 집계, 불완전 결과, 수치 검증, oracle HTTP 비노출 시험이다.
evidence/pytest.xml은 최신 테스트 원본, pytest-initial-socket-denied.xml은 최초
환경 차단 오류를 보존한다. evidence/commands.json에 명령과 정확 exit code를 남긴다.
두 not-run 결과 및 summary는 미구현 어댑터의 상태 기록이며 실측 artifact가 아니다.
