# `ask_session` — PRD·설계

- 작성 2026-09-14 KST · 배경은 [기획서](../plans/20260914_ASK_SESSION_기획서.md)
- 범위: 도구 1개 + 회신 경로 + 루프 차단. 화면 변경 없음.

## 도구 규격

    ask_session(
      target      : str    담당 이름 또는 세션 id
      question    : str    무엇을 묻는가
      context     : str    공유할 내용 (선택, 4,000자 상한)
    ) -> {"sent": true, "target_session": "...", "hop": 1}

`target` 은 세션 id(UUID) 또는 **역할 키**를 받는다. 주도가
`ask_session(target="WaveEngineOwner", ...)` 라고 부르는 쪽이 자연스럽다 —
세션 id 를 외우게 하면 안 쓴다.

## 받는 쪽에 들어가는 내용

    [파동엔진담당에게 — 전략카드담당(5090a247)의 질문]

    진입 시그널을 15초 당기면 승률이 어떻게 되나?

    ── 공유된 내용 ──
    (context)

    ── 답할 때 ──
    이 답은 물어본 대화로 자동 전달됩니다. 결론을 먼저 쓰세요.

`intent_override="system_trigger"` 로 넣는다. 이미 96% 응답률이 확인된 경로다.

## 회신

받는 쪽의 응답이 끝나면 그 본문을 **물어본 세션에** 넣는다.

    [파동엔진담당의 답 — 물어본 질문: "진입 시그널을 15초…"]
    (답 본문)

회신도 `system_trigger` 로 넣되 **답을 요구하지 않는다.** 회신에 또
답하면 그게 무한 루프의 시작이다.

## 루프 차단 — 세 겹

| 겹 | 규칙 | 위반 시 |
|---|---|---|
| 1 | 한 줄기 왕복 **3회**(`hop <= 3`) | 도구가 거절, 사유 반환 |
| 2 | 같은 (A,B) 쌍이 이미 진행 중이면 새 요청 불가 | 도구가 거절 |
| 3 | 자기 자신에게는 못 건다 | 도구가 거절 |

`hop` 은 `chat_messages.metadata` 가 아니라 **전용 테이블**에 둔다.
메시지 metadata 에 두면 회신 경로에서 잃는다.

    session_relay
      id, origin_session_id, target_session_id, hop,
      question, status(pending|answered|failed|blocked),
      request_message_id, answer_message_id,
      created_at, answered_at

## 실패 처리

| 상황 | 처리 |
|---|---|
| 대상 세션 없음 | 즉시 거절, 사유 반환 |
| 대상이 이미 응답 생성 중 | 큐에 넣지 않고 거절 — 끼어들면 그 작업이 깨진다 |
| 대상이 끝내 답 안 함 | `status=failed`, 물어본 쪽에 실패 통지 |
| 회신 주입 실패 | 로그 + `failed`. 원 답변은 대상 세션에 그대로 남아 있다 |

## 백그라운드 실행

도구는 요청만 걸고 즉시 반환한다. 실제 대화는 백그라운드 태스크가 돌린다.

**태스크 참조를 반드시 붙든다.** `asyncio.create_task` 결과를 버리면
가비지 컬렉터가 중간에 거둬 간다 — 2026-09-14 에 `chat_messages.embedding`
이 그 방식으로 소리 없이 실패하고 있었다(assistant 메시지 9.6%만 임베딩).

## 승인 게이트와의 관계

`ask_session` 으로 다른 세션을 시켜 실매매를 바꾸는 우회는 **막힌다.**
승인 게이트는 `ToolExecutor.execute()` 에 있어 어느 세션이 부르든 같은
문을 지난다. 세션을 바꿔도 도구는 같은 검사를 받는다.

## 수용 기준

1. 주도 → 담당 질문이 담당 세션에 들어가고 답이 주도 세션으로 돌아온다.
2. 홉 4회째 요청이 거절된다.
3. 자기 자신 / 없는 세션 / 응답 중인 세션에 못 건다.
4. 실매매 변경은 `ask_session` 을 거쳐도 여전히 막힌다.
5. 백그라운드 태스크가 중간에 사라지지 않는다.
