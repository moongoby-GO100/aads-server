# AADS 도구 전수 분석 + 개선 보고서

- 작성: 2026-05-06 09:47 KST
- 작성자: AADS CTO 세션
- 범위: AADS Backend (서버68) 기준 도구 정의/실행/등록 3계층, MCP 브릿지, 레거시 dispatch
- 근거: `app/api/ceo_chat_tools.py`, `app/services/tool_registry.py`, `app/services/tool_executor.py`, `mcp_servers/aads_tools_bridge.py`, PostgreSQL `tool_results_archive` / `pipeline_jobs`
- 측정 기준: 코드 정적 분석 + DB 실측. 추정값은 [추정] 표기.

---

## 1. 결론 요약

1. AADS는 이미 “서버/DB/SSH/Git/브라우저/AI 분석/Pipeline Runner” 6대 축의 도구가 갖춰진 상태이지만, **3계층(MCP 노출 / 레지스트리 / 실행기)이 서로 다른 셋을 노출**해 “보이는데 안 되는 도구”와 “있는데 안 보이는 도구”가 양쪽으로 발생한다.
2. **부족한 영역은 ‘디바이스 원격제어’, ‘구조화된 운영 명령’, ‘스킬 자동화/체크리스트’, ‘LLM 코스트·라우팅 가시화’, ‘외부 통신(메일/알림 채널)’의 다섯 축**이다.
3. **현재 도구의 핵심 문제는 ① 3계층 불일치 ② raw shell 의존 ③ 검증 불가능한 결과(tool_results_archive 비어 있음) ④ 일부 dispatch 오매핑/누락 ⑤ 스킬 부재**로 정리된다.
4. 즉시 처리 가능한 P0/P1 5건과 중기 로드맵 P2/P3 7건을 제시한다.

---

## 2. 현재 도구 체계 — 3계층 전수 비교

| 계층 | 파일 | 역할 | 도구 수 |
|------|------|------|---------|
| ① MCP 노출 | `app/api/ceo_chat_tools.py` `TOOL_DEFINITIONS` | Claude Code CLI에 보이는 도구 | 54개 |
| ② 레지스트리 | `app/services/tool_registry.py` `_TOOLS + _DEFER_LOADING` | CEO Chat(Anthropic API)에 등록 | 92개 (상시 40 + 온디맨드 52) |
| ③ 실행기 | `app/services/tool_executor.py` `_dispatch` | 실제 함수 매핑 | 98개 (별칭/레거시 포함) |

### 2.1 카테고리 분포 (실측)

| 카테고리 | 수 | 대표 도구 |
|----------|-----|-----------|
| 정보/조회 | 18 | query_db, query_project_database, query_timeline, query_decision_graph, list_remote_dir, read_remote_file, read_github, recall_tool_result |
| 외부 검색/리서치 | 9 | search_naver, search_naver_multi, search_kakao, gemini_grounding_search, search_searxng, fetch_url, jina_read, crawl4ai_fetch, deep_crawl |
| 코드 분석/AI | 8 | code_explorer, semantic_code_search, analyze_changes, search_all_projects, run_agent_team, run_debate, fact_check, fact_check_multiple |
| 액션/실행 | 14 | write_remote_file, patch_remote_file, run_remote_command, execute_sandbox, git_remote_*, pipeline_runner_submit/status/approve/submit_batch |
| 알림/통신 | 4 | send_telegram, send_alert_message, evaluate_alerts, export_data |
| 브라우저 | 7 | browser_navigate, browser_snapshot, browser_screenshot, browser_click, browser_fill, browser_tab_list, capture_screenshot |
| 스케줄/태스크 | 6 | schedule_task, unschedule_task, list_scheduled_tasks, check_task_status, read_task_logs, terminate_task |
| 이미지 | 1 | generate_image |
| 시각QA | 1 | visual_qa_test |

### 2.2 3계층 불일치(핵심 Gap)

- **레지스트리 92 vs 실행기 98**: 6개 dispatch는 등록되어 있지만 LLM이 호출할 수 없다.
- **MCP 54 vs 실행기 98**: 외부 CLI(Claude Code, MCP 클라이언트)에서는 44개 도구가 보이지 않는다.
- **별칭/오매핑**: tool_executor에서 `inspect_service` → `health_check`로 잘못 매핑된 케이스가 코드 검토 중 발견됨. (P0)
- **deferred 로딩**: 52개가 “온디맨드 로딩”인데, 모델이 모르면 호출 자체를 시도하지 않아 사실상 사용률이 낮다.

---

## 3. 도구 사용/품질 실측

### 3.1 DB 사용 이력

- `tool_results_archive`: 최근 14일 저장 호출 0건. (캐시·아카이빙 경로가 사실상 비어 있음)
- 결과: 도구 실패율/p95 latency를 데이터 기반으로 산출 불가. 현재는 “체감/로그” 기준만 가능.

### 3.2 시스템 프롬프트 명시 오류율(체감 기반)

| 도구 | 명시 오류율 | 영향 |
|------|-------------|------|
| `patch_remote_file` | 72.6% | 줄번호 포함/컨텍스트 mismatch로 실패 빈발 |
| `terminate_task` | 60.6% | done/error 상태에서 호출 시 실패 |
| `run_remote_command` | 40.9% | `;` `&&` `bash -c` 차단 미인지 |
| `write_remote_file` | 2.4% | 안정 |

→ 출처: 시스템 프롬프트 “도구 오류율 전략” 항목. 실제 측정 기반 데이터는 부재(아카이브 비어 있음).

### 3.3 Pipeline Runner 안정성

- 최근 50개 job 중 `error` / `deploy_timeout` 비율 [추정] 높음.
- runner-36c5b7bc 8.5h 스톨, runner-4f922625 deploy_timeout 등 장기 정체 사례 다수.
- 검수 트리거 dedup 가드 없음 → 같은 job에 대한 자동 트리거가 6회 반복된 사례 존재.

---

## 4. 현재 도구 — 핵심 문제점 (5대 카테고리)

### 4.1 일관성 문제 — 3계층 불일치
- 같은 기능이 MCP·레지스트리·실행기에서 다른 이름/시그니처로 존재.
- “LLM은 도구 X로 호출 → 실제로는 별칭 Y로 dispatch → 인자 검증 누락” 패턴 존재.
- 레지스트리 deferred 로딩이 외부 ToolSearch에서만 작동, 내부 LLM에는 일부 미노출.

### 4.2 운영 안전성 문제 — raw shell 의존
- `run_remote_command`는 화이트리스트 기반이지만 다단 명령(`;` `&&`) 차단 때문에 한 번에 끝낼 수 있는 작업이 여러 호출로 쪼개진다.
- `docker compose up -d`(전체) 같은 위험 명령은 차단되지만, 부분 재시작/롤백/마이그레이션을 위한 **타입드 ops 도구**가 없다.
- 결과: 매번 “명령 만들기 → 실행 → 결과 파싱” 패턴이 반복되고, AI가 명령 합성에서 실수할 여지가 크다.

### 4.3 관측성 문제 — 결과 아카이브가 비어 있음
- `tool_results_archive` 저장 경로가 코드상 존재하지만 실제로 14일간 0건. 이유는 미확인.
- 이 때문에 “어떤 도구가 얼마나 실패하는지”, “어떤 응답이 캐시 가능한지” 데이터 기반 판단 불가.
- `recall_tool_result`도 신호가 없어 사실상 무용.

### 4.4 실행기 품질 문제 — 오매핑/누락
- `inspect_service` → `health_check` 오매핑 가능성. (P0 검증 후 패치)
- 일부 도구가 실행기에는 있으나 정의에 빠져 있어 “LLM은 모름”.
- 일부 deferred 도구 schema가 ToolSearch로 가져온 뒤에야 호출 가능 → 한 번 더 cache miss를 유발.

### 4.5 능력 결손 영역
- **디바이스 원격제어**: AADS는 Android Agent를 운영하지만, MCP 도구로 “SMS 보내기, 통화기록 가져오기, 위치, 사진 동기화, 권한 상태 조회”를 직접 호출할 수 없다. 모두 디바이스 API + 토큰 기반 수동 호출 필요.
- **외부 통신**: 메일/카카오톡/슬랙/디스코드/SMS(서버 측) 송신 도구가 부재. `send_telegram` 단일 채널만 존재.
- **코스트/모델 가시화**: LiteLLM/Anthropic 비용·라우팅·실패율을 LLM이 직접 조회할 수 있는 도구 부재.
- **스킬 시스템**: 반복 워크플로(예: “프로젝트 헬스체크 + 보고서 저장 + Telegram 알림”)를 이름만으로 부르는 스킬이 부재. 매번 도구 5~10개를 손으로 조립해야 한다.
- **체크리스트/완료 기준 자동화**: WRAP 파일/HANDOVER/검증 명령을 강제하는 ‘complete_task’ 류 도구가 없다.

---

## 5. 부족한 도구 — 우선순위 제안

| 우선순위 | 도구/스킬 | 목적 | 비고 |
|----------|-----------|------|------|
| P0 | `tool_layer_audit` | 3계층 정의/레지스트리/실행기 diff를 실시간 출력 | 신규 도구 추가 시 자동 검사 가능 |
| P0 | `inspect_service` (수정) | 컨테이너/포트/health/최근 로그 일괄 조회 | 현재 health_check로 잘못 매핑 의심 |
| P0 | `device_command` | Android Agent 명령(SMS/연락처/통화/사진/권한) 단일 도구 | 페어링 토큰 자동 사용 |
| P1 | `deploy_safe` | 무중단 배포 표준화(reload-api / bluegreen / restart-single) | docker compose up -d 전체 차단 |
| P1 | `db_safe_write` | 트랜잭션·전후 count·dry-run 강제 | 직접 SQL UPDATE/DELETE 차단 |
| P1 | `tool_metrics` | 최근 24h/7d 도구 호출수/실패율/p95 latency | tool_results_archive 정상화 후 |
| P1 | `notify_channel` | 다채널(Telegram/Slack/Email/SMS) 통합 송신 | 채널·우선순위·중복방지 |
| P2 | `cost_inspect` | LiteLLM/Anthropic 비용·실패율·라우팅 분기 통계 | 비용 이상치 감지 연동 |
| P2 | `prompt_layer_audit` | L1~L5 적용 상태/충돌·우선순위 검증 | provenance 기반 |
| P2 | `runner_guard` | runner 동시성/중복 트리거/스톨 감시 + dedup | dedup 가드 도구화 |
| P3 | `complete_task` | WRAP 작성·HANDOVER 갱신·체크리스트 검증 강제 | R-001 위반 차단 |
| P3 | `skill_run` | 등록된 스킬(재사용 워크플로) 실행 | aads-skills 디렉터리 기반 |

---

## 6. 부족한 스킬 — 우선순위 제안

| 우선순위 | 스킬 | 단계 |
|----------|------|------|
| P0 | `aads_health_full` | 컨테이너→API→DB→runner→최근 alerts→요약 |
| P0 | `runner_finalize_safe` | 빌드→APK 검증→commit→push→배포→health |
| P0 | `device_provision` | APK 빌드→서명→fresh APK→다운로드 라우트 검증 |
| P1 | `prompt_layer_check` | L1~L5 + provenance + 실제 적용 검증 |
| P1 | `tool_consistency_check` | 3계층 diff + dispatch 누락 + 별칭 일치 검증 |
| P1 | `db_migration_safe` | 백업→마이그레이션→count 검증→rollback 안전망 |
| P2 | `cost_audit_daily` | 일일 모델/도구/러너 비용 합산·이상치 보고 |
| P2 | `report_save` | 분석 결과 표준 포맷으로 reports/ 저장+커밋 메모 |
| P2 | `incident_postmortem` | 장애 발생→타임라인→원인→재발방지 자동 작성 |
| P3 | `weekly_review` | 주간 진행/지시·미해결 이슈/HANDOVER diff 종합 |

---

## 7. 실행 로드맵

| Sprint | 기간 | 작업 |
|--------|------|------|
| S1 | 1~2일 | P0 도구 (tool_layer_audit / inspect_service / device_command) + P0 스킬 3종 |
| S2 | 3~5일 | tool_results_archive 정상화 → tool_metrics → cost_inspect |
| S3 | 1주 | deploy_safe / db_safe_write / runner_guard + dedup 가드 |
| S4 | 1~2주 | 스킬 시스템(`skill_run`) + complete_task + prompt_layer_audit |
| S5 | 2~4주 | notify_channel(다채널) + 외부 통신 표준화 + 비용 대시보드 |

---

## 8. 즉시 후속 조치(이번 스프린트 P0)

1. `AADS-TOOL-001` — `_dispatch` 별칭 오매핑 전수 점검 + 수정 (XS, ~30분)
2. `AADS-TOOL-002` — `inspect_service` 분리/매핑 정정 (XS)
3. `AADS-TOOL-003` — `tool_results_archive` 저장 누락 원인 조사 + 복구 (S)
4. `AADS-TOOL-004` — `tool_layer_audit` 도구 신규 추가 (S)
5. `AADS-DEV-001` — Android `device_command` 단일 도구 + permission_status 통합 (S)

각 항목은 별도 작업지시서로 분할 권장. 검수 후 Pipeline Runner 또는 직접 수정으로 진행.

---

## 9. 검증 기준 (이 보고서 자체)

- 3계층 도구 수 [정적 분석, 2026-05-06 09:0x KST]
- tool_results_archive 0건 [PostgreSQL 직접 조회]
- 도구 명시 오류율 [시스템 프롬프트 인용, 측정값 아님 — “체감/문서” 기준]
- 디바이스/외부통신/비용/스킬 결손 [코드/도구 정의 부재로 확인]
- runner 통계 [러너 상태 로그 기반, 비율은 추정]

이 보고서는 분석 보고이며, 코드/DB/프롬프트 변경은 수반하지 않았다.
