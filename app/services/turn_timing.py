"""채팅 한 턴의 구간별 소요 시간을 잰다.

## 왜 만드나

2026-09-14 대표님 질문: "채팅창이 너처럼 즉시 응답이 왜 안되지?"

답하려고 로그를 뒤졌는데 **계측 자체가 없었다.** 어디서 시간을 쓰는지
모르는 채로 추측만 할 수 있었다. 그래서 손으로 재 봤더니 컨텍스트 조립이
2.2초였고 그중 대부분이 질문 임베딩(CPU Ollama)이었다 — 하지만 릴레이
구간은 못 쟀다.

**못 재는 구간이 있으면 고칠 수도 없다.**

## 무엇을 재나

    ctx_ms          컨텍스트 조립 — Auto-RAG·임베딩·프롬프트 컴파일 포함
    rag_ms          그중 Auto-RAG 만
    relay_ms        요청을 보내고 첫 토큰이 올 때까지
    first_token_ms  **턴 시작부터 첫 토큰까지 — 사용자가 체감하는 값**
    total_ms        턴 전체

`first_token_ms` 가 핵심이다. 나머지는 그것이 왜 큰지 설명하는 값이다.

## 재는 것이 느려지면 안 된다

기록은 **끝난 뒤에 백그라운드로** 넣는다. 응답 경로에서 DB 를 기다리면
재려던 것을 재는 행위가 늘린다. 실패해도 조용히 버린다 — 계측이 채팅을
막으면 본말전도다.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextvars import ContextVar
from typing import Any, List, Optional

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("CHAT_TURN_TIMING", "true").lower() in ("1", "true", "yes")


# ─── 프롬프트 캐시 누적 ─────────────────────────────────────────────────────
#
# 캐시 적중은 `oauth_usage_log` 로는 판정할 수 없다. 그 표의 cli_relay 행은
# 30초 창 집계라 러너 트래픽이 같이 들어오고, 창당 cache_read 가 180만 토큰
# 규모다. 채팅 한 턴이 아끼는 1만 토큰은 0.6% 라 노이즈에 묻힌다
# (2026-09-17 실측: 수정 전후 2.36% → 2.83%, 분해 불가).
#
# 그래서 **턴 단위로** 센다. 모델 호출은 한 턴에 여러 번(도구 루프) 일어나므로
# 리스트를 하나 만들어 두고 제자리에서 더한다 — 컨텍스트가 복사돼도 같은
# 객체를 가리키므로 생성기 안쪽에서 더한 값이 바깥의 타이머에 그대로 보인다.
_cache_acc: ContextVar[Optional[List[int]]] = ContextVar("turn_cache_tokens", default=None)


def begin_cache_accounting() -> None:
    """턴 시작 시 누산기를 새로 건다."""
    _cache_acc.set([0, 0])


def add_cache_tokens(read: int, create: int) -> None:
    """모델 호출 한 번의 캐시 읽기/쓰기를 더한다. 누산기가 없으면 조용히 버린다."""
    acc = _cache_acc.get()
    if acc is None:
        return
    try:
        acc[0] += int(read or 0)
        acc[1] += int(create or 0)
    except (TypeError, ValueError):
        pass


def take_cache_tokens() -> tuple[int, int]:
    acc = _cache_acc.get() or [0, 0]
    return acc[0], acc[1]


class TurnTimer:
    """한 턴의 구간을 잰다. 쓰는 쪽이 `mark()` 만 부르면 된다."""

    __slots__ = ("t0", "marks", "session_id", "execution_id", "model", "intent",
                 "prompt_chars", "history_count", "tool_calls", "rag_ms", "_saved")

    def __init__(self, session_id: str) -> None:
        self.t0 = time.perf_counter()
        begin_cache_accounting()
        self.marks: dict[str, float] = {}
        self.session_id = session_id
        self.execution_id: Optional[str] = None
        self.model = ""
        self.intent = ""
        self.prompt_chars = 0
        self.history_count = 0
        self.tool_calls = 0
        # Auto-RAG 는 컨텍스트 빌더 **안쪽**에서 끝난다. 바깥에서 mark 로는
        # 잡을 수 없어 `rag_done` 이 한 번도 찍히지 않았고, 7일 375턴이 전부
        # NULL 이었다(2026-09-16 실측). 빌더가 직접 잰 값을 받아 적는다.
        self.rag_ms: Optional[int] = None
        self._saved = False

    def mark(self, name: str) -> None:
        """턴 시작부터 지금까지의 밀리초를 기록한다. 같은 이름은 첫 번째만."""
        if name not in self.marks:
            self.marks[name] = (time.perf_counter() - self.t0) * 1000.0

    def _ms(self, name: str) -> Optional[int]:
        v = self.marks.get(name)
        return int(v) if v is not None else None

    def _span(self, a: str, b: str) -> Optional[int]:
        va, vb = self.marks.get(a), self.marks.get(b)
        if va is None or vb is None:
            return None
        return max(0, int(vb - va))

    def save(self) -> None:
        """끝난 뒤 백그라운드로 넣는다. 응답 경로에서 기다리지 않는다."""
        if not _ENABLED or self._saved:
            return
        self._saved = True
        self.mark("done")
        try:
            asyncio.create_task(self._write())
        except RuntimeError:
            pass

    async def _write(self) -> None:
        try:
            from app.core.db_pool import get_pool

            _cache_read, _cache_create = take_cache_tokens()
            await get_pool().execute(
                """
                INSERT INTO chat_turn_timing
                    (session_id, execution_id, model, intent,
                     ctx_ms, rag_ms, relay_ms, first_token_ms, total_ms,
                     prompt_chars, history_count, tool_calls,
                     cache_read_tokens, cache_creation_tokens)
                VALUES ($1::uuid, NULLIF($2,'')::uuid, NULLIF($3,''), NULLIF($4,''),
                        $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                self.session_id, self.execution_id or "", self.model, self.intent,
                self._ms("ctx_done"),
                self.rag_ms if self.rag_ms is not None
                else (self._span("ctx_start", "rag_done") or self._ms("rag_done")),
                self._span("request_sent", "first_token"),
                self._ms("first_token"),
                self._ms("done"),
                self.prompt_chars, self.history_count, self.tool_calls,
                _cache_read, _cache_create,
            )
        except Exception as exc:
            # 계측 실패가 채팅을 막으면 본말전도다.
            logger.debug("turn_timing_write_failed", error=str(exc)[:160])


async def recent_summary(hours: int = 24, session_id: str = "") -> dict[str, Any]:
    """구간별 중앙값과 90분위. 평균은 긴 꼬리에 끌려가 실제 체감과 멀어진다."""
    from app.core.db_pool import get_pool

    row = await get_pool().fetchrow(
        """
        SELECT count(*) AS turns,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY first_token_ms) AS p50_first,
               percentile_disc(0.9) WITHIN GROUP (ORDER BY first_token_ms) AS p90_first,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY ctx_ms)   AS p50_ctx,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY rag_ms)   AS p50_rag,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY relay_ms) AS p50_relay,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY total_ms) AS p50_total,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY prompt_chars) AS p50_prompt
        FROM chat_turn_timing
        WHERE created_at > NOW() - ($1 || ' hours')::interval
          AND ($2::text = '' OR session_id = $2::uuid)
        """,
        str(hours), session_id,
    )
    return dict(row) if row else {}
