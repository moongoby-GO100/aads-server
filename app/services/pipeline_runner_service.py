"""
Pipeline Runner Orchestrator — 채팅 → Claude Code 자율 작업 → 검수 → 재지시 → 승인 → 배포

채팅방 연동 플로우 (v2):
  Phase 1: claude_code_work  — Claude Code CLI로 작업 수행 → 완료 보고를 채팅방에 삽입
  Phase 2: ai_review         — 채팅 AI가 결과 검수 → 검수 결과를 채팅방에 삽입
  Phase 3: revision (0~N)    — 검수 실패 시 재지시 루프 → 매 사이클 채팅방 기록
  Phase 4: awaiting_approval — CEO 승인 대기 → 채팅방에 승인 요청 메시지
  Phase 5: deploying         — 커밋/푸시/재시작 → 배포 결과 채팅방 기록
  Phase 6: verifying         — 최종 검증
  Phase 7: done              — 완료 → 채팅방에 최종 보고
"""
import asyncio
import base64
import json
import logging
import os
import shlex
import time
import uuid

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

import asyncpg

from app.core.project_config import PROJECT_MAP

logger = logging.getLogger(__name__)

# ─── 설정 ─────────────────────────────────────────────────────────────────────
_CLAUDE_TIMEOUT = 3600      # Claude Code 직접 대기 타임아웃 (60분, 폴링 모드 폴백용)
_CLAUDE_POLL_INTERVAL = 30  # 분리 실행 폴링 주기 (초)
_CLAUDE_MAX_WAIT = 7200     # 분리 실행 최대 대기 (2시간)

# P1-1: 작업 규모별 동적 타임아웃 (AADS-229)
_TIMEOUT_BY_SIZE = {
    "XS": 600,   # 10분
    "S": 1200,   # 20분
    "M": 3600,   # 60분
    "L": 5400,   # 90분
    "XL": 7200,  # 120분
}


def _get_timeout_for_job(job: "PipelineCJob") -> int:
    """작업 규모(size)에 따른 타임아웃(초) 반환."""
    size = getattr(job, "size", "M") or "M"
    return _TIMEOUT_BY_SIZE.get(size.upper(), _CLAUDE_MAX_WAIT)
_MAX_OUTPUT_CHARS = 6000    # 결과 최대 문자수
_MAX_DIFF_CHARS = 50000     # git diff 최대 문자수 (L3)
_REVIEW_MODEL = "claude-sonnet-4-6"

# AADS-234: LiteLLM Runner 폴백 모델 — size 기반, 무료 쿼터 우선
# CEO 지시: 크기별 Claude 모델 분기 — XL=Opus, L/M=Sonnet, S/XS=Haiku (2026-04-14)
_CLAUDE_MODEL_BY_SIZE = {
    "XS": "claude-haiku-4-5-20251001",
    "S":  "claude-haiku-4-5-20251001",
    "M":  "claude-sonnet-4-6",
    "L":  "claude-sonnet-4-6",
    "XL": "claude-opus-4-6",
}

_LITELLM_FALLBACK_MODELS = {
    "XS": "kimi-k2.5",
    "S":  "kimi-k2.5",
    "M":  "kimi-k2.5",
    "L":  "minimax-m2.7",
    "XL": "minimax-m2.7",
}


# AADS-290: 프로젝트별 litellm_runner.py 경로 매핑
_LITELLM_RUNNER_PATH: Dict[str, str] = {
    "AADS":  "/app/scripts/litellm_runner.py",            # 컨테이너 내부
    "GO100": "/root/kis-autotrade-v4/litellm_runner.py",  # 211 서버
    "KIS":   "/root/kis-autotrade-v4/litellm_runner.py",  # 211 서버
    "SF":    "/root/scripts/litellm_runner.py",           # 114 서버
    "NTV2":  "/root/scripts/litellm_runner.py",           # 114 서버
}

# M1: SSH/LLM 재시도 설정
_SSH_MAX_RETRIES = 3
_SSH_RETRY_BASE_DELAY = 2   # 초 (지수 백오프: 2, 4, 8)

# 프로젝트별 서비스 재시작 명령
_RESTART_CMD: Dict[str, str] = {
    "KIS":   "kill -HUP $(cat /run/gunicorn-kis-v41.pid)",       # gunicorn graceful reload (무중단)
    "GO100": "kill -HUP $(cat /run/gunicorn-go100.pid)",         # gunicorn graceful reload (무중단)
    "SF":    "cd /data/shortflow && docker compose restart worker",
    "NTV2":  "",  # PHP: 파일 수정 즉시 반영
    "AADS":  "bash /root/aads/aads-server/deploy.sh bluegreen",  # Blue-Green 무중단 배포
}

# 활성 작업 저장 (메모리)
_active_jobs: Dict[str, "PipelineCJob"] = {}

# 프로젝트별 동시 실행 방지 락
_project_locks: Dict[str, asyncio.Lock] = {}

# AADS 재시작 디바운스 — 연속 배포 시 마지막 1회만 재시작
_aads_restart_scheduled: float = 0.0  # 예약된 재시작 시각 (time.time)
_AADS_RESTART_DEBOUNCE = 30  # 30초 내 추가 배포 있으면 재시작 연기


async def _debounced_aads_restart(job: "PipelineCJob"):
    """AADS 재시작을 30초 디바운스하여 연속 배포 시 한 번만 재시작."""
    global _aads_restart_scheduled
    now = time.time()
    _aads_restart_scheduled = now + _AADS_RESTART_DEBOUNCE
    job._log("aads_restart_debounce", f"재시작 {_AADS_RESTART_DEBOUNCE}초 후 예약 (연속 배포 병합)")
    await asyncio.sleep(_AADS_RESTART_DEBOUNCE)
    # 디바운스: 대기 후에도 내가 마지막 예약자인지 확인
    if abs(_aads_restart_scheduled - (now + _AADS_RESTART_DEBOUNCE)) > 1:
        job._log("aads_restart_skipped", "후속 배포가 재시작을 인계받음 — 스킵")
        return False
    job._log("aads_restart_exec", "디바운스 완료 — 재시작 실행")
    await job._ssh_command("bash /root/aads/aads-server/deploy.sh bluegreen")
    return True

# H-11: job_id별 approve/reject 동시 호출 방지 락
_job_approve_locks: Dict[str, asyncio.Lock] = {}


def get_job(job_id: str) -> Optional["PipelineCJob"]:
    return _active_jobs.get(job_id)


def list_jobs(chat_session_id: str = None) -> list:
    jobs = list(_active_jobs.values())
    if chat_session_id:
        jobs = [j for j in jobs if j.chat_session_id == chat_session_id]
    return [j.to_dict() for j in jobs]


class PipelineCJob:
    """단일 Pipeline Runner 작업."""

    def __init__(self, project: str, instruction: str,
                 chat_session_id: str, max_cycles: int = 3,
                 dsn: str = "", model: str = "",
                 worker_model: str = "", parallel_group: str = "",
                 depends_on: str = "", size: str = "M"):
        self.job_id = f"runner-{uuid.uuid4().hex[:8]}"
        self.project = project.upper()
        self.instruction = instruction
        # UUID 형식 검증 — 유효하지 않으면 빈 문자열로 처리 (채팅 보고 비활성)
        try:
            if chat_session_id:
                uuid.UUID(chat_session_id)
            self.chat_session_id = chat_session_id
        except (ValueError, AttributeError):
            logger.warning(f"pipeline_c: 유효하지 않은 chat_session_id='{chat_session_id}' → 채팅 보고 비활성")
            self.chat_session_id = ""
        self.claude_session_id = str(uuid.uuid4())
        self.max_cycles = min(max_cycles, 5)
        self.dsn = dsn
        # AI가 선택한 모델 (sonnet/opus/haiku, 빈 문자열이면 기본 sonnet)
        self.model = model if model in ("sonnet", "opus", "haiku") else ""
        # AADS-211: 직접 모델 지정 (worker_model)
        self.worker_model = worker_model
        # AADS-211: 병렬 실행 그룹
        self.parallel_group = parallel_group
        # AADS-211: 의존 작업 job_id
        self.depends_on = depends_on
        # P1-1: 작업 규모 (동적 타임아웃용)
        self.size = (size or "M").upper()
        self.actual_model = ""  # 실제 실행된 모델명 (litellm:kimi-k2.5, claude:sonnet 등)
        self.phase = "queued"
        self.cycle = 0
        self.status = "running"  # running | awaiting_approval | done | error
        self.logs: list = []
        self.result_output = ""
        self.git_diff = ""
        self.review_feedback = ""
        self.created_at = datetime.now()
        self.error_msg = ""

        conf = PROJECT_MAP.get(self.project)
        if not conf:
            raise ValueError(f"Unknown project: {self.project}")
        self.server = conf["server"]
        self.workdir = conf["workdir"]
        self.ssh_port = conf.get("port", "22")

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "project": self.project,
            "instruction": self.instruction[:200],
            "chat_session_id": self.chat_session_id,
            "phase": self.phase,
            "cycle": self.cycle,
            "status": self.status,
            "logs": self.logs[-10:],  # 최근 10개
            "git_diff": self.git_diff[:2000] if self.git_diff else "",
            "review_feedback": self.review_feedback,
            "actual_model": self.actual_model or "",
            "created_at": self.created_at.isoformat(),
            "elapsed_sec": int((datetime.now() - self.created_at).total_seconds()),
        }

    def _log(self, phase: str, message: str):
        entry = {
            "phase": phase,
            "cycle": self.cycle,
            "message": message[:500],
            "timestamp": datetime.now().isoformat(),
        }
        self.logs.append(entry)
        self.phase = phase
        logger.info(f"pipeline_c | job={self.job_id} phase={phase} cycle={self.cycle} | {message[:200]}")
        # 실시간 로그 발행 (fire-and-forget)
        try:
            from app.services.task_logger import emit_task_log
            log_type = "phase_change" if phase in ("claude_code_work","ai_review","revision","awaiting_approval","deploying","verifying","done","error") else "info"
            asyncio.ensure_future(emit_task_log(self.job_id, log_type, message[:500], phase))
        except Exception:
            pass

    # ─── 채팅방 메시지 삽입 ──────────────────────────────────────────────────

    async def _post_to_chat(self, content: str, role: str = "assistant") -> None:
        """
        파이프라인 진행상황을 CEO 채팅방(chat_messages)에 직접 삽입.
        CEO가 채팅방에서 실시간으로 전 과정을 확인할 수 있도록 함.
        """
        if not self.chat_session_id:
            logger.warning(f"pipeline_c | job={self.job_id} | _post_to_chat 건너뜀: chat_session_id 없음 (content={content[:80]}...)")
            return
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO chat_messages
                            (session_id, role, content, model_used, intent, cost,
                             tokens_in, tokens_out, attachments, sources, tools_called)
                        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                        """,
                        self.chat_session_id,
                        role,
                        content,
                        _REVIEW_MODEL if role == "assistant" else None,
                        "pipeline_c",
                        Decimal("0"),
                        0, 0,
                    )
                    await conn.execute(
                        "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                        self.chat_session_id,
                    )
            logger.debug(f"pipeline_c_chat_posted job={self.job_id} role={role} len={len(content)}")
        except Exception as e:
            logger.warning(f"pipeline_c_chat_post_error job={self.job_id}: {e}")

    async def _trigger_ai_reaction(self, message: str) -> None:
        """채팅 AI가 결과를 확인하고 자동으로 반응하도록 트리거."""
        if not self.chat_session_id:
            # 폴백: 프로젝트 워크스페이스에서 최근 세션 조회
            try:
                self.chat_session_id = await _find_recent_session(self.project)
                if self.chat_session_id:
                    logger.info(f"pipeline_c_trigger_session_resolved: job={self.job_id} session={self.chat_session_id[:8]}...")
            except Exception as e:
                logger.warning(f"pipeline_c_trigger_session_fallback_error: {e}")
        if not self.chat_session_id:
            logger.warning(f"pipeline_c_trigger_skipped: job={self.job_id} no session_id")
            return
        try:
            from app.services.chat_service import trigger_ai_reaction
            await trigger_ai_reaction(self.chat_session_id, message)
            logger.info(f"pipeline_c_ai_trigger job={self.job_id} session={self.chat_session_id[:8]}...")
        except Exception as e:
            logger.warning(f"pipeline_c_ai_trigger_error job={self.job_id}: {e}")

    def _format_diff_summary(self, diff: str, max_lines: int = 30) -> str:
        """git diff를 보기 좋은 요약 형태로."""
        if not diff or not diff.strip():
            return "(변경사항 없음)"
        lines = diff.strip().split("\n")
        added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        files = [l.split(" b/")[-1] for l in lines if l.startswith("diff --git")]
        summary = f"변경 파일 {len(files)}개 | +{added} / -{removed} lines"
        if files:
            summary += "\n파일: " + ", ".join(files[:10])
            if len(files) > 10:
                summary += f" 외 {len(files) - 10}개"
        # diff 본문 (축약)
        if len(lines) > max_lines:
            return summary + f"\n```diff\n" + "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines}줄 생략)\n```"
        return summary + f"\n```diff\n{diff.strip()}\n```"

    # ─── 메인 실행 ──────────────────────────────────────────────────────────

    async def run(self):
        """Phase 1~3 자율 실행 → Phase 4 승인 대기에서 멈춤. 매 단계 채팅방 기록.
        AADS-211: parallel_group이 설정된 작업은 프로젝트 락 없이 동시 실행."""
        if self.parallel_group:
            # 병렬 그룹 작업 → 프로젝트 락 없이 바로 실행
            self._log("parallel_start", f"병렬 그룹 [{self.parallel_group}] — 동시 실행")
            await self._run_inner()
        else:
            lock = _project_locks.setdefault(self.project, asyncio.Lock())
            async with lock:
                await self._run_inner()

    async def _run_inner(self):
        """run()의 실제 본체 — 프로젝트 락 안에서 실행."""
        try:
            await self._save_to_db()
            # 작업 시작 이벤트
            try:
                from app.services.task_logger import emit_task_started
                await emit_task_started(self.job_id, self.project, self.instruction[:100], "pipeline_c", self.chat_session_id)
            except Exception:
                pass

            # Phase 1: Claude Code로 작업 수행
            self._log("claude_code_work", f"Claude Code에 작업 지시 중: {self.instruction[:100]}")
            await self._post_to_chat(
                f"🔧 **[Pipeline Runner 시작]** `{self.job_id}`\n"
                f"프로젝트: **{self.project}**\n"
                f"지시: {self.instruction[:300]}\n\n"
                f"Claude Code에 작업을 전달합니다. 완료까지 최대 {_CLAUDE_TIMEOUT // 60}분 소요됩니다."
            )

            # AADS-1864: 검증 체크리스트 자동 삽입
            enriched_instruction = _append_verification_checklist(self.instruction, self.project)

            # CEO 명시 지정: worker_model="litellm" 또는 "litellm:모델명" → LiteLLM Runner 직접 실행
            _wm = self.worker_model or ""
            if _wm == "litellm" or _wm.startswith("litellm:"):
                # CEO 명시 지정: 해당 모델 직행
                _direct_model = _wm.split(":", 1)[1] if ":" in _wm else _LITELLM_FALLBACK_MODELS.get(self.size, "kimi-k2.5")
                self._log("litellm_direct", f"LiteLLM Runner 직접 실행 (CEO 명시 지정, model={_direct_model})")
                await self._post_to_chat(
                    f"🤖 **[LiteLLM Runner 시작]** `{self.job_id}`\n"
                    f"프로젝트: **{self.project}** | 모델: **{_direct_model}**\n"
                    f"지시: {self.instruction[:300]}"
                )
                work_result = await self._run_litellm_fallback(enriched_instruction, override_model=_direct_model)
            elif _wm == "claude":
                # Claude 명시 지정: Claude 직행 (크기별 모델 분기)
                _claude_model = _CLAUDE_MODEL_BY_SIZE.get(self.size, "claude-sonnet-4-6")
                self.actual_model = f"claude:{_claude_model.split('-')[1] if '-' in _claude_model else 'sonnet'}"
                self.model = _claude_model.split('-')[1] if self.size in ("XL",) else ("haiku" if self.size in ("S", "XS") else "sonnet")
                work_result = await self._run_claude_code(enriched_instruction, continue_session=False)
            else:
                # 기본: LiteLLM 우선 실행 → 실패 시 Claude 폴백 (크기별 모델 분기)
                work_result = await self._run_litellm_fallback(enriched_instruction)
                if work_result.get("error"):
                    _claude_model = _CLAUDE_MODEL_BY_SIZE.get(self.size, "claude-sonnet-4-6")
                    self._log("claude_fallback_attempt", f"LiteLLM 실패 → Claude 폴백 (size={self.size}, model={_claude_model}): {work_result['error'][:100]}")
                    self.actual_model = f"claude:{_claude_model.split('-')[1] if '-' in _claude_model else 'sonnet'}"
                    self.model = _claude_model.split('-')[1] if self.size in ("XL",) else ("haiku" if self.size in ("S", "XS") else "sonnet")
                    work_result = await self._run_claude_code(enriched_instruction, continue_session=False)

            if work_result.get("error"):
                self._log("error", f"실행 오류: {work_result['error']}")
                self.status = "error"
                self.error_msg = work_result["error"]
                self.review_feedback = f"ERROR: Claude Code 실행 실패 — {work_result['error'][:300]}"
                await self._post_to_chat(
                    f"❌ **[Pipeline Runner 오류]** `{self.job_id}`\n"
                    f"Claude Code 실행 실패: {work_result['error'][:500]}"
                )
                await self._save_to_db()
                # 채팅 AI에게 에러 조치 트리거 — CEO가 채팅방에서 바로 확인+지시 가능
                await self._trigger_ai_reaction(
                    f"[시스템] Pipeline Runner 작업 `{self.job_id}` (프로젝트: {self.project})이 실패했습니다.\n"
                    f"오류: {work_result['error'][:300]}\n\n"
                    f"CEO에게 오류 원인과 해결 방안을 간단히 보고해주세요."
                )
                return

            self.result_output = work_result.get("output", "")
            self._log("claude_code_done", f"작업 완료. 출력 {len(self.result_output)}자")

            # 채팅방에 작업 완료 보고
            await self._post_to_chat(
                f"✅ **[Claude Code 작업 완료]** `{self.job_id}`\n\n"
                f"**출력 요약** (마지막 {min(len(self.result_output), 2000)}자):\n"
                f"```\n{self.result_output[-2000:]}\n```\n\n"
                f"AI 자동 검수를 시작합니다..."
            )

            # Phase 2~3: 검수 + 재지시 루프
            while self.cycle < self.max_cycles:
                self.cycle += 1

                # git diff 가져오기
                self.git_diff = (await self._ssh_command("git diff HEAD"))[:_MAX_DIFF_CHARS]

                # AI 검수
                self._log("ai_review", f"[{self.cycle}차] AI 검수 중...")
                review = await self._ai_review()

                if review["verdict"] == "DELEGATED":
                    # LLM 검수 실패 → 채팅 AI에게 직접 검수 위임
                    self._log("review_delegated", f"[{self.cycle}차] LLM 검수 실패, 채팅 AI에게 위임")
                    diff_summary = self._format_diff_summary(self.git_diff)
                    output_text = self.result_output[-2000:] if self.result_output else "(출력 없음)"
                    await self._post_to_chat(
                        f"⚠️ **[{self.cycle}차 검수 위임]** `{self.job_id}`\n"
                        f"LLM 검수 호출 실패로 채팅 AI에게 검수를 위임합니다."
                    )
                    self.status = "awaiting_approval"
                    self.review_feedback = "DELEGATED: 채팅 AI 검수 위임"
                    await self._save_to_db()
                    await self._trigger_ai_reaction(
                        f"[시스템] Pipeline Runner 작업 `{self.job_id}` (프로젝트: {self.project})의 "
                        f"AI 자동 검수가 실패하여 당신에게 검수를 위임합니다.\n\n"
                        f"## 원래 지시\n{self.instruction}\n\n"
                        f"## Claude Code 출력 (마지막 부분)\n{output_text}\n\n"
                        f"## 변경사항\n{diff_summary}\n\n"
                        f"## 검수 기준\n"
                        f"1. 원래 지시 사항이 정확히 반영됐는가?\n"
                        f"2. 명백한 버그가 새로 생기지 않았는가?\n"
                        f"3. 변경사항이 없으면 FAIL\n\n"
                        f"검수 후 판단:\n"
                        f"- PASS: pipeline_runner_approve(job_id='{self.job_id}', action='approve')\n"
                        f"- FAIL: pipeline_runner_approve(job_id='{self.job_id}', action='reject', feedback='구체적 사유')"
                    )
                    return

                if review["verdict"] == "PASS":
                    self._log("review_pass", f"[{self.cycle}차] 검수 통과: {review['summary']}")
                    self.review_feedback = f"PASS: {review['summary']}"

                    # 채팅방에 검수 통과 보고
                    diff_summary = self._format_diff_summary(self.git_diff)
                    await self._post_to_chat(
                        f"✅ **[{self.cycle}차 검수 통과]** `{self.job_id}`\n"
                        f"판정: **PASS**\n"
                        f"요약: {review['summary']}\n\n"
                        f"**변경사항:**\n{diff_summary}"
                    )
                    break

                # 검수 실패 → 채팅방에 기록 후 재지시
                self.review_feedback = f"FAIL: {review['feedback']}"
                self._log("revision", f"[{self.cycle}차] 재지시: {review['feedback'][:200]}")

                await self._post_to_chat(
                    f"🔄 **[{self.cycle}차 검수 실패 → 재지시]** `{self.job_id}`\n"
                    f"판정: **FAIL**\n"
                    f"피드백: {review['feedback'][:500]}\n\n"
                    f"Claude Code에 수정을 재지시합니다... ({self.cycle}/{self.max_cycles})"
                )

                # QA FAIL 후 재지시: 이전 프로세스 완전 종료 확인 + 추가 대기
                await asyncio.sleep(5)

                revision_instruction = f"이전 작업에 대한 검수 피드백입니다. 수정해주세요:\n{review['feedback']}"
                work_result = await self._run_claude_code(
                    revision_instruction,
                    continue_session=True,
                )
                # AADS-234: 재작업 실패 시 LiteLLM 폴백
                if work_result.get("error") and self.project == "AADS":
                    self._log("litellm_fallback_attempt", f"재작업 실패 → LiteLLM 폴백: {work_result['error'][:100]}")
                    work_result = await self._run_litellm_fallback(revision_instruction)

                if work_result.get("error"):
                    self._log("error", f"재작업 오류: {work_result['error']}")
                    self.status = "error"
                    self.error_msg = work_result["error"]
                    self.review_feedback = f"ERROR: 재작업 실패 (cycle={self.cycle}) — {work_result['error'][:300]}"
                    await self._post_to_chat(
                        f"❌ **[재작업 오류]** `{self.job_id}`\n"
                        f"Claude Code 재실행 실패: {work_result['error'][:500]}"
                    )
                    await self._save_to_db()
                    await self._trigger_ai_reaction(
                        f"[시스템] Pipeline Runner 재작업 `{self.job_id}` (프로젝트: {self.project})이 실패했습니다.\n"
                        f"오류: {work_result['error'][:300]}\n\n"
                        f"CEO에게 오류 원인과 해결 방안을 간단히 보고해주세요."
                    )
                    return

                self.result_output = work_result.get("output", "")

                # 재작업 완료 보고
                await self._post_to_chat(
                    f"🔧 **[{self.cycle}차 재작업 완료]** `{self.job_id}`\n"
                    f"출력: {self.result_output[-500:]}\n\n"
                    f"재검수 진행 중..."
                )
            else:
                self._log("max_cycles", f"최대 재지시 횟수({self.max_cycles}) 도달")
                await self._post_to_chat(
                    f"⚠️ **[최대 재지시 횟수 도달]** `{self.job_id}`\n"
                    f"{self.max_cycles}회 재지시 완료. 현재 상태로 승인 요청합니다."
                )

            # Phase 4: 승인 대기
            self.git_diff = (await self._ssh_command("git diff HEAD"))[:_MAX_DIFF_CHARS]
            self._log("awaiting_approval", "CEO 승인 대기 중. 채팅에서 승인해주세요.")
            self.status = "awaiting_approval"
            await self._save_to_db()

            # 채팅방에 승인 요청 메시지
            diff_summary = self._format_diff_summary(self.git_diff)
            await self._post_to_chat(
                f"🔔 **[CEO 승인 요청]** `{self.job_id}`\n"
                f"프로젝트: **{self.project}**\n"
                f"검수: {self.review_feedback}\n\n"
                f"**최종 변경사항:**\n{diff_summary}\n\n"
                f"---\n"
                f"승인하려면: \"승인해\" 또는 \"approve\"\n"
                f"거부하려면: \"거부해\" 또는 \"reject\""
            )

            # ★ AI 자동 반응 트리거: AI가 코드 검수 후 직접 승인/거부 도구 호출
            await self._trigger_ai_reaction(
                f"[시스템] Pipeline Runner 작업 `{self.job_id}` (프로젝트: {self.project}) AI 검수 요청.\n\n"
                f"**검수 지시 (반드시 도구 호출로 완료):**\n"
                f"1. 위 변경사항(git diff)을 꼼꼼히 검토하세요.\n"
                f"2. 필요 시 read_remote_file로 수정된 파일 전체를 확인하세요.\n"
                f"3. 검수 완료 후 반드시 아래 중 하나를 실행하세요:\n"
                f"   - 이상 없음: `pipeline_runner_approve(job_id='{self.job_id}', action='approve')` 호출\n"
                f"   - 문제 있음: `pipeline_runner_approve(job_id='{self.job_id}', action='reject', feedback='구체적 사유')` 호출\n"
                f"   - 수정 재지시: reject 후 pipeline_runner_submit으로 수정 지시 제출\n\n"
                f"**검수 기준**: 코드 품질, 보안(API 키 하드코딩 금지), 기존 기능 훼손 없음, 테스트 통과.\n"
                f"도구 호출 없이 '검수 완료' 보고 금지. 반드시 승인 또는 거부 도구를 실행하세요."
            )

        except Exception as e:
            logger.exception(f"pipeline_c_error job={self.job_id}")
            self._log("error", str(e))
            self.status = "error"
            self.error_msg = str(e)
            self.review_feedback = f"ERROR: 예외 발생 — {str(e)[:300]}"
            await self._post_to_chat(
                f"❌ **[Pipeline Runner 예외]** `{self.job_id}`\n{str(e)[:500]}"
            )
            await self._save_to_db()

            # ★ AI 자동 반응 트리거: 에러 발생 시 AI가 원인 분석 및 대안 제시
            await self._trigger_ai_reaction(
                f"[시스템] Pipeline Runner 작업 `{self.job_id}` (프로젝트: {self.project})에서 "
                f"오류가 발생했습니다: {str(e)[:300]}. "
                f"오류 원인을 분석하고 해결 방안을 제시해주세요."
            )

    async def approve(self) -> dict:
        """CEO 승인 → Phase 5~7 배포 + 검증."""
        # H-11: per-job lock to prevent race between concurrent approve/reject calls
        lock = _job_approve_locks.setdefault(self.job_id, asyncio.Lock())
        if lock.locked():
            return {"error": "이미 처리 중입니다 (approve/reject 동시 호출 방지)"}
        async with lock:
            return await self._approve_inner()

    async def _approve_inner(self) -> dict:
        if self.status != "awaiting_approval":
            return {"error": f"승인 불가 상태: {self.status}"}

        self.status = "running"
        try:
            # Phase 5: 푸시 (commit은 Runner가 작업 완료 시 이미 수행)
            # cross-process flock으로 Chat-Direct git 작업과 충돌 방지
            self._log("deploying", "git push 진행 중...")
            await self._post_to_chat(
                f"🚀 **[배포 시작]** `{self.job_id}`\n"
                f"CEO 승인 완료. git push + 서비스 재시작 진행 중..."
            )

            from app.core.git_lock import git_project_lock
            try:
                async with git_project_lock(self.project, timeout=60):
                    push_result = await self._ssh_command("git push")
                self._log("push_done", f"push 완료: {push_result[:200]}")
            except Exception as _push_err:
                # C-3: Python approve 실패 시 DB를 'approved'로 설정 → Shell Runner가 배포 이어받음
                logger.error(f"approve_push_failed: job={self.job_id} err={_push_err} → Shell Runner 폴백")
                self.status = "approved"
                await self._save_to_db()
                await self._post_to_chat(f"⚠️ **[배포 폴백]** `{self.job_id}`\ngit push 실패 — Shell Runner에 배포를 위임합니다.")
                return {"status": "approved_fallback", "error": str(_push_err)}

            # 서비스 재시작
            restart_cmd = _RESTART_CMD.get(self.project, "")

            # ★ AADS 자기수정 안전장치: 재시작 전에 모든 상태를 DB에 선저장
            if self.project == "AADS" and restart_cmd:
                self._log("aads_pre_restart", "AADS 자기수정 감지 — 재시작 전 상태 선저장")
                self.phase = "restarting"
                await self._save_to_db()
                # 완료 보고를 재시작 전에 채팅방에 미리 삽입
                await self._post_to_chat(
                    f"⚠️ **[AADS 자기수정 — 재시작]** `{self.job_id}`\n"
                    f"AADS 서비스를 재시작합니다. 잠시 연결이 끊길 수 있습니다.\n"
                    f"재시작 후 자동으로 검증을 진행합니다."
                )
                # 재시작 실행 — 디바운스로 연속 배포 시 한 번만 재시작
                await _debounced_aads_restart(self)
                # M4: 재시작 후 health polling (sleep 대신)
                for _poll in range(15):  # 최대 30초 (2초 × 15)
                    await asyncio.sleep(2)
                    _health = await self._ssh_command(
                        "curl -sf http://localhost:8080/health 2>/dev/null && echo OK || echo WAIT",
                        timeout=5, retries=1,
                    )
                    if "OK" in _health:
                        break
                else:
                    logger.warning(f"pipeline_c job={self.job_id} AADS health poll timeout after 30s")

                # 복귀 확인: health check
                verify = await self._final_verify()
                self._log("done", f"최종 완료: {verify['summary']}")
                self.status = "done"
                await self._save_to_db()
                await self._post_to_chat(
                    f"✅ **[Pipeline Runner 완료 — AADS 자기수정]** `{self.job_id}`\n"
                    f"커밋: {verify.get('last_commit', 'N/A')}\n"
                    f"Health: {verify.get('health', 'N/A')[:200]}\n"
                    f"에러: {verify.get('errors', '없음')[:200] or '없음'}\n\n"
                    f"**결과: {verify['summary']}**"
                )
                # ★ AADS 프론트엔드(dashboard) 배포 후 QA 자동 실행
                await self._run_frontend_qa_if_needed()

                return {
                    "status": "done",
                    "summary": verify["summary"],
                    "health": verify.get("health", ""),
                    "errors": verify.get("errors", ""),
                }

            # 일반 프로젝트 재시작
            if restart_cmd:
                self._log("restarting", f"서비스 재시작: {restart_cmd}")
                await self._ssh_command(restart_cmd)
                await asyncio.sleep(5)  # 재시작 대기

            # Phase 6: 최종 검증
            self._log("verifying", "최종 검증 중...")
            verify = await self._final_verify()

            # Phase 7: 완료
            self._log("done", f"최종 완료: {verify['summary']}")
            self.status = "done"
            await self._save_to_db()

            # 채팅방에 최종 완료 보고
            await self._post_to_chat(
                f"✅ **[Pipeline Runner 완료]** `{self.job_id}`\n"
                f"프로젝트: **{self.project}**\n"
                f"커밋: {verify.get('last_commit', 'N/A')}\n"
                f"Health: {verify.get('health', 'N/A')[:200]}\n"
                f"에러: {verify.get('errors', '없음')[:200] or '없음'}\n\n"
                f"**결과: {verify['summary']}**"
            )

            # AADS-1864: 매니저 AI에 QA 자동 보고
            try:
                from app.api.qa import auto_report_on_completion
                await auto_report_on_completion(
                    job_id=self.job_id,
                    project=self.project,
                    summary=verify.get("summary", ""),
                    checklist_results={
                        "service_running": "OK" in verify.get("health", ""),
                        "error_log_zero": not verify.get("errors"),
                        "health_check": verify.get("summary", "").startswith("OK"),
                    },
                )
            except Exception as e:
                logger.warning(f"pipeline_c_qa_auto_report_error job={self.job_id}: {e}")

            # ★ AADS 프론트엔드(dashboard) 배포 후 QA 자동 실행
            await self._run_frontend_qa_if_needed()

            return {
                "status": "done",
                "summary": verify["summary"],
                "health": verify.get("health", ""),
                "errors": verify.get("errors", ""),
            }

        except Exception as e:
            logger.exception(f"pipeline_c_approve_error job={self.job_id}")
            self._log("error", f"배포 중 오류: {e}")
            self.status = "error"
            self.error_msg = str(e)
            self.review_feedback = f"ERROR: 배포 중 예외 — {str(e)[:300]}"
            await self._post_to_chat(
                f"❌ **[배포 오류]** `{self.job_id}`\n{str(e)[:500]}"
            )
            await self._save_to_db()
            await self._trigger_ai_reaction(
                f"[시스템] Pipeline Runner 배포 작업 `{self.job_id}` (프로젝트: {self.project})에서 오류가 발생했습니다.\n"
                f"오류: {str(e)[:300]}\n\n"
                f"CEO에게 오류 원인과 해결 방안을 간단히 보고해주세요."
            )
            return {"error": str(e)}

    async def _run_frontend_qa_if_needed(self):
        """AADS 프로젝트 + aads-dashboard 변경 시 QA 파이프라인 자동 실행."""
        if self.project != "AADS":
            return
        if "aads-dashboard/" not in (self.git_diff or ""):
            return

        try:
            from app.services.qa_pipeline import run_full_qa
            from app.services.ceo_notify import notify_ceo

            self._log("frontend_qa", "프론트엔드(dashboard) 변경 감지 — QA 자동 실행 중...")
            await self._post_to_chat(
                f"🔍 **[프론트엔드 QA 시작]** `{self.job_id}`\n"
                f"aads-dashboard 변경 감지. 시각적 QA 검사를 실행합니다..."
            )

            qa_result = await run_full_qa(
                project_id="AADS",
                deploy_url="https://aads.newtalk.kr/",
                pages=["/", "/chat", "/ops"],
                project_context="Pipeline Runner 자동 배포 후 프론트엔드 QA",
            )

            verdict = qa_result.get("verdict", "UNKNOWN")
            is_fail = "FAIL" in verdict

            await self._post_to_chat(
                f"{'❌' if is_fail else '✅'} **[프론트엔드 QA 결과]** `{self.job_id}`\n"
                f"판정: **{verdict}**\n"
                f"디자인 점수: {qa_result.get('design_score', 'N/A')}\n"
                f"테스트: {qa_result.get('test_status', 'N/A')} | "
                f"시각: {qa_result.get('visual_status', 'N/A')}"
            )

            if is_fail:
                await notify_ceo(
                    project_id="AADS",
                    qa_result=qa_result,
                    screenshots=qa_result.get("screenshots"),
                    scorecard=qa_result.get("scorecard"),
                )
                logger.warning(f"pipeline_c_frontend_qa_fail job={self.job_id}: {verdict}")

        except Exception as e:
            logger.warning(f"pipeline_c_frontend_qa_error job={self.job_id}: {e}")

    async def reject(self, reason: str = "") -> dict:
        """CEO 거부 → 변경사항 되돌리기."""
        # H-11: per-job lock to prevent race between concurrent approve/reject calls
        lock = _job_approve_locks.setdefault(self.job_id, asyncio.Lock())
        if lock.locked():
            return {"error": "이미 처리 중입니다 (approve/reject 동시 호출 방지)"}
        async with lock:
            return await self._reject_inner(reason)

    async def _reject_inner(self, reason: str = "") -> dict:
        if self.status != "awaiting_approval":
            return {"error": f"거부 불가 상태: {self.status}"}

        self._log("rejected", f"CEO 거부: {reason}")
        # 변경사항 완전 제거 (Shell Runner는 approve 후에만 커밋하므로 reset 불필요)
        await self._ssh_command("git checkout .")
        await self._ssh_command("git clean -fd")
        self.status = "done"
        self.review_feedback = f"REJECTED: {reason}"
        await self._save_to_db()

        # 채팅방에 거부+원복 기록
        await self._post_to_chat(
            f"🚫 **[Pipeline Runner 거부]** `{self.job_id}`\n"
            f"사유: {reason or '(미지정)'}\n"
            f"변경사항이 원복되었습니다."
        )

        return {"status": "rejected", "message": "변경사항이 원복되었습니다."}

    # ─── Claude Code 프로세스 종료 헬퍼 ─────────────────────────────────────

    async def _kill_existing_claude_process(self, context: str = "pre-run"):
        """기존 Claude Code 프로세스 완전 종료 후 대기.
        동일 프로젝트 workdir에서 실행 중인 claude 프로세스를 찾아 강제 종료."""
        try:
            # 해당 workdir에서 실행 중인 claude 프로세스 PID 조회
            # [Fix-B] pgrep -f으로 실제 claude 바이너리 탐지 (nohup bash 래퍼 무관)
            # [Fix-E] workdir 기반 프로젝트 격리
            ps_cmd = "pgrep -af 'claude' 2>/dev/null || true"
            ps_out = await self._ssh_command(ps_cmd, timeout=10, retries=1)
            pids = []
            for _line in ps_out.strip().split("\n"):
                _parts = _line.strip().split(None, 1)
                if _parts and _parts[0].isdigit():
                    _cmd_part = _parts[1] if len(_parts) > 1 else ''
                    if self.workdir in _cmd_part or 'claude -p' in _cmd_part:
                        pids.append(_parts[0])

            if not pids:
                # 2차: job별 임시 PID 파일로 조회
                pid_file = f"/tmp/pipeline_c_{self.job_id}.pid"
                ps_out2 = await self._ssh_command(f"cat {pid_file} 2>/dev/null || true", timeout=10, retries=1)
                pids = [p.strip() for p in ps_out2.strip().split("\n") if p.strip().isdigit()]

            if pids:
                logger.warning(
                    f"pipeline_c_kill_existing job={self.job_id} context={context} "
                    f"killing {len(pids)} claude processes: {pids}"
                )
                kill_cmd = "kill -9 " + " ".join(pids) + " 2>/dev/null || true"
                await self._ssh_command(kill_cmd, timeout=10, retries=1)
                # 프로세스 완전 종료 대기
                await asyncio.sleep(3)
                logger.info(f"pipeline_c_kill_existing job={self.job_id} done, waited 3s")
            else:
                logger.debug(f"pipeline_c_kill_existing job={self.job_id} no existing claude processes found")
        except Exception as e:
            logger.warning(f"pipeline_c_kill_existing error job={self.job_id}: {e}")

    # ─── Claude Code CLI 실행 ───────────────────────────────────────────────

    async def _run_claude_code(self, instruction: str, continue_session: bool) -> dict:
        """
        SSH로 원격 서버의 Claude Code CLI 실행.

        동작 방식: nohup 분리 실행 + 폴링
        1. 원격 서버에서 nohup으로 Claude Code를 백그라운드 실행 (출력 → 임시 파일)
        2. 주기적으로 SSH로 프로세스 완료 여부 확인
        3. 완료되면 출력 파일을 읽어서 반환
        → SSH 연결 끊김, 30분 타임아웃 문제 없음 (최대 2시간 대기)
        """
        # 이전 Claude Code 프로세스 완전 종료 (Session ID 충돌 방지)
        await self._kill_existing_claude_process(
            context="revision" if continue_session else "initial"
        )

        escaped = shlex.quote(instruction)

        # locale + API 키 주입 (locale 미설정 시 Claude CLI 경고→exit=137 방지)
        api_key_setup = (
            "export LANG=en_US.UTF-8; export LC_ALL=en_US.UTF-8; export LANGUAGE=en_US:en; "
            "export MANPATH=; "  # manpath locale 경고 억제
            "source ~/.claude/api_keys.env 2>/dev/null; "
            "export CLAUDE_CODE_OAUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN:-$API_KEY_1}; "
            "unset ANTHROPIC_API_KEY 2>/dev/null; "
            "unset ANTHROPIC_BASE_URL 2>/dev/null; "  # LiteLLM proxy로 라우팅 방지 → 직접 Anthropic API 사용
        )

        # 고유 출력 파일 경로
        out_file = f"/tmp/pipeline_c_{self.job_id}.out"
        err_file = f"/tmp/pipeline_c_{self.job_id}.err"
        pid_file = f"/tmp/pipeline_c_{self.job_id}.pid"
        done_file = f"/tmp/pipeline_c_{self.job_id}.done"

        try:
            # Step 1: 분리 실행 시작 — 매 시도마다 새 session-id + 이전 프로세스 kill
            # SSH 타임아웃 시 nohup 프로세스가 원격에서 살아있을 수 있으므로
            # 재시도 전 반드시 이전 프로세스 kill + 새 UUID 발급
            pid_output = None
            for _detach_attempt in range(3):
                # 매 시도마다 새 session-id 발급 (재시도 시 충돌 방지)
                self.claude_session_id = str(uuid.uuid4())
                # [Fix-D] continue_session 무관하게 항상 새 세션으로 실행
                # '--session-id -c' 조합은 Claude CLI 오류 유발 → 플래그 제거
                # 재지시 시 이전 컨텍스트는 instruction 텍스트에 포함됨
                _model_flag = f" --model {self.model}" if self.model else ""
                claude_cmd = (
                    f"claude -p --output-format text"
                    f"{_model_flag} "
                    f"{escaped}"
                )
                encoded_cmd = base64.b64encode(
                    f'{claude_cmd} > {out_file} 2> {err_file}; echo $? > {done_file}'.encode()
                ).decode()
                detach_cmd = (
                    f"{api_key_setup}"
                    f"cd {shlex.quote(self.workdir)} && "
                    f"nohup bash -c \"$(echo {encoded_cmd} | base64 -d)\" "
                    f"> /dev/null 2>&1 & echo $!"
                )
                # retries=1: nohup 명령은 재시도 금지 (내부에서 직접 재시도 관리)
                pid_output = await self._ssh_command(detach_cmd, timeout=15, retries=1)
                if pid_output.strip().isdigit():
                    break  # 성공
                # 실패 시: 이전 시도에서 살아있을 수 있는 프로세스 kill 후 재시도
                logger.warning(
                    f"pipeline_c detach_attempt={_detach_attempt+1}/3 failed: {pid_output[:100]}, "
                    f"killing existing + retrying with new session-id"
                )
                await self._kill_existing_claude_process(context=f"detach_retry_{_detach_attempt+1}")
                await asyncio.sleep(2)
            remote_pid = pid_output.strip()
            if not remote_pid.isdigit():
                # 분리 실행 실패 시 직접 실행으로 폴백
                logger.warning(f"pipeline_c detach failed: {pid_output[:200]}, falling back to direct exec")
                return await self._run_claude_code_direct(instruction, continue_session)

            self._log("claude_code_detached", f"원격 분리 실행 시작 (PID={remote_pid})")

            # 채팅방에 진행 상황 보고
            await self._post_to_chat(
                f"⏳ **[작업 실행 중]** `{self.job_id}`\n"
                f"Claude Code가 원격 서버에서 실행 중입니다 (PID={remote_pid}).\n"
                f"최대 {_get_timeout_for_job(self) // 60}분까지 대기하며, 30초마다 상태를 확인합니다."
            )

            # Step 2: 폴링으로 완료 대기 (P1-1: 동적 타임아웃)
            _job_timeout = _get_timeout_for_job(self)
            elapsed = 0
            last_report = 0
            while elapsed < _job_timeout:
                await asyncio.sleep(_CLAUDE_POLL_INTERVAL)
                elapsed += _CLAUDE_POLL_INTERVAL

                # .done 파일 존재 확인 = 작업 완료
                check = await self._ssh_command(f"cat {done_file} 2>/dev/null || echo RUNNING", timeout=10)
                check = check.strip()

                if check != "RUNNING":
                    # 완료! 출력 읽기
                    exit_code = int(check) if check.isdigit() else -1
                    output = await self._ssh_command(f"tail -c 50000 {out_file} 2>/dev/null", timeout=15)
                    err = await self._ssh_command(f"cat {err_file} 2>/dev/null", timeout=10)

                    # 임시 파일 정리
                    await self._ssh_command(
                        f"rm -f {out_file} {err_file} {pid_file} {done_file}", timeout=5
                    )

                    if exit_code != 0 and not output.strip():
                        return {"error": f"exit={exit_code}: {err[:500]}", "output": ""}

                    self._log("claude_code_complete", f"완료 (exit={exit_code}, {len(output)}자, {elapsed}초)")
                    return {
                        "output": output[-_MAX_OUTPUT_CHARS:],
                        "exit_code": exit_code,
                        "error": None,
                    }

                # 10분마다 진행 상황 보고
                if elapsed - last_report >= 600:
                    last_report = elapsed
                    minutes = elapsed // 60
                    # 프로세스가 아직 살아있는지 확인
                    alive = await self._ssh_command(f"kill -0 {remote_pid} 2>&1 && echo ALIVE || echo DEAD", timeout=5)
                    if "DEAD" in alive:
                        # 프로세스 죽었는데 .done 없음 → 비정상 종료
                        output = await self._ssh_command(f"tail -c 50000 {out_file} 2>/dev/null", timeout=15)
                        err = await self._ssh_command(f"cat {err_file} 2>/dev/null", timeout=10)
                        await self._ssh_command(f"rm -f {out_file} {err_file} {pid_file} {done_file}", timeout=5)
                        return {"error": f"프로세스 비정상 종료 (PID={remote_pid}): {err[:500]}", "output": output[-_MAX_OUTPUT_CHARS:]}

                    await self._post_to_chat(
                        f"⏳ **[작업 진행 중]** `{self.job_id}` — {minutes}분 경과\n"
                        f"Claude Code가 계속 실행 중입니다 (PID={remote_pid})."
                    )

            # 최대 대기 시간 초과
            await self._ssh_command(f"kill {remote_pid} 2>/dev/null; rm -f {out_file} {err_file} {pid_file} {done_file}", timeout=5)
            return {"error": f"Claude Code 최대 대기시간 초과 ({_job_timeout // 60}분, size={self.size})", "output": ""}

        except Exception as e:
            return {"error": str(e), "output": ""}

    async def _run_claude_code_direct(self, instruction: str, continue_session: bool) -> dict:
        """직접 실행 폴백 (분리 실행 불가 시)."""
        # 이전 Claude Code 프로세스 완전 종료 (Session ID 충돌 방지)
        await self._kill_existing_claude_process(context="direct-fallback")

        escaped = shlex.quote(instruction)

        # 항상 새 session-id 발급 ("Session ID already in use" 충돌 근본 방지)
        self.claude_session_id = str(uuid.uuid4())
        _model_flag = f" --model {self.model}" if self.model else ""
        # [Fix D-2] continue_session 분기 제거 - 항상 새 세션
        claude_cmd = (
            f"claude -p --output-format text"
            f"{_model_flag} "
            f"--session-id {self.claude_session_id} "
            f"{escaped}"
        )

        api_key_setup_direct = (
            "export LANG=en_US.UTF-8; export LC_ALL=en_US.UTF-8; export LANGUAGE=en_US:en; "
            "export MANPATH=; "
            "source ~/.claude/api_keys.env 2>/dev/null; "
            "export CLAUDE_CODE_OAUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN:-$API_KEY_1}; "
            "unset ANTHROPIC_API_KEY 2>/dev/null; "
            "unset ANTHROPIC_BASE_URL 2>/dev/null; "  # LiteLLM proxy로 라우팅 방지
        )
        full_cmd = f"{api_key_setup_direct}cd {shlex.quote(self.workdir)} && {claude_cmd}"

        proc = None
        try:
            if self.server == "localhost":
                proc = await asyncio.create_subprocess_shell(
                    full_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                    "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                    "-p", self.ssh_port,
                    f"root@{self.server}", full_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CLAUDE_TIMEOUT
            )
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0 and not output.strip():
                return {"error": f"exit={proc.returncode}: {err[:500]}", "output": ""}

            return {
                "output": output[-_MAX_OUTPUT_CHARS:],
                "exit_code": proc.returncode,
                "error": None,
            }

        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return {"error": f"Claude Code 타임아웃 ({_CLAUDE_TIMEOUT}초)", "output": ""}
        except Exception as e:
            return {"error": str(e), "output": ""}

    # ─── AADS-234: LiteLLM Runner 폴백 ─────────────────────────────────────

    async def _run_litellm_fallback(self, instruction: str, override_model: str = "") -> dict:
        """LiteLLM Runner 실행. AADS: 로컬 | GO100/KIS/SF/NTV2: SSH 원격 실행.
        override_model: CEO가 직접 지정한 모델명 (비어있으면 size 기반 자동 선택)."""
        model = override_model or _LITELLM_FALLBACK_MODELS.get(self.size, "qwen3-coder-plus")
        is_remote = self.server not in ("localhost", "host.docker.internal")

        self._log("litellm_fallback", f"LiteLLM Runner 시작 (project={self.project}, model={model}, remote={is_remote})")
        await self._post_to_chat(
            f"🔄 **[LiteLLM Runner]** `{self.job_id}`\n"
            f"프로젝트: **{self.project}** | 모델: **{model}**"
        )

        escaped_instruction = shlex.quote(instruction)
        escaped_workdir = shlex.quote(self.workdir)

        if is_remote:
            # GO100/KIS/SF/NTV2: SSH 원격 실행
            runner_path = _LITELLM_RUNNER_PATH.get(self.project)
            if not runner_path:
                return {"error": f"LiteLLM Runner 미지원 프로젝트: {self.project}", "output": ""}
            litellm_key = os.getenv("LITELLM_MASTER_KEY", "")
            cmd_on_remote = (
                f"LITELLM_MASTER_KEY={shlex.quote(litellm_key)} "
                f"/root/litellm-venv/bin/python3 {runner_path} "
                f"--model {model} "
                f"-i {escaped_instruction} "
                f"-w {escaped_workdir}"
            )
            try:
                output = await self._ssh_command(cmd_on_remote, timeout=_get_timeout_for_job(self))
                self.actual_model = model  # litellm: 접두사 없이 정식명 저장
                self._log("litellm_fallback_done", f"LiteLLM 완료 ({len(output)}자, model={model}, SSH)")
                return {"output": output[-_MAX_OUTPUT_CHARS:], "exit_code": 0, "error": None}
            except asyncio.TimeoutError:
                return {"error": f"LiteLLM SSH 타임아웃 ({_get_timeout_for_job(self) // 60}분)", "output": ""}
            except Exception as e:
                return {"error": f"LiteLLM SSH 오류: {e}", "output": ""}
        else:
            # AADS: 로컬 컨테이너 내부 실행
            cmd = (
                f"python3 /app/scripts/litellm_runner.py "
                f"--model {model} "
                f"-i {escaped_instruction} "
                f"-w {escaped_workdir}"
            )
            proc = None
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                timeout = _get_timeout_for_job(self)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                output = stdout.decode("utf-8", errors="replace")
                err = stderr.decode("utf-8", errors="replace")
                if proc.returncode != 0 and not output.strip():
                    return {"error": f"LiteLLM exit={proc.returncode}: {err[:500]}", "output": ""}
                self.actual_model = model  # litellm: 접두사 없이 정식명 저장
                self._log("litellm_fallback_done", f"LiteLLM 완료 ({len(output)}자, model={model})")
                return {"output": output[-_MAX_OUTPUT_CHARS:], "exit_code": proc.returncode, "error": None}
            except asyncio.TimeoutError:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                return {"error": f"LiteLLM 타임아웃 ({_get_timeout_for_job(self) // 60}분)", "output": ""}
            except Exception as e:
                return {"error": f"LiteLLM 오류: {e}", "output": ""}

    # ─── AI 검수 ────────────────────────────────────────────────────────────

    async def _ai_review(self) -> dict:
        """AADS AI가 Claude Code 작업 결과를 자동 검수."""
        from app.core.anthropic_client import call_background_llm

        diff_text = self.git_diff[:3000] if self.git_diff else "(변경사항 없음)"
        output_text = self.result_output[:2000] if self.result_output else "(출력 없음)"

        review_prompt = f"""당신은 코드 리뷰어입니다. Claude Code 작업 결과를 검수하세요.

## 원래 지시
{self.instruction}

## Claude Code 출력 (마지막 부분)
{output_text}

## Git Diff
{diff_text}

## 검수 기준
1. 원래 지시 사항이 정확히 반영됐는가?
2. 명백한 버그가 새로 생기지 않았는가?
3. 변경사항이 없으면 FAIL (작업 미수행)

반드시 아래 JSON 형식으로만 응답하세요:
{{"verdict": "PASS 또는 FAIL", "summary": "한줄 요약", "feedback": "수정이 필요한 구체적 내용 (PASS면 빈 문자열)"}}"""

        response_text = ""
        try:
            response_text = await call_background_llm(
                prompt=review_prompt,
                system="코드 리뷰어. JSON으로만 응답.",
                max_tokens=1024,
            )
            # JSON 추출 (5단계 폴백 파싱)
            text = response_text.strip()
            # 1단계: 코드펜스 제거
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            # 2단계: 직접 파싱 시도
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            # 3단계: JSON 객체 패턴 추출
            import re
            m = re.search(r'\{[\s\S]*\}', text)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
            # 4단계: 줄 단위에서 JSON 찾기
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            # 5단계: 키워드 기반 verdict 추출
            verdict = "PASS" if any(k in text.upper() for k in ["PASS", "통과", "정상"]) else "FAIL"
            return {"verdict": verdict, "summary": "폴백 파싱으로 verdict 추출", "feedback": text[:500]}
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"ai_review_parse_failed: {e}, raw={response_text[:300] if response_text else 'N/A'}")
            return {
                "verdict": "DELEGATED",
                "summary": "LLM 검수 호출 실패 — 채팅 AI에게 검수 위임",
                "feedback": "",
                "parse_error": str(e),
            }

    # ─── 최종 검증 ──────────────────────────────────────────────────────────

    async def _final_verify(self) -> dict:
        """배포 후 최종 검증."""
        results = {}

        # health check
        health_cmd = "curl -sf http://localhost:8080/health 2>/dev/null || curl -sf http://localhost:8000/health 2>/dev/null || echo NO_HEALTH_ENDPOINT"
        results["health"] = await self._ssh_command(health_cmd)

        # 에러 로그
        results["errors"] = await self._ssh_command(
            "journalctl --since '3 min ago' --no-pager 2>/dev/null | grep -i error | tail -5"
        )

        # git log (최종 커밋 확인)
        results["last_commit"] = await self._ssh_command("git log --oneline -1")

        all_ok = (
            "error" not in results["health"].lower()
            and not results["errors"].strip()
        )
        results["summary"] = (
            f"{'정상' if all_ok else '확인 필요'} | "
            f"commit: {results['last_commit'].strip()} | "
            f"health: {'OK' if all_ok else results['health'][:100]}"
        )
        return results

    # ─── SSH 유틸 ───────────────────────────────────────────────────────────

    async def _ssh_command(self, command: str, timeout: int = 30, retries: int = 0) -> str:
        """원격 서버 명령 실행 (내부용, 보안 화이트리스트 없음 — 오케스트레이터 전용).
        M1: SSH 실패 시 지수 백오프 재시도 (retries=0이면 _SSH_MAX_RETRIES 사용)."""
        max_retries = retries or _SSH_MAX_RETRIES
        full_cmd = f"cd {shlex.quote(self.workdir)} && {command}"

        for attempt in range(max_retries):
            proc = None
            try:
                if self.server == "localhost":
                    proc = await asyncio.create_subprocess_shell(
                        full_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                else:
                    proc = await asyncio.create_subprocess_exec(
                        "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                        "-p", self.ssh_port,
                        f"root@{self.server}", full_cmd,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                out = stdout.decode("utf-8", errors="replace")
                if len(out) > _MAX_OUTPUT_CHARS:
                    out = out[-_MAX_OUTPUT_CHARS:]
                return out
            except asyncio.TimeoutError:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                if attempt < max_retries - 1:
                    delay = _SSH_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"ssh_retry job={self.job_id} attempt={attempt+1}/{max_retries} timeout={timeout}s delay={delay}s cmd={command[:80]}")
                    await asyncio.sleep(delay)
                    continue
                return f"[TIMEOUT {timeout}s after {max_retries} retries]"
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = _SSH_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"ssh_retry job={self.job_id} attempt={attempt+1}/{max_retries} error={e} delay={delay}s")
                    await asyncio.sleep(delay)
                    continue
                return f"[ERROR after {max_retries} retries] {e}"
        return "[ERROR] max retries exhausted"

    # ─── DB 저장 ────────────────────────────────────────────────────────────

    async def _save_to_db(self):
        """현재 상태를 DB에 저장."""
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO pipeline_jobs
                        (job_id, chat_session_id, project, instruction, claude_session_id,
                         phase, cycle, max_cycles, status, logs, result_output, git_diff,
                         review_feedback, worker_model, parallel_group, depends_on,
                         actual_model, size, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,now())
                    ON CONFLICT (job_id) DO UPDATE SET
                        phase = EXCLUDED.phase,
                        cycle = EXCLUDED.cycle,
                        status = EXCLUDED.status,
                        logs = EXCLUDED.logs,
                        result_output = EXCLUDED.result_output,
                        git_diff = EXCLUDED.git_diff,
                        review_feedback = EXCLUDED.review_feedback,
                        actual_model = EXCLUDED.actual_model,
                        size = EXCLUDED.size,
                        updated_at = now()
                """,
                    self.job_id, self.chat_session_id, self.project,
                    self.instruction, self.claude_session_id,
                    self.phase, self.cycle, self.max_cycles, self.status,
                    json.dumps(self.logs, ensure_ascii=False),
                    (self.result_output or "")[:10000],
                    (self.git_diff or "")[:10000],
                    self.review_feedback or "",
                    self.worker_model or None,
                    self.parallel_group or None,
                    self.depends_on or None,
                    self.actual_model or None,
                    getattr(self, "size", "M"),
                )
            # 작업 완료/실패 이벤트
            if self.status in ("done", "error"):
                try:
                    from app.services.task_logger import emit_task_completed
                    await emit_task_completed(self.job_id, self.status, self.review_feedback or self.error_msg or "")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"pipeline_c_save_db_error job={self.job_id}: {e}")


# ─── 프로젝트 → 세션 자동 매핑 ─────────────────────────────────────────────────

async def _find_recent_session(project: str) -> str:
    """프로젝트의 워크스페이스에서 가장 최근 활성 세션 ID를 찾는다.
    워크스페이스 이름이 [KIS], [NTV2] 형태이므로 `[PROJECT]` 패턴으로 검색.
    MCP / 독립 프로세스 경로에서 pool 미초기화 시 asyncpg 직접 연결로 폴백."""

    async def _query(conn, proj: str) -> str:
        ws_pattern = f"\\[{proj.upper()}\\]%"
        row = await conn.fetchrow(
            """
            SELECT cs.id
            FROM chat_sessions cs
            JOIN chat_workspaces cw ON cs.workspace_id = cw.id
            WHERE cw.name LIKE $1
            ORDER BY cs.updated_at DESC
            LIMIT 1
            """,
            ws_pattern,
        )
        if row:
            return str(row["id"])
        # 폴백: CEO 통합지시 세션 (프로젝트별 워크스페이스 없을 때)
        row = await conn.fetchrow(
            """
            SELECT cs.id
            FROM chat_sessions cs
            JOIN chat_workspaces cw ON cs.workspace_id = cw.id
            WHERE cw.name LIKE '\\[CEO\\]%'
            ORDER BY cs.updated_at DESC
            LIMIT 1
            """,
        )
        return str(row["id"]) if row else ""

    try:
        from app.core.db_pool import get_pool
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                return await _query(conn, project)
        except RuntimeError:
            # pool 미초기화 (MCP / 독립 프로세스 경로) → 직접 연결
            import asyncpg as _asyncpg
            import os as _os
            db_url = _os.getenv("DATABASE_URL", "")
            if not db_url:
                logger.warning(f"_find_recent_session: pool 없음 + DATABASE_URL 미설정, project={project}")
                return ""
            logger.info(f"_find_recent_session: pool 미초기화, 직접 연결 시도 project={project}")
            _conn = await _asyncpg.connect(db_url)
            try:
                return await _query(_conn, project)
            finally:
                await _conn.close()
    except Exception as e:
        logger.warning(f"_find_recent_session error for {project}: {e}", exc_info=True)
        return ""


# ─── AADS-1864: 검증 체크리스트 ──────────────────────────────────────────────

_VERIFICATION_CHECKLIST_TEMPLATE = """

## 검증 체크리스트 (완료 필수 조건)
- [ ] 구현 목표: (무엇을 구현했는지 1줄 요약)
- [ ] 검증 방법: (curl 명령 또는 URL 또는 UI 셀렉터)
- [ ] 완료 기준: (어떤 응답/결과가 나와야 완료인지)
- [ ] 실패 기준: (이런 결과면 실패로 간주)
- [ ] 서비스 재시작 확인: docker ps → container running
- [ ] 에러 로그 0건: docker logs --since 60s | grep -i error

RESULT 파일에 위 체크리스트 항목별 실행 결과를 반드시 포함하세요.
"""


def _append_verification_checklist(instruction: str, project: str) -> str:
    """지시서 끝에 검증 체크리스트 자동 append (AADS-1864)."""
    if "검증 체크리스트" in instruction:
        return instruction  # 이미 포함된 경우 중복 방지
    return instruction.rstrip() + _VERIFICATION_CHECKLIST_TEMPLATE


# ─── 외부 API 함수 ────────────────────────────────────────────────────────────

async def start_pipeline(
    project: str,
    instruction: str,
    chat_session_id: str,
    max_cycles: int = 3,
    dsn: str = "",
    model: str = "",
    worker_model: str = "",
    parallel_group: str = "",
    depends_on: str = "",
) -> dict:
    """Pipeline Runner 시작 (asyncio.create_task로 백그라운드 실행).
    AADS-211: worker_model(직접 모델 지정), parallel_group(병렬 실행), depends_on(의존성)."""
    logger.info(f"[DIAG] start_pipeline: chat_session_id='{chat_session_id}' project={project}"
                f" worker_model={worker_model} parallel_group={parallel_group} depends_on={depends_on}")

    # chat_session_id가 비어있으면 해당 프로젝트 워크스페이스의 최근 세션을 자동 조회
    if not chat_session_id:
        chat_session_id = await _find_recent_session(project)
        if chat_session_id:
            logger.info(f"pipeline_c: auto-resolved session_id='{chat_session_id}' for project={project}")
        else:
            logger.warning(f"pipeline_c: 프로젝트 {project}의 활성 세션을 찾을 수 없음 — 채팅 보고 비활성")

    job = PipelineCJob(
        project=project,
        instruction=instruction,
        chat_session_id=chat_session_id,
        max_cycles=max_cycles,
        dsn=dsn,
        model=model,
        worker_model=worker_model,
        parallel_group=parallel_group,
        depends_on=depends_on,
    )
    _active_jobs[job.job_id] = job

    # 이벤트 루프 내 create_task (별도 스레드 불필요)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_job_async(job))
    except RuntimeError:
        # 이벤트 루프가 없는 경우 (테스트 등) — 스레드 폴백
        _run_job_in_thread(job)

    return {
        "job_id": job.job_id,
        "project": job.project,
        "status": "started",
        "message": f"Pipeline Runner 시작됨. 작업 완료 후 채팅방에 보고됩니다. job_id: {job.job_id}",
    }


async def _run_job_async(job: PipelineCJob):
    """asyncio.create_task용 래퍼 — DB 풀 공유 가능."""
    try:
        await job.run()
    except Exception as e:
        logger.error(f"pipeline_c_task_error job={job.job_id}: {e}")


def _run_job_in_thread(job: PipelineCJob):
    """폴백: 별도 스레드에서 이벤트 루프를 만들어 파이프라인 실행."""
    import threading
    def _worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(job.run())
        except Exception as e:
            logger.error(f"pipeline_c_thread_error job={job.job_id}: {e}")
        finally:
            loop.close()
    t = threading.Thread(target=_worker, name=f"pipeline-{job.job_id}", daemon=True)
    t.start()
    logger.info(f"pipeline_c_started job={job.job_id} thread={t.name}")


async def get_pipeline_status(job_id: str) -> dict:
    """파이프라인 상태 조회."""
    job = get_job(job_id)
    if job:
        return job.to_dict()

    # 메모리에 없으면 DB 조회
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM pipeline_jobs WHERE job_id = $1", job_id
            )
            if row:
                return {
                    "job_id": row["job_id"],
                    "project": row["project"],
                    "instruction": row["instruction"][:200],
                    "phase": row["phase"],
                    "cycle": row["cycle"],
                    "status": row["status"],
                    "logs": json.loads(row["logs"]) if row["logs"] else [],
                    "git_diff": (row["git_diff"] or "")[:2000],
                    "review_feedback": row["review_feedback"] or "",
                    "created_at": row["created_at"].isoformat(),
                }
    except Exception as e:
        logger.error(f"get_pipeline_status_db_error: {e}")

    return {"error": f"작업을 찾을 수 없음: {job_id}"}


async def list_pipelines(chat_session_id: str = None) -> list:
    """활성 Runner 작업 목록."""
    return list_jobs(chat_session_id)


async def approve_pipeline(job_id: str) -> dict:
    """파이프라인 승인 → 배포 실행."""
    job = get_job(job_id)
    if not job:
        return {"error": f"활성 작업을 찾을 수 없음: {job_id}"}
    return await job.approve()


async def reject_pipeline(job_id: str, reason: str = "") -> dict:
    """파이프라인 거부 → 변경사항 원복."""
    job = get_job(job_id)
    if not job:
        return {"error": f"활성 작업을 찾을 수 없음: {job_id}"}
    return await job.reject(reason)


async def cancel_pipeline(job_id: str) -> dict:
    """파이프라인 강제 취소: 원격 Claude 프로세스 kill + 상태 error 전환."""
    job = get_job(job_id)
    killed_pids = []

    # 메모리에 있으면 원격 프로세스 kill 시도
    if job:
        try:
            # claude_session_id로 프로세스 찾아 kill
            ps_out = await job._ssh_command(
                f"ps aux | grep 'session-id {job.claude_session_id}' | grep -v grep | awk '{{print $2}}'",
                timeout=10,
            )
            pids = [p.strip() for p in ps_out.strip().split("\n") if p.strip().isdigit()]
            if pids:
                await job._ssh_command(f"kill {' '.join(pids)}", timeout=10)
                killed_pids = pids
        except Exception as e:
            logger.warning(f"cancel_pipeline kill error job={job_id}: {e}")

        job.status = "error"
        job.phase = "cancelled"
        job.error_msg = "CEO/AI에 의해 강제 취소됨"
        job.review_feedback = "CANCELLED: CEO/AI에 의해 강제 취소됨"
        await job._save_to_db()
        if job_id in _active_jobs:
            del _active_jobs[job_id]

        return {
            "status": "cancelled",
            "job_id": job_id,
            "killed_pids": killed_pids,
            "message": f"작업 취소 완료. Kill된 프로세스: {killed_pids or '없음'}",
        }

    # 메모리에 없으면 DB에서 조회 후 상태만 업데이트
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT project, status, phase FROM pipeline_jobs WHERE job_id = $1", job_id
            )
            if not row:
                return {"error": f"작업을 찾을 수 없음: {job_id}"}
            if row["status"] in ("done", "error") and row["phase"] == "cancelled":
                return {"error": f"이미 취소된 작업: {job_id}"}

            await conn.execute(
                "UPDATE pipeline_jobs SET status='error', phase='cancelled', "
                "review_feedback=COALESCE(review_feedback,'')||' | 강제취소', updated_at=now() "
                "WHERE job_id=$1",
                job_id,
            )
            return {
                "status": "cancelled",
                "job_id": job_id,
                "killed_pids": [],
                "message": f"DB 상태 취소 처리 완료 (메모리에 없어 프로세스 kill 미수행, 필요 시 수동 kill)",
            }
    except Exception as e:
        return {"error": f"취소 실패: {e}"}


async def retry_pipeline(job_id: str) -> dict:
    """에러/취소된 파이프라인을 동일 지시로 재실행."""
    # 먼저 메모리에서 찾기
    job = get_job(job_id)
    if job:
        if job.status not in ("error", "done"):
            return {"error": f"재실행 불가 — 현재 상태: {job.status}/{job.phase}. 먼저 cancel 필요."}
        project = job.project
        instruction = job.instruction
        chat_session_id = job.chat_session_id
        max_cycles = job.max_cycles
        dsn = getattr(job, "dsn", "")
        # 기존 작업 메모리 정리
        if job_id in _active_jobs:
            del _active_jobs[job_id]
    else:
        # DB에서 조회
        try:
            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT project, instruction, chat_session_id, max_cycles, status, phase FROM pipeline_jobs WHERE job_id = $1",
                    job_id,
                )
                if not row:
                    return {"error": f"작업을 찾을 수 없음: {job_id}"}
                if row["status"] not in ("error", "done"):
                    return {"error": f"재실행 불가 — 현재 상태: {row['status']}/{row['phase']}. 먼저 cancel 필요."}
                project = row["project"]
                instruction = row["instruction"]
                chat_session_id = row["chat_session_id"] or ""
                max_cycles = row["max_cycles"] or 3
                dsn = ""
        except Exception as e:
            return {"error": f"DB 조회 실패: {e}"}

    # 새 파이프라인 시작 (동일 지시)
    result = await start_pipeline(
        project=project,
        instruction=instruction,
        chat_session_id=chat_session_id,
        max_cycles=max_cycles,
        dsn=dsn,
    )
    result["retry_from"] = job_id
    result["message"] = f"재실행 시작 (원본: {job_id}). 새 job_id: {result['job_id']}"
    return result


# ─── AADS 자기수정 복구: 재시작으로 중단된 작업 검출 + 완료 처리 ──────────────

async def recover_interrupted_jobs():
    """
    서버 시작 시 호출:
    1. restarting phase에서 중단된 AADS 파이프라인을 복구
    2. running/queued 상태로 남아있는 고아 pipeline_jobs를 error 처리
    3. in_progress 상태로 24시간 이상 남아있는 고아 directive_lifecycle을 failed 처리
    """
    logger.info("pipeline_c_recovery: starting recover_interrupted_jobs")
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            # ── Phase 0: 고아 pipeline_jobs 정리 (running/queued → error) + 채팅방 알림 ──
            # claude_code_detached는 원격 프로세스가 완료됐을 수 있으므로 Phase 0.5에서 별도 처리
            orphan_rows = await conn.fetch(
                """
                SELECT job_id, chat_session_id, project, substring(instruction from 1 for 100) as instr
                FROM pipeline_jobs
                WHERE status = 'running' AND phase NOT IN ('restarting', 'done', 'error', 'claude_code_detached')
                """
            )
            orphan_count = await conn.execute(
                """
                UPDATE pipeline_jobs
                SET status = 'error', phase = 'error',
                    review_feedback = COALESCE(review_feedback, '') || ' | 서버 재시작으로 중단됨',
                    updated_at = now()
                WHERE status = 'running' AND phase NOT IN ('restarting', 'done', 'error', 'claude_code_detached')
                """
            )
            if orphan_count and orphan_count != "UPDATE 0":
                logger.info(f"pipeline_c_recovery: orphan pipeline_jobs cleaned: {orphan_count}")
                # 채팅방에 중단 알림 전송 (같은 세션에 최근 1시간 내 동일 중단 메시지 있으면 중복 방지)
                for orow in orphan_rows:
                    sid = orow.get("chat_session_id")
                    if not sid:
                        continue
                    try:
                        # 중복 체크: 같은 세션에 최근 1시간 내 Pipeline Runner 중단 메시지가 있는지
                        _instr_preview = orow.get('instr', '')[:60]
                        _dup_count = await conn.fetchval(
                            """SELECT count(*) FROM chat_messages
                               WHERE session_id = $1::uuid
                                 AND content LIKE '%Pipeline Runner 중단%'
                                 AND content LIKE $2
                                 AND created_at > NOW() - INTERVAL '1 hour'""",
                            sid, f"%{_instr_preview[:40]}%",
                        )
                        if _dup_count and _dup_count > 0:
                            logger.info(f"pipeline_c_recovery: skip duplicate interrupt msg for {orow['job_id']}")
                            continue

                        await conn.execute(
                            """
                            INSERT INTO chat_messages
                                (session_id, role, content, intent, cost,
                                 tokens_in, tokens_out, attachments, sources, tools_called)
                            VALUES ($1::uuid, 'assistant', $2, 'pipeline_c', 0,
                                    0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                            """,
                            sid,
                            f"⚠️ **[Pipeline Runner 중단]** `{orow['job_id']}`\n"
                            f"프로젝트: **{orow.get('project', '?')}**\n"
                            f"사유: 서버 재시작으로 중단됨\n"
                            f"작업: {orow.get('instr', '')[:200]}\n\n"
                            f"재실행이 필요하면 다시 지시해주세요.",
                        )
                        await conn.execute(
                            "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                            sid,
                        )
                    except Exception as post_err:
                        logger.warning(f"pipeline_c_recovery: chat post failed for {orow['job_id']}: {post_err}")

            # ── Phase 0.5: detached 작업 복구 — 원격 .done 파일 확인 후 결과 수거 ──
            detached_rows = await conn.fetch(
                """
                SELECT job_id, chat_session_id, project, substring(instruction from 1 for 200) as instr
                FROM pipeline_jobs
                WHERE status = 'running' AND phase = 'claude_code_detached'
                """
            )
            for drow in detached_rows:
                _djob_id = drow["job_id"]
                _dproject = drow.get("project", "?")
                _dsid = drow.get("chat_session_id", "")
                try:
                    _conf = PROJECT_MAP.get(_dproject.upper(), {})
                    _server = _conf.get("server", "")
                    _ssh_port = _conf.get("port", "22")
                    _done_file = f"/tmp/pipeline_c_{_djob_id}.done"
                    _out_file = f"/tmp/pipeline_c_{_djob_id}.out"

                    if _server:
                        # 원격 서버에서 .done 파일 확인
                        _check_cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p {_ssh_port} root@{_server} 'cat {_done_file} 2>/dev/null'"
                    else:
                        # 로컬 (AADS)
                        _check_cmd = f"cat {_done_file} 2>/dev/null"

                    _proc = await asyncio.create_subprocess_shell(
                        _check_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _stdout, _ = await asyncio.wait_for(_proc.communicate(), timeout=15)
                    _exit_code_str = _stdout.decode("utf-8", errors="replace").strip()

                    if _exit_code_str != "":
                        # .done 파일 존재 → 원격 완료! 결과 수거
                        if _server:
                            _read_cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p {_ssh_port} root@{_server} 'cat {_out_file} 2>/dev/null'"
                        else:
                            _read_cmd = f"cat {_out_file} 2>/dev/null"
                        _rproc = await asyncio.create_subprocess_shell(
                            _read_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _rstdout, _ = await asyncio.wait_for(_rproc.communicate(), timeout=15)
                        _result_text = _rstdout.decode("utf-8", errors="replace").strip()[:_MAX_OUTPUT_CHARS]
                        _success = _exit_code_str == "0"
                        _status = "done" if _success else "error"

                        await conn.execute(
                            """
                            UPDATE pipeline_jobs
                            SET status = $2, phase = $2,
                                review_feedback = COALESCE(review_feedback, '') || $3,
                                updated_at = now()
                            WHERE job_id = $1
                            """,
                            _djob_id, _status,
                            f" | 서버재시작후 결과수거: exit={_exit_code_str} output={len(_result_text)}자",
                        )

                        # 채팅방에 결과 보고
                        if _dsid:
                            _emoji = "✅" if _success else "⚠️"
                            _chat_msg = (
                                f"{_emoji} **[Pipeline Runner 결과 수거]** `{_djob_id}`\n"
                                f"프로젝트: **{_dproject}**\n"
                                f"서버 재시작 중 원격 작업이 완료되어 결과를 수거했습니다.\n\n"
                                f"**결과:**\n{_result_text[:1500]}"
                            )
                            try:
                                await conn.execute(
                                    """
                                    INSERT INTO chat_messages
                                        (session_id, role, content, intent, cost,
                                         tokens_in, tokens_out, attachments, sources, tools_called)
                                    VALUES ($1::uuid, 'assistant', $2, 'pipeline_c', 0,
                                            0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                                    """,
                                    _dsid, _chat_msg,
                                )
                                await conn.execute(
                                    "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                                    _dsid,
                                )
                            except Exception as _chat_err:
                                logger.warning(f"pipeline_c_recovery_detached_chat_error: {_chat_err}")

                            # AI 자동 반응 트리거
                            try:
                                from app.services.chat_service import trigger_ai_reaction
                                await trigger_ai_reaction(
                                    _dsid,
                                    f"[시스템] Pipeline Runner 작업 `{_djob_id}` (프로젝트: {_dproject})이 완료되었습니다. "
                                    f"결과: {_result_text[:500]}\n\n위 결과를 확인하고 필요한 후속 조치가 있으면 보고해주세요."
                                )
                            except Exception as _trig_err:
                                logger.warning(f"pipeline_c_recovery_trigger_error: {_trig_err}")

                        logger.info(f"pipeline_c_recovery: detached job {_djob_id} completed (exit={_exit_code_str}, output={len(_result_text)}자)")
                    else:
                        # .done 파일 없음 → 아직 실행 중이거나 실패
                        # 생성 후 2시간 이상이면 error 처리
                        _age_row = await conn.fetchrow(
                            "SELECT EXTRACT(EPOCH FROM (NOW() - created_at)) as age_sec FROM pipeline_jobs WHERE job_id = $1",
                            _djob_id,
                        )
                        _age = _age_row["age_sec"] if _age_row else 0
                        if _age > _CLAUDE_MAX_WAIT:
                            await conn.execute(
                                """
                                UPDATE pipeline_jobs
                                SET status = 'error', phase = 'error',
                                    review_feedback = COALESCE(review_feedback, '') || ' | 서버 재시작 후 원격 작업 타임아웃',
                                    updated_at = now()
                                WHERE job_id = $1
                                """,
                                _djob_id,
                            )
                            logger.warning(f"pipeline_c_recovery: detached job {_djob_id} timed out ({_age:.0f}s)")
                        else:
                            # 아직 실행 중일 수 있음 — 폴링 재개
                            logger.info(f"pipeline_c_recovery: detached job {_djob_id} still running ({_age:.0f}s), resuming polling")
                            # 폴링 재개를 위한 백그라운드 태스크 생성
                            asyncio.get_running_loop().create_task(
                                _resume_detached_polling(_djob_id, _dproject, _dsid, drow.get("instr", ""))
                            )

                except Exception as _derr:
                    logger.error(f"pipeline_c_recovery_detached_error: job={_djob_id} err={_derr}")
                    await conn.execute(
                        """
                        UPDATE pipeline_jobs
                        SET status = 'error', phase = 'error',
                            review_feedback = COALESCE(review_feedback, '') || $2,
                            updated_at = now()
                        WHERE job_id = $1
                        """,
                        _djob_id, f" | 복구 실패: {str(_derr)[:200]}",
                    )

            # ── Phase 0b: 장기 방치 awaiting_approval → 채팅 AI 재트리거 (텔레그램 제거) ──
            stale_approval_rows = await conn.fetch(
                """
                SELECT job_id, project, chat_session_id, substring(instruction from 1 for 100) as instr
                FROM pipeline_jobs
                WHERE status = 'awaiting_approval'
                  AND updated_at < NOW() - INTERVAL '30 minutes'
                """
            )
            for srow in stale_approval_rows:
                _sjob = srow["job_id"]
                _sproj = srow.get("project", "?")
                _sinstr = srow.get("instr", "")
                _ssid = srow.get("chat_session_id") or ""
                try:
                    if _ssid:
                        from app.services.chat_service import trigger_ai_reaction
                        await trigger_ai_reaction(
                            _ssid,
                            f"[시스템] 승인 대기 30분 초과 — `{_sjob}` ({_sproj}) 재검수 요청.\n"
                            f"작업: {_sinstr[:80]}\n\n"
                            f"pipeline_runner_approve(job_id='{_sjob}', action='approve' 또는 'reject')로 즉시 처리하세요."
                        )
                    logger.info(f"pipeline_c_stale_approval_retrigger: {_sjob}")
                except Exception as _stale_err:
                    logger.warning(f"pipeline_c_stale_approval_retrigger_error: {_sjob}: {_stale_err}")

            # ── Phase 0a: 서버 재시작으로 중단된 작업 자동 재실행 ──
            # 최근 30분 내 중단 작업 → 원본 instruction 추출 → 최대 2회 재실행
            resume_rows = await conn.fetch(
                """
                SELECT job_id, chat_session_id, project, instruction, cycle, max_cycles, model_used
                FROM (
                    SELECT pj.*,
                           (SELECT model_used FROM chat_messages
                            WHERE session_id = pj.chat_session_id::uuid AND model_used IS NOT NULL
                            ORDER BY created_at DESC LIMIT 1) as model_used
                    FROM pipeline_jobs pj
                    WHERE pj.status = 'error'
                      AND pj.review_feedback LIKE '%서버 재시작으로 중단%'
                      AND pj.review_feedback NOT LIKE '%자동 재실행됨%'
                      AND pj.created_at > NOW() - INTERVAL '30 minutes'
                      AND pj.cycle < pj.max_cycles
                ) sub
                ORDER BY created_at DESC LIMIT 3
                """
            )
            _auto_resumed = 0
            for rrow in resume_rows:
                _rjob_id = rrow["job_id"]
                _rproject = rrow.get("project", "")
                _rsid = rrow.get("chat_session_id", "")
                _rinstr = rrow.get("instruction", "")
                _rcycle = rrow.get("cycle", 0)
                _rmax = rrow.get("max_cycles", 3)

                if not _rinstr or not _rproject:
                    continue

                # 원본 instruction 추출 — [시스템: ...] 프리픽스 반복 제거
                import re as _re
                _original_instr = _re.sub(
                    r'\[시스템: 서버 재시작으로 이전 작업\([^)]+\)이 중단되었습니다\.[^\]]*\]\s*',
                    '', _rinstr
                ).strip()
                if not _original_instr:
                    continue

                # 같은 원본 instruction으로 이미 2회 이상 재실행됐는지 확인 (무한루프 방지)
                _retry_count = await conn.fetchval(
                    """SELECT count(*) FROM pipeline_jobs
                       WHERE project = $1
                         AND instruction LIKE '%' || left($2, 80) || '%'
                         AND review_feedback LIKE '%자동 재실행됨%'
                         AND created_at > NOW() - INTERVAL '2 hours'""",
                    _rproject, _original_instr[:80],
                )
                if _retry_count and _retry_count >= 2:
                    logger.info(f"pipeline_c_auto_resume_skip: {_rjob_id} already retried {_retry_count}x, skipping")
                    continue

                try:
                    # 이전 작업을 resumed 상태로 마킹
                    await conn.execute(
                        """UPDATE pipeline_jobs
                           SET review_feedback = COALESCE(review_feedback, '') || ' | 자동 재실행됨',
                               updated_at = NOW()
                           WHERE job_id = $1""",
                        _rjob_id,
                    )

                    # 원본 instruction으로 재실행 (체인 프리픽스 없이)
                    _resume_instruction = _original_instr
                    result = await start_pipeline(
                        project=_rproject,
                        instruction=_resume_instruction,
                        chat_session_id=_rsid,
                        max_cycles=max(1, _rmax - _rcycle),
                        model=rrow.get("model_used") or "",
                    )
                    _auto_resumed += 1

                    # 채팅방에 재실행 알림
                    if _rsid:
                        try:
                            await conn.execute(
                                """INSERT INTO chat_messages
                                    (session_id, role, content, intent, cost,
                                     tokens_in, tokens_out, attachments, sources, tools_called)
                                VALUES ($1::uuid, 'assistant', $2, 'pipeline_c', 0,
                                        0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)""",
                                _rsid,
                                f"🔄 **[Pipeline Runner 자동 재실행]** `{_rjob_id}` → `{result.get('job_id', '?')}`\n"
                                f"프로젝트: **{_rproject}**\n"
                                f"서버 재시작으로 중단된 작업을 자동으로 이어서 실행합니다.",
                            )
                            await conn.execute(
                                "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                                _rsid,
                            )
                        except Exception:
                            pass

                    logger.info(f"pipeline_c_auto_resume: {_rjob_id} → {result.get('job_id', '?')} (project={_rproject})")
                except Exception as _rerr:
                    logger.warning(f"pipeline_c_auto_resume_failed: {_rjob_id} error={_rerr}")

            if _auto_resumed:
                logger.info(f"pipeline_c_recovery: auto-resumed {_auto_resumed} interrupted job(s)")

            # ── Phase 0b: 고아 directive_lifecycle 정리 (24시간 이상 in_progress → failed) ──
            stale_count = await conn.execute(
                """
                UPDATE directive_lifecycle
                SET status = 'failed',
                    error_detail = COALESCE(error_detail, '') || '서버 재시작으로 중단됨',
                    completed_at = NOW()
                WHERE status = 'in_progress'
                  AND created_at < NOW() - INTERVAL '24 hours'
                """
            )
            if stale_count and stale_count != "UPDATE 0":
                logger.info(f"pipeline_c_recovery: stale directives cleaned: {stale_count}")

            # ── Phase 1: restarting 작업 복구 ──
            rows = await conn.fetch(
                """
                SELECT job_id, chat_session_id, project, instruction, phase, status
                FROM pipeline_jobs
                WHERE status = 'running' AND phase = 'restarting'
                ORDER BY updated_at DESC LIMIT 5
                """
            )
            if not rows:
                logger.info("pipeline_c_recovery: no restarting jobs found — done")
                return

            for row in rows:
                job_id = row["job_id"]
                logger.info(f"pipeline_c_recovery: recovering interrupted job {job_id}")

                # 검증 수행
                health_cmd = "curl -sf http://localhost:8100/api/v1/health 2>/dev/null || echo FAIL"
                proc = await asyncio.create_subprocess_shell(
                    health_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                health = stdout.decode("utf-8", errors="replace").strip()

                last_commit_proc = await asyncio.create_subprocess_shell(
                    "cd /root/aads/aads-server && git log --oneline -1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                lc_stdout, _ = await asyncio.wait_for(last_commit_proc.communicate(), timeout=10)
                last_commit = lc_stdout.decode("utf-8", errors="replace").strip()

                all_ok = "ok" in health.lower() or "status" in health.lower()
                summary = f"{'복구 정상' if all_ok else '복구 확인 필요'} | commit: {last_commit} | health: {health[:100]}"

                # DB 업데이트: done 처리
                await conn.execute(
                    """
                    UPDATE pipeline_jobs
                    SET status = 'done', phase = 'done',
                        review_feedback = review_feedback || $2,
                        updated_at = now()
                    WHERE job_id = $1
                    """,
                    job_id,
                    f" | 재시작 복구: {summary}",
                )

                # 채팅방에 복구 완료 메시지
                chat_sid = row["chat_session_id"]
                if chat_sid:
                    try:
                        async with conn.transaction():
                            await conn.execute(
                                """
                                INSERT INTO chat_messages
                                    (session_id, role, content, intent, cost,
                                     tokens_in, tokens_out, attachments, sources, tools_called)
                                VALUES ($1::uuid, 'assistant', $2, 'pipeline_c', 0,
                                        0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                                """,
                                chat_sid,
                                f"✅ **[Pipeline Runner 재시작 복구]** `{job_id}`\n"
                                f"AADS 재시작 후 자동 복구 완료.\n"
                                f"Health: {health[:200]}\n"
                                f"커밋: {last_commit}\n"
                                f"**결과: {summary}**",
                            )
                            await conn.execute(
                                "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                                chat_sid,
                            )
                    except Exception as e2:
                        logger.warning(f"pipeline_c_recovery_chat_error: {e2}")

                logger.info(f"pipeline_c_recovery: job {job_id} recovered — {summary}")

    except Exception as e:
        logger.error(f"pipeline_c_recovery_error: {e}")


# ─── Detached 작업 폴링 재개 ──────────────────────────────────────────────────


async def _resume_detached_polling(job_id: str, project: str, chat_session_id: str, instruction: str):
    """서버 재시작 후 아직 실행 중인 detached 작업의 폴링을 재개."""
    _conf = PROJECT_MAP.get(project.upper(), {})
    _server = _conf.get("server", "")
    _ssh_port = _conf.get("port", "22")
    _done_file = f"/tmp/pipeline_c_{job_id}.done"
    _out_file = f"/tmp/pipeline_c_{job_id}.out"
    _poll_start = time.time()

    logger.info(f"pipeline_c_resume_polling: job={job_id} server={_server}")

    while time.time() - _poll_start < _CLAUDE_MAX_WAIT:
        await asyncio.sleep(_CLAUDE_POLL_INTERVAL)
        try:
            if _server:
                _cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p {_ssh_port} root@{_server} 'cat {_done_file} 2>/dev/null'"
            else:
                _cmd = f"cat {_done_file} 2>/dev/null"
            _proc = await asyncio.create_subprocess_shell(
                _cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _stdout, _ = await asyncio.wait_for(_proc.communicate(), timeout=15)
            _exit_str = _stdout.decode("utf-8", errors="replace").strip()
            if _exit_str == "":
                continue  # 아직 실행 중

            # 완료! 결과 읽기
            if _server:
                _rcmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p {_ssh_port} root@{_server} 'cat {_out_file} 2>/dev/null'"
            else:
                _rcmd = f"cat {_out_file} 2>/dev/null"
            _rproc = await asyncio.create_subprocess_shell(
                _rcmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _rstdout, _ = await asyncio.wait_for(_rproc.communicate(), timeout=15)
            _result = _rstdout.decode("utf-8", errors="replace").strip()[:_MAX_OUTPUT_CHARS]
            _ok = _exit_str == "0"

            from app.core.db_pool import get_pool
            pool = get_pool()
            async with pool.acquire() as conn:
                _st = "done" if _ok else "error"
                await conn.execute(
                    "UPDATE pipeline_jobs SET status=$2, phase=$2, review_feedback=COALESCE(review_feedback,'')||$3, updated_at=now() WHERE job_id=$1",
                    job_id, _st, f" | 폴링재개후 완료: exit={_exit_str}",
                )
                if chat_session_id:
                    _emoji = "✅" if _ok else "⚠️"
                    await conn.execute(
                        """INSERT INTO chat_messages (session_id,role,content,intent,cost,tokens_in,tokens_out,attachments,sources,tools_called)
                        VALUES($1::uuid,'assistant',$2,'pipeline_c',0,0,0,'[]'::jsonb,'[]'::jsonb,'[]'::jsonb)""",
                        chat_session_id,
                        f"{_emoji} **[Pipeline Runner 완료]** `{job_id}`\n프로젝트: **{project}**\n\n**결과:**\n{_result[:1500]}",
                    )
                    await conn.execute(
                        "UPDATE chat_sessions SET message_count=message_count+1, updated_at=NOW() WHERE id=$1::uuid",
                        chat_session_id,
                    )
                    try:
                        from app.services.chat_service import trigger_ai_reaction
                        await trigger_ai_reaction(
                            chat_session_id,
                            f"[시스템] Pipeline Runner 작업 `{job_id}` (프로젝트: {project})이 완료되었습니다. "
                            f"결과: {_result[:500]}\n\n위 결과를 확인하고 필요한 후속 조치가 있으면 보고해주세요."
                        )
                    except Exception:
                        pass

            logger.info(f"pipeline_c_resume_polling: job={job_id} completed (exit={_exit_str})")
            return

        except Exception as e:
            logger.warning(f"pipeline_c_resume_polling_error: job={job_id} err={e}")

    # 타임아웃
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE pipeline_jobs SET status='error', phase='error', review_feedback=COALESCE(review_feedback,'')||' | 폴링재개후 타임아웃', updated_at=now() WHERE job_id=$1",
                job_id,
            )
    except Exception:
        pass
    logger.warning(f"pipeline_c_resume_polling: job={job_id} timed out")


# ─── Watchdog: 작업 감시 + 스톨 감지 + 채팅방 알림 ───────────────────────────

_STALL_THRESHOLD_SEC = 1800  # 30분 이상 같은 phase에 머무르면 스톨로 판단 (Claude Code 긴 작업 대응)
_watchdog_task: Optional[asyncio.Task] = None
_stall_chat_count: dict[str, int] = {}  # job_id -> number of stall chat messages sent
_STALL_CHAT_MAX = 3  # max stall messages per job to prevent spam


async def start_watchdog(interval: int = 120):
    """파이프라인 워치독 시작 (2분마다 활성 작업 점검)."""
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        return  # 이미 실행 중
    _watchdog_task = asyncio.create_task(_watchdog_loop(interval))
    logger.info(f"pipeline_c_watchdog started (interval={interval}s)")


async def _watchdog_loop(interval: int):
    """활성 작업을 주기적으로 점검하여 스톨 감지 및 채팅방 알림."""
    while True:
        try:
            await asyncio.sleep(interval)
            await _check_stalled_jobs()
            await _collect_orphan_results()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"pipeline_c_watchdog_error: {e}")


async def _check_stalled_jobs():
    """스톨된 작업 감지 → 채팅방에 경고 메시지 삽입."""
    now = datetime.now()
    for job_id, job in list(_active_jobs.items()):
        if job.status in ("done", "error", "failed", "cancelled"):
            # 완료/에러 작업 즉시 정리 (스톨 알림 반복 방지)
            _active_jobs.pop(job_id, None)
            _stall_chat_count.pop(job_id, None)
            continue

        # awaiting_approval 24시간 초과 정리
        if job.status == "awaiting_approval":
            if (now - job.created_at).total_seconds() > 86400:
                _active_jobs.pop(job_id, None)
            continue

        # L2: 마지막 로그 시간 확인 (타임스탬프 파싱 강화)
        last_log_time = job.created_at
        if job.logs:
            try:
                last_ts = job.logs[-1].get("timestamp", "")
                if last_ts:
                    # isoformat 파싱 + Z suffix 대응 + 숫자만 체크
                    last_ts = last_ts.replace("Z", "+00:00")
                    last_log_time = datetime.fromisoformat(last_ts)
            except (ValueError, TypeError, AttributeError) as ts_err:
                logger.debug(f"watchdog_ts_parse_fail job={job_id}: {ts_err}, ts='{job.logs[-1].get('timestamp', '')}'")
                # 파싱 실패 시 updated_at가 있으면 사용
                pass

        elapsed = (now - last_log_time).total_seconds()
        if elapsed > _STALL_THRESHOLD_SEC:
            stall_minutes = int(elapsed // 60)
            logger.warning(
                f"pipeline_c_stall_detected job={job_id} phase={job.phase} "
                f"stalled_for={stall_minutes}min"
            )

            # 채팅방에 스톨 경고 (rate limit: max _STALL_CHAT_MAX per job)
            _stall_sent = _stall_chat_count.get(job_id, 0)
            if _stall_sent < _STALL_CHAT_MAX:
                await job._post_to_chat(
                    f"⚠️ **[Pipeline Runner 스톨 감지]** `{job.job_id}`\n"
                    f"Phase: {job.phase} | 마지막 활동: {stall_minutes}분 전\n"
                    f"프로젝트: {job.project}\n\n"
                    f"작업이 {stall_minutes}분간 진행되지 않고 있습니다.\n"
                    f"확인이 필요합니다. `pipeline_runner_status(job_id=\"{job.job_id}\")` 로 상태를 조회하세요."
                )
                _stall_chat_count[job_id] = _stall_sent + 1
            else:
                logger.debug(f"pipeline_c_stall_chat_suppressed job={job_id} (sent {_stall_sent}/{_STALL_CHAT_MAX})")

            # [Fix-F] stall 로그 기록 + 자동 kill (좀비 방지)
            if not job.logs or job.logs[-1].get("phase") != "stall_detected":
                job._log("stall_detected", f"{stall_minutes}분 스톨 감지")
                await job._save_to_db()

            # 30분 이상 stall이면 자동 강제 종료
            if elapsed > _STALL_THRESHOLD_SEC:
                try:
                    await job._kill_existing_claude_process(context="watchdog-auto-kill")
                    from app.core.db_pool import get_pool
                    _pool = get_pool()
                    async with _pool.acquire() as _conn:
                        await _conn.execute(
                            "UPDATE pipeline_jobs SET status='error', phase='error', "
                            "review_feedback=COALESCE(review_feedback,'')||$2, updated_at=now() "
                            "WHERE job_id=$1",
                            job.job_id, f" | watchdog 자동종료: {stall_minutes}분 스톨"
                        )
                    job.status = "error"
                    job.phase = "error"
                    _active_jobs.pop(job.job_id, None)
                    _stall_chat_count.pop(job.job_id, None)
                    logger.warning(f"pipeline_c_watchdog_auto_killed job={job.job_id} after {stall_minutes}min")
                except Exception as _ke:
                    logger.error(f"pipeline_c_watchdog_kill_err job={job.job_id}: {_ke}")


async def _collect_orphan_results():
    """
    2분마다 실행: DB에서 error 상태인 최근 작업 중 원격 .done 파일이 존재하는 것을 수거.
    서버 재시작으로 폴링이 죽었지만 원격 Claude Code는 완료된 경우를 처리.
    """
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            # 최근 2시간 이내 error 처리된 작업 (서버 재시작으로 중단된 것만)
            rows = await conn.fetch(
                """
                SELECT job_id, chat_session_id, project, review_feedback
                FROM pipeline_jobs
                WHERE status = 'error'
                  AND review_feedback LIKE '%서버 재시작으로 중단%'
                  AND updated_at > NOW() - INTERVAL '2 hours'
                ORDER BY updated_at DESC LIMIT 5
                """
            )
            if not rows:
                return

            for row in rows:
                _jid = row["job_id"]
                _proj = row["project"]
                _conf = PROJECT_MAP.get(_proj.upper(), {})
                _server = _conf.get("server", "")
                _ssh_port = _conf.get("port", "22")
                _done_file = f"/tmp/pipeline_c_{_jid}.done"
                _out_file = f"/tmp/pipeline_c_{_jid}.out"

                try:
                    if _server and _server != "host.docker.internal":
                        _cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p {_ssh_port} root@{_server} 'cat {_done_file} 2>/dev/null'"
                    else:
                        _cmd = f"cat {_done_file} 2>/dev/null"
                    _proc = await asyncio.create_subprocess_shell(
                        _cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    _stdout, _ = await asyncio.wait_for(_proc.communicate(), timeout=15)
                    _exit_str = _stdout.decode("utf-8", errors="replace").strip()
                    if not _exit_str:
                        continue  # .done 파일 없음 = 아직 완료 안 됨

                    # .done 존재 → 결과 수거
                    if _server and _server != "host.docker.internal":
                        _rcmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p {_ssh_port} root@{_server} 'tail -c 50000 {_out_file} 2>/dev/null'"
                    else:
                        _rcmd = f"tail -c 50000 {_out_file} 2>/dev/null"
                    _rproc = await asyncio.create_subprocess_shell(
                        _rcmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    _rstdout, _ = await asyncio.wait_for(_rproc.communicate(), timeout=15)
                    _result = _rstdout.decode("utf-8", errors="replace").strip()[:_MAX_OUTPUT_CHARS]
                    _ok = _exit_str.strip() == "0"
                    _st = "done" if _ok else "error"

                    await conn.execute(
                        "UPDATE pipeline_jobs SET status=$2, phase=$2, result_output=$3, review_feedback=COALESCE(review_feedback,'')||$4, updated_at=now() WHERE job_id=$1",
                        _jid, _st, _result[:5000],
                        f" | watchdog 결과수거: exit={_exit_str}",
                    )

                    _sid = row.get("chat_session_id")
                    if _sid:
                        _emoji = "✅" if _ok else "⚠️"
                        await conn.execute(
                            """INSERT INTO chat_messages (session_id,role,content,intent,cost,tokens_in,tokens_out,attachments,sources,tools_called)
                            VALUES($1::uuid,'assistant',$2,'pipeline_c',0,0,0,'[]'::jsonb,'[]'::jsonb,'[]'::jsonb)""",
                            _sid,
                            f"{_emoji} **[Pipeline Runner 결과 수거]** `{_jid}`\n프로젝트: **{_proj}** | exit={_exit_str}\n\n**결과:**\n{_result[:1500]}",
                        )
                        await conn.execute(
                            "UPDATE chat_sessions SET message_count=message_count+1, updated_at=NOW() WHERE id=$1::uuid",
                            _sid,
                        )
                        try:
                            from app.services.chat_service import trigger_ai_reaction
                            await trigger_ai_reaction(
                                _sid,
                                f"[시스템] Pipeline Runner 작업 `{_jid}` (프로젝트: {_proj})이 완료되었습니다. "
                                f"결과: {_result[:500]}\n\n위 결과를 확인하고 필요한 후속 조치가 있으면 보고해주세요."
                            )
                        except Exception:
                            pass

                    logger.info(f"watchdog_orphan_collected: job={_jid} exit={_exit_str} output={len(_result)}자")

                except asyncio.TimeoutError:
                    pass  # SSH 타임아웃 — 다음 주기에 재시도
                except Exception as _e:
                    logger.debug(f"watchdog_orphan_check_error: job={_jid} err={_e}")
    except Exception as e:
        if "DB pool" not in str(e):
            logger.debug(f"watchdog_orphan_collect_error: {e}")
