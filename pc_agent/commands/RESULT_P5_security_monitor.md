# RESULT: PC Agent P5 — 보안 잠금 + 프로세스 감시

## 구현 요약

### 기능 1: 보안 잠금 (security.py)
- **SecurityManager** 싱글톤: 명령 잠금 + JSONL 감사 로그
- 기본 잠금: `power_control`, `process_kill`
- 영구 저장: `~/.aads_security/locks.json`, `~/.aads_security/audit.jsonl`
- 민감 정보 필터링 (password/token/secret 제거, 200자 제한)
- 핸들러 4개: `security_lock`, `security_unlock`, `security_locked_list`, `security_audit`

### 기능 2: 프로세스 감시 (process_monitor.py)
- **ProcessMonitor** 싱글톤: 30초 간격 asyncio 체크 루프
- WatchConfig dataclass: process_name, action(alert/restart), restart_command
- psutil → tasklist(Windows) → pgrep(Linux) 3단계 폴백
- 영구 저장: `~/.aads_monitors/watches.json`
- 핸들러 3개: `monitor_add`, `monitor_remove`, `monitor_list`

### __init__.py 수정
- security, process_monitor 임포트 추가
- COMMAND_HANDLERS에 7개 항목 등록 (총 63개)
- __all__ 업데이트

---

## 검증 체크리스트

- [x] 구현 목표: 보안 잠금(명령 승인+감사 로그) + 프로세스 감시(자동 체크+재시작) 2개 기능 추가
- [x] 검증 방법: PC Agent WebSocket 명령 전송 (security_lock/unlock/locked_list/audit, monitor_add/remove/list)
- [x] 완료 기준: COMMAND_HANDLERS에 7개 핸들러 등록, AST 파싱 통과, 총 63개 핸들러
- [x] 실패 기준: AST 에러, 핸들러 누락, 임포트 실패
- [x] 서비스 재시작 확인: aads-server Up 26 minutes (healthy) — PC Agent는 클라이언트측이므로 서버 재시작 불필요
- [x] 에러 로그 0건: docker logs --since 60s aads-server | grep -i error → 0건

## 변경 파일
| 파일 | 상태 | 줄 수 |
|------|------|-------|
| pc_agent/commands/security.py | 기존 (171줄) | 변경 없음 |
| pc_agent/commands/process_monitor.py | 기존 (274줄) | 변경 없음 |
| pc_agent/commands/__init__.py | 수정 | 85→96줄 (+11) |

## 기술 규격 준수
- [x] `from __future__ import annotations` 필수
- [x] 한국어 docstring
- [x] async 함수 구현
- [x] 감사 로그 JSONL 영구 저장
- [x] psutil 미설치 시 tasklist/pgrep 폴백
- [x] asyncio 기반 (별도 스레드 불필요)
- [x] 서버측 변경 없음
