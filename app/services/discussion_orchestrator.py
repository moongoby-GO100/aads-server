from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any

import structlog

try:
    from app.core.anthropic_client import call_llm_with_fallback
except Exception:  # pragma: no cover - import safety for isolated unit tests
    async def call_llm_with_fallback(*args: Any, **kwargs: Any) -> str | None:
        return None

try:
    from app.services.discussion_presets import (
        get_preset,
        estimate_round_cost,
        MODEL_ROUND_COST_USD,
    )
except Exception:  # pragma: no cover
    def get_preset(name: str | None) -> dict[str, Any]:
        return {"participants": [], "synthesizer_model": "claude-opus-4-6"}
    def estimate_round_cost(p: Any) -> float:
        return 0.0
    MODEL_ROUND_COST_USD: dict[str, float] = {}

logger = structlog.get_logger(__name__)


class DiscussionMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class DiscussionStatus(str, Enum):
    ACTIVE = "active"
    WAIT_CEO = "wait_ceo"
    SYNTHESIZING = "synthesizing"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class ParticipantConfig:
    name: str
    role: str
    model: str
    system_prompt: str = ""
    color: str = ""
    avatar: str = ""


@dataclass
class RoundEntry:
    round_number: int
    participant_name: str
    content: str
    model: str
    ceo_directive: str | None = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DiscussionState:
    discussion_id: str
    session_id: str
    topic: str
    participants: list[ParticipantConfig] = field(default_factory=list)
    mode: DiscussionMode = DiscussionMode.MANUAL
    status: DiscussionStatus = DiscussionStatus.ACTIVE
    current_round: int = 0
    rounds: list[RoundEntry] = field(default_factory=list)
    total_cost_usd: float = 0.0
    budget_usd: float = 10.0
    ceo_directive: str | None = None
    synthesizer_model: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


class DiscussionOrchestrator:
    """멀티-LLM 토론 오케스트레이터 — SSE 스트리밍, 멀티라운드, 자동모드, CEO 개입, DB 저장."""

    def __init__(self) -> None:
        self._active_sessions: dict[str, DiscussionState] = {}

    # ── state management ──

    def register_state(self, state: DiscussionState) -> DiscussionState:
        self._active_sessions[state.discussion_id] = state
        return state

    def _resolve_state(
        self,
        discussion_id: str | None = None,
        state: DiscussionState | None = None,
    ) -> DiscussionState | None:
        if state is not None:
            return state
        if discussion_id:
            return self._active_sessions.get(discussion_id)
        if len(self._active_sessions) == 1:
            return next(iter(self._active_sessions.values()))
        return None

    def get_active_discussion(self, session_id: str) -> DiscussionState | None:
        for state in self._active_sessions.values():
            if state.session_id == session_id:
                return state
        return None

    # ── context building ──

    def _build_round_context(
        self,
        participant: ParticipantConfig,
        ceo_directive: str | None = None,
        discussion_id: str | None = None,
        state: DiscussionState | None = None,
    ) -> str:
        active_state = self._resolve_state(discussion_id=discussion_id, state=state)

        lines = [
            f"참가자: {participant.name}",
            f"역할: {participant.role}",
            f"모델: {participant.model}",
        ]

        if participant.system_prompt:
            lines.append(f"시스템 프롬프트: {participant.system_prompt}")

        if active_state is not None:
            round_number = active_state.current_round or 1
            lines.insert(0, f"현재 라운드: {round_number}")
            lines.insert(0, f"토론 주제: {active_state.topic}")

            previous_entries = [
                entry
                for entry in active_state.rounds
                if entry.participant_name != participant.name
            ]
            if previous_entries:
                lines.append("이전 발언:")
                for entry in previous_entries:
                    lines.append(
                        f"- {entry.participant_name} ({entry.model}): {entry.content}"
                    )

        directive = (ceo_directive or "").strip()
        if directive:
            lines.append(f"CEO 지시: {directive}")

        return "\n".join(lines)

    # ── command detection ──

    @staticmethod
    def _normalize_command(command: str | None) -> str:
        return " ".join((command or "").strip().lower().split())

    def _is_stop_command(self, command: str | None) -> bool:
        normalized = self._normalize_command(command)
        stop_commands = {
            "그만", "그만해", "중지", "종료", "멈춰", "스톱",
            "stop", "halt", "cancel",
        }
        return normalized in stop_commands

    def _is_proceed_command(self, command: str | None) -> bool:
        normalized = self._normalize_command(command)
        proceed_commands = {
            "다음", "계속", "진행", "ㄱㄱ",
            "go", "next", "continue",
        }
        return normalized in proceed_commands

    # ── SSE helper ──

    def _sse(self, event: str, payload: dict[str, Any]) -> str:
        body = json.dumps({"event": event, **payload}, ensure_ascii=False)
        return f"data: {body}\n\n"

    # ── LLM call ──

    async def generate_participant_reply(
        self,
        participant: ParticipantConfig,
        context: str,
    ) -> str | None:
        return await call_llm_with_fallback(
            prompt=context,
            model=participant.model,
            max_tokens=1024,
            system=participant.system_prompt or None,
        )

    # ── core: start discussion (SSE generator) ──

    async def start_discussion(
        self,
        session_id: str,
        topic: str,
        *,
        mode: DiscussionMode = DiscussionMode.MANUAL,
        preset: str | None = "standard",
        custom_participants: list[dict[str, Any]] | None = None,
        budget_usd: float = 10.0,
        synthesizer_model: str | None = None,
    ) -> AsyncGenerator[str, None]:
        discussion_id = f"disc-{uuid.uuid4().hex[:8]}"
        start_ts = time.time()

        preset_config = get_preset(preset)
        if custom_participants:
            participants = [ParticipantConfig(**p) for p in custom_participants]
        else:
            participants = [ParticipantConfig(**p) for p in preset_config["participants"]]

        synth_model = synthesizer_model or preset_config.get("synthesizer_model", "claude-opus-4-6")

        state = DiscussionState(
            discussion_id=discussion_id,
            session_id=session_id,
            topic=topic,
            participants=participants,
            mode=mode,
            status=DiscussionStatus.ACTIVE,
            budget_usd=budget_usd,
            synthesizer_model=synth_model,
        )
        self.register_state(state)

        yield self._sse("discussion_start", {
            "discussion_id": discussion_id,
            "topic": topic,
            "mode": mode.value,
            "participants": [asdict(p) for p in participants],
            "budget_usd": budget_usd,
        })

        if mode == DiscussionMode.AUTO:
            async for chunk in self._auto_loop(state):
                yield chunk
        else:
            async for chunk in self._run_round(state):
                yield chunk
            state.status = DiscussionStatus.WAIT_CEO
            state.touch()
            yield self._sse("wait_ceo", {
                "discussion_id": discussion_id,
                "round": state.current_round,
                "message": "라운드 완료. CEO 지시를 기다립니다. (다음/계속/그만)",
            })

        if state.status == DiscussionStatus.DONE:
            duration_ms = int((time.time() - start_ts) * 1000)
            await self._save_to_db(state, duration_ms)

    # ── core: continue discussion (CEO intervention) ──

    async def continue_discussion(
        self,
        session_id: str,
        message: str,
    ) -> AsyncGenerator[str, None]:
        state = self.get_active_discussion(session_id)
        if state is None:
            yield self._sse("error", {"message": "활성 토론이 없습니다."})
            return

        start_ts = time.time()

        if self._is_stop_command(message):
            yield self._sse("ceo_stop", {"message": "CEO가 토론 종료를 요청했습니다."})
            state.status = DiscussionStatus.SYNTHESIZING
            state.touch()
            async for chunk in self._synthesize(state):
                yield chunk
            state.status = DiscussionStatus.DONE
            state.touch()
            duration_ms = int((time.time() - start_ts) * 1000)
            await self._save_to_db(state, duration_ms)
            self._active_sessions.pop(state.discussion_id, None)
            return

        if not self._is_proceed_command(message):
            state.ceo_directive = message
            yield self._sse("ceo_directive", {
                "directive": message,
                "message": f"CEO 지시 반영: {message}",
            })

        if state.mode == DiscussionMode.AUTO:
            async for chunk in self._auto_loop(state):
                yield chunk
        else:
            async for chunk in self._run_round(state):
                yield chunk
            state.status = DiscussionStatus.WAIT_CEO
            state.touch()
            yield self._sse("wait_ceo", {
                "discussion_id": state.discussion_id,
                "round": state.current_round,
                "message": "라운드 완료. CEO 지시를 기다립니다.",
            })

    # ── round execution (parallel participant calls) ──

    async def _run_round(self, state: DiscussionState) -> AsyncGenerator[str, None]:
        state.current_round += 1
        state.status = DiscussionStatus.ACTIVE
        state.touch()

        yield self._sse("round_start", {
            "discussion_id": state.discussion_id,
            "round": state.current_round,
            "participants": [p.name for p in state.participants],
        })

        async def _call_participant(p: ParticipantConfig) -> RoundEntry:
            context = self._build_round_context(
                participant=p,
                ceo_directive=state.ceo_directive,
                state=state,
            )
            reply = await self.generate_participant_reply(p, context)
            content = reply or "(응답 없음)"
            cost = MODEL_ROUND_COST_USD.get(p.model, 0.5)
            return RoundEntry(
                round_number=state.current_round,
                participant_name=p.name,
                content=content,
                model=p.model,
                ceo_directive=state.ceo_directive,
                cost_usd=cost,
            )

        tasks = [_call_participant(p) for p in state.participants]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error("participant_call_failed", error=str(result))
                entry = RoundEntry(
                    round_number=state.current_round,
                    participant_name="error",
                    content=f"오류: {result}",
                    model="error",
                )
            else:
                entry = result

            state.rounds.append(entry)
            state.total_cost_usd += entry.cost_usd
            yield self._sse("participant_reply", {
                "discussion_id": state.discussion_id,
                "round": state.current_round,
                "participant": entry.participant_name,
                "model": entry.model,
                "content": entry.content,
                "cost_usd": entry.cost_usd,
            })

        state.ceo_directive = None
        state.touch()

        yield self._sse("round_complete", {
            "discussion_id": state.discussion_id,
            "round": state.current_round,
            "total_cost_usd": round(state.total_cost_usd, 4),
            "budget_remaining": round(state.budget_usd - state.total_cost_usd, 4),
        })

    # ── auto loop (continuous rounds until stop/budget) ──

    async def _auto_loop(self, state: DiscussionState) -> AsyncGenerator[str, None]:
        max_rounds = 20

        while state.current_round < max_rounds:
            if state.total_cost_usd >= state.budget_usd:
                yield self._sse("budget_exceeded", {
                    "total_cost_usd": round(state.total_cost_usd, 4),
                    "budget_usd": state.budget_usd,
                    "message": "예산 초과로 자동 종료합니다.",
                })
                break

            if state.status == DiscussionStatus.CANCELLED:
                yield self._sse("cancelled", {"message": "토론이 취소되었습니다."})
                return

            async for chunk in self._run_round(state):
                yield chunk

            await asyncio.sleep(0.5)

        state.status = DiscussionStatus.SYNTHESIZING
        state.touch()
        async for chunk in self._synthesize(state):
            yield chunk

        state.status = DiscussionStatus.DONE
        state.touch()

    # ── synthesis ──

    async def _synthesize(self, state: DiscussionState) -> AsyncGenerator[str, None]:
        yield self._sse("synthesis_start", {
            "discussion_id": state.discussion_id,
            "model": state.synthesizer_model or "claude-opus-4-6",
        })

        round_summaries = []
        for entry in state.rounds:
            round_summaries.append(
                f"[라운드 {entry.round_number}] {entry.participant_name} ({entry.model}): {entry.content}"
            )

        synthesis_prompt = (
            f"다음은 '{state.topic}' 주제에 대한 멀티 LLM 토론 기록입니다.\n\n"
            + "\n".join(round_summaries)
            + "\n\n위 토론을 종합하여 핵심 합의점, 미해결 쟁점, 최종 권고안을 "
            "구조화된 기획서 형태로 정리하세요. 마크다운 형식으로 작성하세요."
        )

        synthesis = await call_llm_with_fallback(
            prompt=synthesis_prompt,
            model=state.synthesizer_model or "claude-opus-4-6",
            max_tokens=4096,
        )

        synthesis_text = synthesis or "(종합 실패)"

        yield self._sse("synthesis_complete", {
            "discussion_id": state.discussion_id,
            "synthesis": synthesis_text,
            "total_rounds": state.current_round,
            "total_cost_usd": round(state.total_cost_usd, 4),
        })

    # ── DB persistence ──

    async def _save_to_db(self, state: DiscussionState, duration_ms: int) -> None:
        try:
            from app.core.db_pool import get_pool
            pool = await get_pool()

            synthesis_text = ""
            for entry in state.rounds:
                synthesis_text += f"[R{entry.round_number}] {entry.participant_name}: {entry.content}\n"

            participants_json = json.dumps(
                [asdict(p) for p in state.participants], ensure_ascii=False
            )
            rounds_json = json.dumps(
                [
                    {
                        "round_number": r.round_number,
                        "participant_name": r.participant_name,
                        "content": r.content,
                        "model": r.model,
                        "ceo_directive": r.ceo_directive,
                        "cost_usd": r.cost_usd,
                    }
                    for r in state.rounds
                ],
                ensure_ascii=False,
            )

            now = datetime.now(timezone.utc).isoformat()
            await pool.execute(
                """INSERT INTO discussion_sessions
                   (id, session_id, topic, status, mode, participants,
                    current_round, rounds, synthesizer_model, synthesis,
                    budget_usd, total_cost, duration_ms, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb,
                           $7, $8::jsonb, $9, $10,
                           $11, $12, $13, $14::timestamptz, $15::timestamptz)
                """,
                state.discussion_id,
                state.session_id,
                state.topic,
                state.status.value,
                state.mode.value,
                participants_json,
                state.current_round,
                rounds_json,
                state.synthesizer_model,
                synthesis_text,
                state.budget_usd,
                state.total_cost_usd,
                duration_ms,
                now,
                now,
            )
            logger.info("discussion_saved", discussion_id=state.discussion_id)
        except Exception as exc:
            logger.error("discussion_save_failed", error=str(exc))

    # ── CEO directive injection ──

    def inject_ceo_directive(self, discussion_id: str, directive: str) -> bool:
        state = self._active_sessions.get(discussion_id)
        if state is None:
            return False
        state.ceo_directive = directive
        state.touch()
        return True

    def cancel_discussion(self, discussion_id: str) -> bool:
        state = self._active_sessions.get(discussion_id)
        if state is None:
            return False
        state.status = DiscussionStatus.CANCELLED
        state.touch()
        self._active_sessions.pop(discussion_id, None)
        return True

    def get_discussion_status(self, discussion_id: str) -> dict[str, Any] | None:
        state = self._active_sessions.get(discussion_id)
        if state is None:
            return None

        payload = asdict(state)
        payload["mode"] = state.mode.value
        payload["status"] = state.status.value
        return payload


orchestrator = DiscussionOrchestrator()

__all__ = [
    "DiscussionMode",
    "DiscussionOrchestrator",
    "DiscussionState",
    "DiscussionStatus",
    "ParticipantConfig",
    "RoundEntry",
    "call_llm_with_fallback",
    "orchestrator",
]
