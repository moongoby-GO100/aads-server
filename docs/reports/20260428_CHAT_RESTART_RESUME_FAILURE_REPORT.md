# AADS 채팅 서버 재시작 후 이어쓰기 실패 원인 보고서

- 작성 시각: 2026-04-28 12:28~12:32 KST
- 대상 세션: `19808b87-82bc-48df-9a02-e994cfed023c` (`AADS-002[기능개선]`)
- 직접 관련 실행: `394c20ba-8486-4606-bd17-0a11e943ce33`
- 관련 사용자 요청: "채팅창 기능 전수 분석 기술문서를 작성 저장하고 현황 보고 및 사용자 입장에서 개선안 보고해"
- 저장된 기술문서: `docs/reports/20260428_CHAT_FEATURE_FULL_AUDIT.md` (26,746 bytes)

## 1. 결론

서버 재시작 후 대화가 "이어지는 것처럼" 복구되었지만, 실제로는 최종 보고까지 이어서 생성하지 못했다.

원인은 Redis Stream 완료 마커(`done=true`)가 실제 최종 응답 완료가 아니라 `aads-api` 종료 시점의 producer `finally`에서 찍힌 점이다. 이후 재시작 복구 로직은 Redis에 `done=true`가 있으므로 LLM 재호출을 생략하고, 571자짜리 중간 진행 보고를 `recovered_from_redis` 최종 assistant 메시지로 저장했다.

즉, 작업 파일은 저장됐지만 채팅창 최종 보고는 누락되었다.

## 2. 실측 타임라인

| 시각(KST) | 항목 | 실측 내용 |
|---|---|---|
| 12:14:27 | 사용자 요청 | 채팅창 기능 전수 분석 기술문서 작성 요청 |
| 12:14:29 | execution 시작 | `394c20ba-8486-4606-bd17-0a11e943ce33`, `status=completed`로 최종 저장됨 |
| 12:14:49~12:17:28 | Redis Stream delta | "분석 범위 확정", "DB 기준 36,674건..." 같은 중간 진행 문구가 토큰으로 저장됨 |
| 12:17:29 | 서버 종료 | `aads-api` SIGTERM 수신 |
| 12:17:30 | Redis done | `chat:stream:394c20ba-...` 마지막 엔트리 `done=true` |
| 12:17:55 | DB 저장 | assistant 메시지 `ed7d5e39...`, `model_used=recovered_from_redis`, 길이 571자 |
| 12:19 | 보고서 파일 | `docs/reports/20260428_CHAT_FEATURE_FULL_AUDIT.md`, 26,746 bytes 존재 |
| 12:20:04 | 서버 재시작 | `aads-api` 재차 SIGTERM 후 기동 |

## 3. DB/Redis 증거

### 3.1 DB 메시지

`chat_messages` 조회 결과, 해당 assistant 메시지는 최종 보고가 아니라 중간 진행 문구까지만 포함했다.

```text
id=ed7d5e39-d23f-47c5-b653-8ec9a338158e
model_used=recovered_from_redis
len=571
tail=... 이제 분석 결과를 `docs/reports`에 기술문서로 저장하겠습니다.
```

같은 시점의 보고서 파일은 26,746 bytes로 실제 저장되어 있었다. 따라서 "작업 산출물 저장"과 "채팅 최종 보고 전달" 사이에서 스트림이 끊겼다.

### 3.2 Redis Stream

Redis key `chat:stream:394c20ba-8486-4606-bd17-0a11e943ce33`:

```text
length=76
first-entry=1777346070372-0
last-entry=1777346250029-0
last-entry fields: done=true
```

마지막 delta는 다음 문장이다.

```text
... 이제 분석 결과를 `docs/reports`에 기술문서로 저장하겠습니다.
```

이후 최종 "작성 완료/저장 위치/개선안" 보고 delta는 없었다.

## 4. 코드상 원인

### 4.1 완료 마커가 너무 쉽게 찍힘

`app/services/chat_service.py`의 `with_background_completion()` producer는 기존 코드에서 `finally`에 들어오면 항상 다음을 수행했다.

1. Redis Stream `mark_stream_done()`
2. streaming placeholder 삭제
3. `_streaming_state.completed=True`

문제는 `finally`가 정상 완료뿐 아니라 `CancelledError`, `GeneratorExit`, 서버 SIGTERM 중에도 실행된다는 점이다. 따라서 서버 종료 중 실제 `done` SSE를 받지 못했는데도 Redis에 완료 마커가 찍혔다.

### 4.2 복구 로직이 Redis done을 과신

`_resume_single_stream()`은 재시작 후 다음 조건이면 LLM 이어쓰기를 생략했다.

```python
if redis_complete and redis_content and len(redis_content) > len(partial_content):
    full_response = redis_content
    _resume_model = "recovered_from_redis"
```

하지만 이번 케이스에서 `redis_complete=True`는 정상 완료가 아니라 종료 중 `finally`가 남긴 잘못된 완료 마커였다. DB execution이 실제 완료됐는지 교차 확인하지 않았기 때문에 중간 진행 문구가 최종 답변으로 승격됐다.

### 4.3 프론트 Patch C와는 별개 문제

Patch C는 `streaming-status.execution_id=null`일 때 `activeSession.current_execution_id`로 SSE attach를 시도하는 재진입 UI 패치다. 이번 장애는 attach 이전 단계인 백엔드 Redis 완료 판정 오류다.

## 5. 현황

| 항목 | 상태 | 근거 |
|---|---|---|
| 현재 세션 새 실행 | running | DB `chat_turn_executions`, `4f502965...` |
| 이전 기술문서 파일 | 저장됨 | `docs/reports/20260428_CHAT_FEATURE_FULL_AUDIT.md`, 26,746 bytes |
| 이전 채팅 최종 보고 | 누락 | assistant 메시지 571자, `recovered_from_redis` |
| `recovered_from_redis` 짧은 메시지 | 3건 | DB 조회: 전체 85건 중 1,000자 미만 3건 |
| aads-server | healthy | Docker `aads-server Up ... healthy` |

## 6. 적용한 코드 조치

`app/services/chat_service.py`에 방어 패치를 적용했다.

### 6.1 producer 완료 판정 강화

- `state["saw_done_event"]` 추가
- 실제 SSE `type="done"`을 파싱한 경우에만 Redis `done=true`를 기록
- `CancelledError`, `GeneratorExit`, SIGTERM, auto-cancel 등 미완료 종료에서는 placeholder를 삭제하지 않음
- 미완료 종료는 `bg_producer_incomplete_exit` 경고 로그로 남김

### 6.2 재시작 복구 Redis 과신 방지

- Redis Stream에 `done=true`가 있어도 DB `chat_turn_executions.status='completed'`가 아니면 완료 응답으로 신뢰하지 않음
- 이 경우 `resume_redis_done_ignored` 로그를 남기고 기존 LLM 이어쓰기 경로로 진행

## 7. 검증

| 검증 | 결과 |
|---|---|
| 컨테이너 Python 문법 검증 | 통과: `docker exec aads-server python3 -m py_compile /app/app/services/chat_service.py` |
| DB 원인 확인 | 완료 |
| Redis Stream 원인 확인 | 완료 |
| 운영 메모리 반영 | 아직 미반영. 현재 응답 중 재시작하면 다시 끊길 수 있어 코드 저장/문법 검증까지만 수행 |

## 8. 사용자 입장 개선안

1. 채팅 UI에 "작업 산출물 저장됨 / 최종 보고 생성 중" 상태를 분리 표시해야 한다.
2. `recovered_from_redis` 메시지가 1,000자 미만이고 문장이 "저장하겠습니다/확인하겠습니다"로 끝나면 완료로 표시하지 말고 "복구 계속" 버튼을 노출해야 한다.
3. 서버 재시작 전 active execution이 있으면 Blue-Green drain 단계에서 Redis done 마커가 아니라 DB completed 상태를 기준으로 대기해야 한다.
4. 보고서 파일이 생성된 경우, 최종 보고가 끊겨도 다음 진입 시 "생성된 보고서" 링크를 자동 노출해야 한다.

## 9. 다음 조치

1. 현재 응답 완료 후 `aads-api`만 reload하여 패치를 메모리에 반영한다.
2. 재현 테스트: 긴 응답 중 `supervisorctl restart aads-api` 후 Redis done/DB completed 정합성 확인.
3. 짧은 `recovered_from_redis` 3건을 후처리 대상으로 검토한다.
