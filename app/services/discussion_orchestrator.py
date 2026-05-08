from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any

try:
    from app.core.anthropic_client import call_llm_with_fallback
except Exception:  # pragma: no cover - import safety for isolated unit tests
    async def call_llm_with_fallback(*args: Any, **kwargs: Any) -> str | None:
        return None


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
    """멀티-LLM 토론 상태를 관리하는 최소 오케스트레이터."""

    def __init__(self) -> None:
        self._active_sessions: dict[str, DiscussionState] = {}

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

    @staticmethod
    def _normalize_command(command: str | None) -> str:
        return " ".join((command or "").strip().lower().split())

    def _is_stop_command(self, command: str | None) -> bool:
        normalized = self._normalize_command(command)
        stop_commands = {
            "그만",
            "그만해",
            "중지",
            "종료",
            "멈춰",
            "스톱",
            "stop",
            "halt",
            "cancel",
        }
        return normalized in stop_commands

    def _is_proceed_command(self, command: str | None) -> bool:
        normalized = self._normalize_command(command)
        proceed_commands = {
            "다음",
            "계속",
            "진행",
            "ㄱㄱ",
            "go",
            "next",
            "continue",
        }
        return normalized in proceed_commands

    def _sse(self, event: str, payload: dict[str, Any]) -> str:
        body = json.dumps({"event": event, **payload}, ensure_ascii=False)
        return f"data: {body}\n\n"

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


__all__ = [
    "DiscussionMode",
    "DiscussionOrchestrator",
    "DiscussionState",
    "DiscussionStatus",
    "ParticipantConfig",
    "RoundEntry",
    "call_llm_with_fallback",
]
