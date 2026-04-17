# Codex MCP tool_use/tool_result 이벤트 파싱 정밀 패치

- **커밋**: `28b8436` (원격 반영 완료, `bb393f3..28b8436  main -> main`)
- **배포 시각**: 2026-04-17 09:18:20 KST (Relay 재기동) → 09:35 KST 문서 기록
- **작성자**: AADS PM/CTO AI (Claude Opus 4.6)
- **CEO 승인**: 정밀 패치 지시(04/17 09:15 KST) + 커밋/푸시/문서기록/무중단 배포 승인

## 1. 문제 요약
- **증상**: TEST-001(`833a7bb4`) 창에서 GPT-5.4(Codex CLI) 모델 응답이 UI에 출력되지 않음. DB에는 assistant 메시지 2,115자 정상 저장되는데 프론트 렌더링 빈 채로 끝남.
- **2차 증상**: Relay 로그에 `Codex: ... tools=0` 기록 지속 → MCP 도구 호출 이벤트가 전혀 파싱되지 않음.
- **근본 원인 (실증)**: Codex CLI가 내보내는 실제 NDJSON 이벤트 스키마가 `function_call`이 아니라 `mcp_tool_call` / `agent_message` / `item.streaming` 인데, Relay 파서는 구 스키마(`function_call`)만 인식해 MCP 호출과 최종 텍스트 델타를 모두 drop.

## 2. 조치 내역 (`scripts/claude_relay_server.py`, +106 / −30)
| # | 함수/섹션 | 변경 |
|---|-----------|------|
| 1 | `_parse_codex_tool_event` | `mcp_tool_call` 스키마 처리 추가. `server.tool_name` / `arguments` / `output` 필드 매핑 |
| 2 | `handle_codex_stream` | `agent_message`에서 최종 텍스트만 추출하도록 보강. 기존 raw reasoning 토큰 혼입 제거 |
| 3 | 동일 | `item.streaming` delta 실시간 파싱 → 토큰 단위 스트림 복구 |
| 4 | 로깅 | Codex stderr 레벨 `DEBUG → INFO` 로 상향(디버깅 효율) |
| 5 | `_build_codex_home()` | `AADS_SESSION_ID` 빈 값 가드 → `"default"` 기본값 주입 (MCP Bridge 측 KeyError 방지) |

## 3. 실측 검증 (패치 반영 PID 19928 Relay 기준)
- **Relay 로그 (09:27:48)**: `Codex: model=gpt-5.4 prompt_len=105 tools=1` — 이전 `tools=0` → `tools=1` 상승 확인
- **POST 응답 크기**: 305B → **1,135B** (4배 상승)
- **세션 `patch-verify-20260417` NDJSON 전문**:
  1. `assistant/text chunk` — "AADS 원격에서 `date`를 실행해…"
  2. `tool_use` — `name=run_remote_command, input={project:AADS, command:date}`
  3. `tool_result` — `is_err=False, content="[AADS 명령 실행 — exit=0]\n$ date\nFri Apr 17 09:27:56 KST 2026"`
  4. `assistant/text chunk` — 최종 응답 텍스트 정상 전달

## 4. 무중단 배포 절차 (실행 완료)
| 단계 | 시각 (KST) | 결과 |
|------|-----------|------|
| 구문 검증 | 09:17 | `ast.parse OK` |
| Relay 프로세스 재기동 | 09:18:20 | 구 PID 종료 → 신규 PID 19928 |
| 실호출 검증 | 09:27:48 | `tools=1`, 1,135B 응답 |
| 커밋 | 09:34 | `28b8436` (`ALLOW_AUTH_COMMIT=1`) |
| 원격 푸시 | 09:35 | `bb393f3..28b8436 main -> main` |
| 문서 기록 | 09:35 | 본 파일 |

- Relay는 **파일 수정 → 프로세스 재기동(09:18:20)** 순으로 이미 패치본을 가동 중이므로, 커밋/푸시 단계에서 추가 재시작 없음(제로 다운타임).
- `aads-server` 컨테이너(FastAPI 본체)는 이번 패치 범위 밖이라 재빌드/재시작 불필요.

## 5. 남은 후속 작업
1. TEST-001(`833a7bb4`) 창에서 CEO가 실제 Codex MCP 도구 호출(`run_remote_command(AADS,"date")`) 1건 재시험 — UI 렌더링까지 최종 확인.
2. 기존에 생성된 구 세션(HOME/config.toml이 舊 스펙)들 auto-refresh 훅 추가 여부 결정.
3. `query_db` MCP 도구가 `SELECT 1`조차 거부하던 부작용(04/17 오전 관측)은 이번 패치 범위 밖 — 별도 티켓으로 추적 필요.

## 6. 관련 커밋 히스토리
- `28b8436` (본 건) — Codex MCP 이벤트 파싱 정밀 패치
- `bb393f3` — config.toml 공식 스펙 보강 (globals + startup/tool_timeout_sec)
- `d39cd9e` — auth.json 심볼릭 링크 (401 방지)
- `eec1384` — 세션별 HOME 분리 + tool_names 전달
- `1ea66d6` — MCP 도구 연결 + Kimi/MiniMax tools 전달 (A+C안)
