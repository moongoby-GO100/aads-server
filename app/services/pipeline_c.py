"""
Pipeline C Orchestrator — 채팅 → Claude Code 자율 작업 → 검수 → 재지시 → 승인 → 배포

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
_MAX_OUTPUT_CHARS = 6000    # 결과 최대 문자수
_MAX_DIFF_CHARS = 50000     # git diff 최대 문자수 (L3)
_REVIEW_MODEL = "claude-sonnet-4-6"

# M1: SSH/LLM 재시도 설정
_SSH_MAX_RETRIES = 3
_SSH_RETRY_BASE_DELAY = 2   # 초 (지수 백오프: 2, 4, 8)

# 프로젝트별 서비스 재시작 명령
_RESTART_CMD: Dict[str, str] = {
    "KIS":   "supervisorctl restart webapp",
    "GO100": "supervisorctl restart go100",
    "SF":    "cd /data/shortflow && docker compose restart worker",
    "NTV2":  "",  # PHP: 파일 수정 즉시 반영
    "AADS":  "supervisorctl restart aads-api",
}

# 활성 작업 저장 (메모리)
_active_jobs: Dict[str, "PipelineCJob"] = {}

# 프로젝트별 동시 실행 방지 락
_project_locks: Dict[str, asyncio.Lock] = {}


def get_job(job_id: str) -> Optional["PipelineCJob"]:
    return _active_jobs.get(job_id)


def list_jobs(chat_session_id: str = None) -> list:
    jobs = list(_active_jobs.values())
    if chat_session_id:
        jobs = [j for j in jobs if j.chat_session_id == chat_session_id]
    return [j.to_dict() for j in jobs]


class PipelineCJob:
    """단일 파이프라인C 작업."""

    def __init__(self, project: str, instruction: str,
                 chat_session_id: str, max_cycles: int = 3,
                 dsn: str = ""):
        self.job_id = f"pc-{int(time.time())}-{uuid.uuid4().hex[:6]}"
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
        """Phase 1~3 자율 실행 → Phase 4 승인 대기에서 멈춤. 매 단계 채팅방 기록."""
        lock = _project_locks.setdefault(self.project, asyncio.Lock())
        async with lock:
            await self._run_inner()

    async def _run_inner(self):
        """run()의 실제 본체 — 프로젝트 락 안에서 실행."""
        try:
            await self._save_to_db()

            # Phase 1: Claude Code로 작업 수행
            self._log("claude_code_work", f"Claude Code에 작업 지시 중: {self.instruction[:100]}")
            await self._post_to_chat(
                f"🔧 **[Pipeline C 시작]** `{self.job_id}`\n"
                f"프로젝트: **{self.project}**\n"
                f"지시: {self.instruction[:300]}\n\n"
                f"Claude Code에 작업을 전달합니다. 완료까지 최대 {_CLAUDE_TIMEOUT // 60}분 소요됩니다."
            )

            work_result = await self._run_claude_code(self.instruction, continue_session=False)

            if work_result.get("error"):
                self._log("error", f"Claude Code 실행 오류: {work_result['error']}")
                self.status = "error"
                self.error_msg = work_result["error"]
                await self._post_to_chat(
                    f"❌ **[Pipeline C 오류]** `{self.job_id}`\n"
                    f"Claude Code 실행 실패: {work_result['error'][:500]}"
                )
                await self._save_to_db()
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

                work_result = await self._run_claude_code(
                    f"이전 작업에 대한 검수 피드백입니다. 수정해주세요:\n{review['feedback']}",
                    continue_session=True,
                )
                if work_result.get("error"):
                    self._log("error", f"재작업 오류: {work_result['error']}")
                    self.status = "error"
                    self.error_msg = work_result["error"]
                    await self._post_to_chat(
                        f"❌ **[재작업 오류]** `{self.job_id}`\n"
                        f"Claude Code 재실행 실패: {work_result['error'][:500]}"
                    )
                    await self._save_to_db()
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

            # ★ AI 자동 반응 트리거: 승인 요청을 AI가 확인하고 CEO에게 요약 보고
            await self._trigger_ai_reaction(
                f"[시스템] Pipeline C 작업 `{self.job_id}` (프로젝트: {self.project})이 "
                f"승인 대기 상태입니다. 위 변경사항을 확인하고 CEO에게 간단히 요약해주세요. "
                f"승인/거부 판단에 필요한 핵심 정보를 알려주세요."
            )

        except Exception as e:
            logger.exception(f"pipeline_c_error job={self.job_id}")
            self._log("error", str(e))
            self.status = "error"
            self.error_msg = str(e)
            await self._post_to_chat(
                f"❌ **[Pipeline C 예외]** `{self.job_id}`\n{str(e)[:500]}"
            )
            await self._save_to_db()

            # ★ AI 자동 반응 트리거: 에러 발생 시 AI가 원인 분석 및 대안 제시
            await self._trigger_ai_reaction(
                f"[시스템] Pipeline C 작업 `{self.job_id}` (프로젝트: {self.project})에서 "
                f"오류가 발생했습니다: {str(e)[:300]}. "
                f"오류 원인을 분석하고 해결 방안을 제시해주세요."
            )

    async def approve(self) -> dict:
        """CEO 승인 → Phase 5~7 배포 + 검증."""
        if self.status != "awaiting_approval":
            return {"error": f"승인 불가 상태: {self.status}"}

        self.status = "running"
        try:
            # Phase 5: 커밋 + 푸시
            self._log("deploying", "git add + commit + push 진행 중...")
            await self._post_to_chat(
                f"🚀 **[배포 시작]** `{self.job_id}`\n"
                f"CEO 승인 완료. git commit + push + 서비스 재시작 진행 중..."
            )

            await self._ssh_command("git add -u")
            commit_msg = f"Pipeline-C: {self.instruction[:80]} (job: {self.job_id})"
            safe_msg = shlex.quote(commit_msg)
            await self._ssh_command(f'git commit -m {safe_msg}')
            push_result = await self._ssh_command("git push")
            self._log("push_done", f"push 완료: {push_result[:200]}")

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
                # 재시작 실행 (이후 이 프로세스도 재시작됨)
                await self._ssh_command(restart_cmd)
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
                    f"✅ **[Pipeline C 완료 — AADS 자기수정]** `{self.job_id}`\n"
                    f"커밋: {verify.get('last_commit', 'N/A')}\n"
                    f"Health: {verify.get('health', 'N/A')[:200]}\n"
                    f"에러: {verify.get('errors', '없음')[:200] or '없음'}\n\n"
                    f"**결과: {verify['summary']}**"
                )
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
                f"✅ **[Pipeline C 완료]** `{self.job_id}`\n"
                f"프로젝트: **{self.project}**\n"
                f"커밋: {verify.get('last_commit', 'N/A')}\n"
                f"Health: {verify.get('health', 'N/A')[:200]}\n"
                f"에러: {verify.get('errors', '없음')[:200] or '없음'}\n\n"
                f"**결과: {verify['summary']}**"
            )

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
            await self._post_to_chat(
                f"❌ **[배포 오류]** `{self.job_id}`\n{str(e)[:500]}"
            )
            await self._save_to_db()
            return {"error": str(e)}

    async def reject(self, reason: str = "") -> dict:
        """CEO 거부 → 변경사항 되돌리기."""
        if self.status != "awaiting_approval":
            return {"error": f"거부 불가 상태: {self.status}"}

        self._log("rejected", f"CEO 거부: {reason}")
        # git stash로 변경사항 원복 (관련 없는 작업 보존)
        await self._ssh_command(f"git stash push -m 'pipeline_c_reject_{self.job_id}'")
        self.status = "done"
        self.review_feedback = f"REJECTED: {reason}"
        await self._save_to_db()

        # 채팅방에 거부+원복 기록
        await self._post_to_chat(
            f"🚫 **[Pipeline C 거부]** `{self.job_id}`\n"
            f"사유: {reason or '(미지정)'}\n"
            f"변경사항이 원복되었습니다."
        )

        return {"status": "rejected", "message": "변경사항이 원복되었습니다."}

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
        escaped = shlex.quote(instruction)

        if continue_session:
            claude_cmd = f"claude -p --output-format text -c {escaped}"
        else:
            claude_cmd = (
                f"claude -p --output-format text "
                f"--session-id {self.claude_session_id} "
                f"{escaped}"
            )

        # API 키 주입 (OAuth 만료 대비)
        api_key_setup = (
            "source ~/.claude/api_keys.env 2>/dev/null; "
            "export ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-$API_KEY_1}; "
        )

        # 고유 출력 파일 경로
        out_file = f"/tmp/pipeline_c_{self.job_id}.out"
        err_file = f"/tmp/pipeline_c_{self.job_id}.err"
        pid_file = f"/tmp/pipeline_c_{self.job_id}.pid"
        done_file = f"/tmp/pipeline_c_{self.job_id}.done"

        # base64로 명령어 인코딩하여 쉘 이스케이프 문제 회피
        encoded_cmd = base64.b64encode(f'{claude_cmd} > {out_file} 2> {err_file}; echo $? > {done_file}'.encode()).decode()
        detach_cmd = (
            f"{api_key_setup}"
            f"cd {shlex.quote(self.workdir)} && "
            f"nohup bash -c \"$(echo {encoded_cmd} | base64 -d)\" "
            f"> /dev/null 2>&1 & echo $!"
        )

        try:
            # Step 1: 분리 실행 시작
            pid_output = await self._ssh_command(detach_cmd, timeout=15)
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
                f"최대 2시간까지 대기하며, 30초마다 상태를 확인합니다."
            )

            # Step 2: 폴링으로 완료 대기
            elapsed = 0
            last_report = 0
            while elapsed < _CLAUDE_MAX_WAIT:
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
            return {"error": f"Claude Code 최대 대기시간 초과 ({_CLAUDE_MAX_WAIT // 60}분)", "output": ""}

        except Exception as e:
            return {"error": str(e), "output": ""}

    async def _run_claude_code_direct(self, instruction: str, continue_session: bool) -> dict:
        """직접 실행 폴백 (분리 실행 불가 시)."""
        escaped = shlex.quote(instruction)

        if continue_session:
            claude_cmd = f"claude -p --output-format text -c {escaped}"
        else:
            claude_cmd = (
                f"claude -p --output-format text "
                f"--session-id {self.claude_session_id} "
                f"{escaped}"
            )

        api_key_setup = (
            "source ~/.claude/api_keys.env 2>/dev/null; "
            "export ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-$API_KEY_1}; "
        )
        full_cmd = f"{api_key_setup}cd {shlex.quote(self.workdir)} && {claude_cmd}"

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

    # ─── AI 검수 ────────────────────────────────────────────────────────────

    async def _ai_review(self) -> dict:
        """AADS AI가 Claude Code 작업 결과를 자동 검수."""
        from app.api.ceo_chat import call_llm

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
            response_text, _, _ = await call_llm(
                _REVIEW_MODEL,
                "코드 리뷰어. JSON으로만 응답.",
                [{"role": "user", "content": review_prompt}],
            )
            # JSON 추출
            text = response_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(text)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"ai_review_parse_failed: {e}, raw={response_text[:300] if response_text else 'N/A'}")
            return {
                "verdict": "FAIL",
                "summary": "AI 검수 응답 파싱 실패",
                "feedback": "AI 검수 응답을 파싱할 수 없습니다. 수동 확인이 필요합니다.",
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
                         review_feedback, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now())
                    ON CONFLICT (job_id) DO UPDATE SET
                        phase = EXCLUDED.phase,
                        cycle = EXCLUDED.cycle,
                        status = EXCLUDED.status,
                        logs = EXCLUDED.logs,
                        result_output = EXCLUDED.result_output,
                        git_diff = EXCLUDED.git_diff,
                        review_feedback = EXCLUDED.review_feedback,
                        updated_at = now()
                """,
                    self.job_id, self.chat_session_id, self.project,
                    self.instruction, self.claude_session_id,
                    self.phase, self.cycle, self.max_cycles, self.status,
                    json.dumps(self.logs, ensure_ascii=False),
                    (self.result_output or "")[:10000],
                    (self.git_diff or "")[:10000],
                    self.review_feedback or "",
                )
        except Exception as e:
            logger.error(f"pipeline_c_save_db_error job={self.job_id}: {e}")


# ─── 프로젝트 → 세션 자동 매핑 ─────────────────────────────────────────────────

async def _find_recent_session(project: str) -> str:
    """프로젝트의 워크스페이스에서 가장 최근 활성 세션 ID를 찾는다.
    워크스페이스 이름이 [KIS], [NTV2] 형태이므로 `[PROJECT]` 패턴으로 검색."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        # 워크스페이스 이름 패턴: "[KIS] 자동매매", "[NTV2] NewTalk V2" 등
        ws_pattern = f"\\[{project.upper()}\\]%"  # escape for LIKE
        async with pool.acquire() as conn:
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
    except Exception as e:
        logger.warning(f"_find_recent_session error for {project}: {e}")
        return ""


# ─── 외부 API 함수 ────────────────────────────────────────────────────────────

async def start_pipeline(
    project: str,
    instruction: str,
    chat_session_id: str,
    max_cycles: int = 3,
    dsn: str = "",
) -> dict:
    """파이프라인C 시작 (asyncio.create_task로 백그라운드 실행)."""
    logger.info(f"[DIAG] start_pipeline: chat_session_id='{chat_session_id}' project={project}")

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
        "message": f"파이프라인C 시작됨. 작업 완료 후 채팅방에 보고됩니다. job_id: {job.job_id}",
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
    """활성 파이프라인 목록."""
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


# ─── AADS 자기수정 복구: 재시작으로 중단된 작업 검출 + 완료 처리 ──────────────

async def recover_interrupted_jobs():
    """
    서버 시작 시 호출:
    1. restarting phase에서 중단된 AADS 파이프라인을 복구
    2. running/queued 상태로 남아있는 고아 pipeline_jobs를 error 처리
    3. in_progress 상태로 24시간 이상 남아있는 고아 directive_lifecycle을 failed 처리
    """
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            # ── Phase 0: 고아 pipeline_jobs 정리 (running/queued → error) + 채팅방 알림 ──
            # 먼저 알림 대상 조회
            orphan_rows = await conn.fetch(
                """
                SELECT job_id, chat_session_id, project, substring(instruction from 1 for 100) as instr
                FROM pipeline_jobs
                WHERE status = 'running' AND phase NOT IN ('restarting', 'done', 'error')
                """
            )
            orphan_count = await conn.execute(
                """
                UPDATE pipeline_jobs
                SET status = 'error', phase = 'error',
                    review_feedback = COALESCE(review_feedback, '') || ' | 서버 재시작으로 중단됨',
                    updated_at = now()
                WHERE status = 'running' AND phase NOT IN ('restarting', 'done', 'error')
                """
            )
            if orphan_count and orphan_count != "UPDATE 0":
                logger.info(f"pipeline_c_recovery: orphan pipeline_jobs cleaned: {orphan_count}")
                # 채팅방에 중단 알림 전송
                for orow in orphan_rows:
                    sid = orow.get("chat_session_id")
                    if not sid:
                        continue
                    try:
                        await conn.execute(
                            """
                            INSERT INTO chat_messages
                                (session_id, role, content, intent, cost,
                                 tokens_in, tokens_out, attachments, sources, tools_called)
                            VALUES ($1::uuid, 'assistant', $2, 'pipeline_c', 0,
                                    0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                            """,
                            sid,
                            f"⚠️ **[Pipeline C 중단]** `{orow['job_id']}`\n"
                            f"프로젝트: **{orow.get('project', '?')}**\n"
                            f"사유: 서버 재시작으로 중단됨\n"
                            f"작업: {orow.get('instr', '')[:200]}\n\n"
                            f"재실행이 필요하면 다시 지시해주세요.",
                        )
                    except Exception as post_err:
                        logger.warning(f"pipeline_c_recovery: chat post failed for {orow['job_id']}: {post_err}")

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
                                f"✅ **[Pipeline C 재시작 복구]** `{job_id}`\n"
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


# ─── Watchdog: 작업 감시 + 스톨 감지 + 채팅방 알림 ───────────────────────────

_STALL_THRESHOLD_SEC = 600  # 10분 이상 같은 phase에 머무르면 스톨로 판단
_watchdog_task: Optional[asyncio.Task] = None


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
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"pipeline_c_watchdog_error: {e}")


async def _check_stalled_jobs():
    """스톨된 작업 감지 → 채팅방에 경고 메시지 삽입."""
    now = datetime.now()
    for job_id, job in list(_active_jobs.items()):
        if job.status in ("done", "error"):
            # L4: 오래된 완료 작업 정리 (10분 이상)
            if (now - job.created_at).total_seconds() > 600:
                _active_jobs.pop(job_id, None)
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

            # 채팅방에 스톨 경고
            await job._post_to_chat(
                f"⚠️ **[Pipeline C 스톨 감지]** `{job.job_id}`\n"
                f"Phase: {job.phase} | 마지막 활동: {stall_minutes}분 전\n"
                f"프로젝트: {job.project}\n\n"
                f"작업이 {stall_minutes}분간 진행되지 않고 있습니다.\n"
                f"확인이 필요합니다. `pipeline_c_status(job_id=\"{job.job_id}\")` 로 상태를 조회하세요."
            )

            # 스톨 로그 기록 (중복 방지: 이미 stall 로그가 마지막이면 skip)
            if not job.logs or job.logs[-1].get("phase") != "stall_detected":
                job._log("stall_detected", f"{stall_minutes}분 스톨 감지")
                await job._save_to_db()
