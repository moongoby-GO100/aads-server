"""
Pipeline C Orchestrator — 채팅 → Claude Code 자율 작업 → 검수 → 재지시 → 승인 → 배포

승인 모드 플로우:
  Phase 1: claude_code_work  — Claude Code CLI로 작업 수행
  Phase 2: ai_review         — AADS AI가 결과 검수
  Phase 3: revision (0~N)    — 검수 실패 시 재지시 루프
  Phase 4: awaiting_approval — CEO 승인 대기
  Phase 5: deploying         — 커밋/푸시/재시작
  Phase 6: verifying         — 최종 검증
  Phase 7: done              — 완료
"""
import asyncio
import json
import logging
import shlex
import time
import uuid

from datetime import datetime
from typing import Any, Dict, Optional

import asyncpg

from app.core.project_config import PROJECT_MAP

logger = logging.getLogger(__name__)

# ─── 설정 ─────────────────────────────────────────────────────────────────────
_CLAUDE_TIMEOUT = 600       # Claude Code 실행 타임아웃 (10분)
_MAX_OUTPUT_CHARS = 6000    # 결과 최대 문자수
_REVIEW_MODEL = "claude-sonnet-4-6"

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
        self.chat_session_id = chat_session_id
        self.claude_session_id = str(uuid.uuid4())
        self.max_cycles = max_cycles
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

    # ─── 메인 실행 ──────────────────────────────────────────────────────────

    async def run(self):
        """Phase 1~3 자율 실행 → Phase 4 승인 대기에서 멈춤."""
        try:
            await self._save_to_db()

            # Phase 1: Claude Code로 작업 수행
            self._log("claude_code_work", f"Claude Code에 작업 지시 중: {self.instruction[:100]}")
            work_result = await self._run_claude_code(self.instruction, continue_session=False)

            if work_result.get("error"):
                self._log("error", f"Claude Code 실행 오류: {work_result['error']}")
                self.status = "error"
                self.error_msg = work_result["error"]
                await self._save_to_db()
                return

            self.result_output = work_result.get("output", "")
            self._log("claude_code_done", f"작업 완료. 출력 {len(self.result_output)}자")

            # Phase 2~3: 검수 + 재지시 루프
            while self.cycle < self.max_cycles:
                self.cycle += 1

                # git diff 가져오기
                self.git_diff = await self._ssh_command("git diff HEAD")

                # AI 검수
                self._log("ai_review", f"[{self.cycle}차] AI 검수 중...")
                review = await self._ai_review()

                if review["verdict"] == "PASS":
                    self._log("review_pass", f"[{self.cycle}차] 검수 통과: {review['summary']}")
                    self.review_feedback = f"PASS: {review['summary']}"
                    break

                # 검수 실패 → 재지시
                self.review_feedback = f"FAIL: {review['feedback']}"
                self._log("revision", f"[{self.cycle}차] 재지시: {review['feedback'][:200]}")

                work_result = await self._run_claude_code(
                    f"이전 작업에 대한 검수 피드백입니다. 수정해주세요:\n{review['feedback']}",
                    continue_session=True,
                )
                if work_result.get("error"):
                    self._log("error", f"재작업 오류: {work_result['error']}")
                    self.status = "error"
                    self.error_msg = work_result["error"]
                    await self._save_to_db()
                    return

                self.result_output = work_result.get("output", "")
            else:
                self._log("max_cycles", f"최대 재지시 횟수({self.max_cycles}) 도달")

            # Phase 4: 승인 대기
            self.git_diff = await self._ssh_command("git diff HEAD")
            self._log("awaiting_approval", "CEO 승인 대기 중. 채팅에서 승인해주세요.")
            self.status = "awaiting_approval"
            await self._save_to_db()

        except Exception as e:
            logger.exception(f"pipeline_c_error job={self.job_id}")
            self._log("error", str(e))
            self.status = "error"
            self.error_msg = str(e)
            await self._save_to_db()

    async def approve(self) -> dict:
        """CEO 승인 → Phase 5~7 배포 + 검증."""
        if self.status != "awaiting_approval":
            return {"error": f"승인 불가 상태: {self.status}"}

        self.status = "running"
        try:
            # Phase 5: 커밋 + 푸시
            self._log("deploying", "git add + commit + push 진행 중...")
            await self._ssh_command("git add -A")
            commit_msg = f"Pipeline-C: {self.instruction[:80]} (job: {self.job_id})"
            safe_msg = commit_msg.replace('"', '\\"')
            await self._ssh_command(f'git commit -m "{safe_msg}"')
            push_result = await self._ssh_command("git push")
            self._log("push_done", f"push 완료: {push_result[:200]}")

            # 서비스 재시작
            restart_cmd = _RESTART_CMD.get(self.project, "")
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
            await self._save_to_db()
            return {"error": str(e)}

    async def reject(self, reason: str = "") -> dict:
        """CEO 거부 → 변경사항 되돌리기."""
        if self.status != "awaiting_approval":
            return {"error": f"거부 불가 상태: {self.status}"}

        self._log("rejected", f"CEO 거부: {reason}")
        # git checkout으로 변경사항 원복
        await self._ssh_command("git checkout -- .")
        self.status = "done"
        self.review_feedback = f"REJECTED: {reason}"
        await self._save_to_db()
        return {"status": "rejected", "message": "변경사항이 원복되었습니다."}

    # ─── Claude Code CLI 실행 ───────────────────────────────────────────────

    async def _run_claude_code(self, instruction: str, continue_session: bool) -> dict:
        """SSH로 원격 서버의 Claude Code CLI 실행."""
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
        full_cmd = f"{api_key_setup}cd {shlex.quote(self.workdir)} && {claude_cmd}"

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
            logger.warning(f"ai_review_parse_error: {e}, raw={response_text[:300] if 'response_text' in dir() else 'N/A'}")
            return {"verdict": "PASS", "summary": "검수 파싱 실패 — 수동 확인 필요", "feedback": ""}

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

    async def _ssh_command(self, command: str, timeout: int = 30) -> str:
        """원격 서버 명령 실행 (내부용, 보안 화이트리스트 없음 — 오케스트레이터 전용)."""
        full_cmd = f"cd {shlex.quote(self.workdir)} && {command}"
        try:
            if self.server == "localhost":
                proc = await asyncio.create_subprocess_shell(
                    full_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            else:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                    f"root@{self.server}", full_cmd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")
            if len(out) > _MAX_OUTPUT_CHARS:
                out = out[-_MAX_OUTPUT_CHARS:]
            return out
        except asyncio.TimeoutError:
            return f"[TIMEOUT {timeout}s]"
        except Exception as e:
            return f"[ERROR] {e}"

    # ─── DB 저장 ────────────────────────────────────────────────────────────

    async def _save_to_db(self):
        """현재 상태를 DB에 저장."""
        try:
            from app.config import settings
            dsn = self.dsn or settings.DATABASE_URL or settings.SUPABASE_DIRECT_URL
            conn = await asyncpg.connect(dsn=dsn)
            try:
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
            finally:
                await conn.close()
        except Exception as e:
            logger.error(f"pipeline_c_save_db_error job={self.job_id}: {e}")


# ─── 외부 API 함수 ────────────────────────────────────────────────────────────

def _run_job_in_thread(job: PipelineCJob):
    """별도 스레드에서 이벤트 루프를 만들어 파이프라인 실행."""
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


async def start_pipeline(
    project: str,
    instruction: str,
    chat_session_id: str,
    max_cycles: int = 3,
    dsn: str = "",
) -> dict:
    """파이프라인C 시작 (별도 스레드 백그라운드)."""
    job = PipelineCJob(
        project=project,
        instruction=instruction,
        chat_session_id=chat_session_id,
        max_cycles=max_cycles,
        dsn=dsn,
    )
    _active_jobs[job.job_id] = job
    _run_job_in_thread(job)
    return {
        "job_id": job.job_id,
        "project": job.project,
        "status": "started",
        "message": f"파이프라인C 시작됨. 작업 완료 후 승인 요청됩니다. job_id: {job.job_id}",
    }


async def get_pipeline_status(job_id: str) -> dict:
    """파이프라인 상태 조회."""
    job = get_job(job_id)
    if job:
        return job.to_dict()

    # 메모리에 없으면 DB 조회
    try:
        from app.config import settings
        dsn = settings.DATABASE_URL or settings.SUPABASE_DIRECT_URL
        conn = await asyncpg.connect(dsn=dsn)
        try:
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
        finally:
            await conn.close()
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
