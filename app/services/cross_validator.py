"""
AADS-113/118: 교차검증 엔진 + 자동복구 — CrossValidator / AutoRecovery
9종 검증: 정체감지/브릿지정합/커밋정합/비용추적/환경트렌드/매니저응답/파이프라인흐름
         +체크8(seen_tasks 차단감지)/체크9(미감지 지시서 복원)
2분마다 watchdog_daemon.py에서 호출
"""
import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

logger = structlog.get_logger()

KST = timezone(timedelta(hours=9))
SEEN_TASKS_FILE = "/root/.genspark/directive_seen_tasks.json"
AUTO_TRIGGER_SCRIPT = "/root/aads/scripts/auto_trigger.sh"
RUNNING_DIR = "/root/.genspark/directives/running"
PENDING_DIR = "/root/.genspark/directives/pending"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads2026secure@aads-postgres:5432/aads"
)


class AutoRecovery:
    """자동 복구 액션 모음."""

    async def _restart_auto_trigger(self):
        """auto_trigger.sh 재실행."""
        try:
            result = subprocess.run(
                ["bash", AUTO_TRIGGER_SCRIPT],
                timeout=30, capture_output=True, text=True
            )
            logger.info("auto_trigger_restarted",
                        rc=result.returncode, stdout=result.stdout[:200])
        except Exception as e:
            logger.error("auto_trigger_restart_failed", error=str(e))

    async def _check_and_restart_auto_trigger(self):
        """auto_trigger 프로세스 확인 + 재시작."""
        try:
            check = subprocess.run(
                ["pgrep", "-f", "auto_trigger.sh"],
                capture_output=True, text=True
            )
            if check.returncode != 0:
                logger.warning("auto_trigger_not_running_restarting")
                await self._restart_auto_trigger()
        except Exception as e:
            logger.error("auto_trigger_check_failed", error=str(e))

    async def _requeue_directive(self, task_id: str):
        """running → pending 재투입."""
        import glob
        try:
            pattern = os.path.join(RUNNING_DIR, "*.md")
            for fpath in glob.glob(pattern):
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                if task_id in content:
                    dest = os.path.join(PENDING_DIR, os.path.basename(fpath))
                    os.makedirs(PENDING_DIR, exist_ok=True)
                    os.rename(fpath, dest)
                    logger.info("directive_requeued", task_id=task_id, dest=dest)
                    return
        except Exception as e:
            logger.error("requeue_failed", task_id=task_id, error=str(e))

    async def _remove_from_seen_tasks(self, task_id: str):
        """seen_tasks.json에서 해당 task_id 제거."""
        try:
            if not os.path.exists(SEEN_TASKS_FILE):
                return
            with open(SEEN_TASKS_FILE, "r", encoding="utf-8") as f:
                seen = json.load(f)
            changed = False
            for project_key in list(seen.keys()):
                if task_id in seen[project_key]:
                    seen[project_key].pop(task_id, None)
                    changed = True
            if changed:
                with open(SEEN_TASKS_FILE, "w", encoding="utf-8") as f:
                    json.dump(seen, f, indent=2, ensure_ascii=False)
                logger.info("seen_tasks_cleaned", task_id=task_id)
        except Exception as e:
            logger.error("remove_seen_tasks_failed", task_id=task_id, error=str(e))

    async def _cleanup_disk(self):
        """디스크 정리: docker prune + 오래된 로그 삭제."""
        try:
            subprocess.run(
                ["docker", "system", "prune", "-f"],
                timeout=60, capture_output=True
            )
            subprocess.run(
                "find /var/log/aads -name '*.log' -mtime +7 -delete",
                shell=True, timeout=30, capture_output=True
            )
            logger.info("disk_cleanup_done")
        except Exception as e:
            logger.error("disk_cleanup_failed", error=str(e))

    async def _notify_ceo(self, issue: dict, pool=None):
        """텔레그램 알림 + ceo_decision_log INSERT."""
        msg = f"[AADS 교차검증 경고]\n타입: {issue.get('type')}\n심각도: {issue.get('severity')}\n{json.dumps(issue, ensure_ascii=False, default=str)}"
        # 텔레그램 알림 (watchdog_daemon 방식 재사용)
        try:
            import urllib.request as _urllib
            env_file = "/root/aads/aads-server/.env"
            token, chat_id = "", ""
            if os.path.exists(env_file):
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TELEGRAM_BOT_TOKEN="):
                            token = line.split("=", 1)[1].strip('"').strip("'")
                        elif line.startswith("TELEGRAM_CHAT_ID="):
                            chat_id = line.split("=", 1)[1].strip('"').strip("'")
            if token and chat_id:
                payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
                req = _urllib.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload, headers={"Content-Type": "application/json"}
                )
                _urllib.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("telegram_notify_failed", error=str(e))

        # ceo_decision_log INSERT
        if pool:
            try:
                async with pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO ceo_decision_log (task_id, decision, reason)
                        VALUES ($1, $2, $3)
                    """, issue.get("task_id", ""), "alert",
                        json.dumps(issue, ensure_ascii=False, default=str))
            except Exception as e:
                logger.warning("ceo_decision_log_insert_failed", error=str(e))


class CrossValidator(AutoRecovery):
    """9종 교차검증 엔진 (AADS-113/118)."""

    def __init__(self, pool):
        self.pool = pool
        self.blocked_tasks_count: int = 0
        self.undetected_tasks_count: int = 0
        self.last_seen_tasks_check: Optional[str] = None

    async def run_all_checks(self) -> list:
        """모든 검증 실행 → critical은 자동복구, warning은 CEO 알림."""
        results = []
        checks = [
            self.check_stalled_directives,
            self.check_bridge_directive_consistency,
            self.check_commit_completeness,
            self.check_cost_tracking,
            self.check_env_trend,
            self.check_agent_responsiveness,
            self.check_pipeline_flow,
            self.check_seen_tasks_blocked,
            self.check_undetected_directives,
        ]
        for check in checks:
            try:
                issues = await check()
                results.extend(issues)
            except Exception as e:
                logger.error("cross_validator_check_error", check=check.__name__, error=str(e))
                results.append({
                    "type": "validator_error",
                    "check": check.__name__,
                    "error": str(e),
                    "severity": "warning",
                })

        for issue in results:
            if issue.get("severity") == "critical":
                await self.auto_recover(issue)
            elif issue.get("severity") == "warning":
                await self._notify_ceo(issue, self.pool)

        # 결과를 system_metrics에 기록
        await self._record_metrics(results)
        return results

    async def auto_recover(self, issue: dict):
        """critical 이슈 자동복구."""
        issue_type = issue.get("type", "")
        logger.info("auto_recovering", issue_type=issue_type, task_id=issue.get("task_id"))
        if issue_type == "queue_stalled":
            await self._restart_auto_trigger()
            issue["auto_recovered"] = True
        elif issue_type == "execution_stalled":
            task_id = issue.get("task_id")
            if task_id:
                await self._requeue_directive(task_id)
                if self.pool:
                    try:
                        async with self.pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE directive_lifecycle SET status='requeued' WHERE task_id=$1",
                                task_id
                            )
                    except Exception as e:
                        logger.warning("requeue_db_update_failed", error=str(e))
            issue["auto_recovered"] = True
        elif issue_type == "bridge_orphan":
            task_id = issue.get("task_id")
            if task_id:
                await self._remove_from_seen_tasks(task_id)
            issue["auto_recovered"] = True
        elif issue_type == "pipeline_blocked":
            await self._check_and_restart_auto_trigger()
            issue["auto_recovered"] = True
        elif issue_type == "disk_trend_warning":
            await self._cleanup_disk()
            issue["auto_recovered"] = True

    # ─── 검증 1: 지시서 정체 감지 ────────────────────────────────────────────

    async def check_stalled_directives(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            stalled_queue = await conn.fetch("""
                SELECT task_id, project, queued_at FROM directive_lifecycle
                WHERE status='queued' AND queued_at < NOW() - INTERVAL '10 min'
            """)
            for d in stalled_queue:
                issues.append({
                    "type": "queue_stalled",
                    "task_id": d["task_id"],
                    "project": d["project"],
                    "queued_at": str(d["queued_at"]),
                    "severity": "critical",
                    "auto_recovered": False,
                })

            stalled_running = await conn.fetch("""
                SELECT task_id, project, started_at FROM directive_lifecycle
                WHERE status='running' AND started_at < NOW() - INTERVAL '60 min'
            """)
            for d in stalled_running:
                issues.append({
                    "type": "execution_stalled",
                    "task_id": d["task_id"],
                    "project": d["project"],
                    "started_at": str(d["started_at"]),
                    "severity": "critical",
                    "auto_recovered": False,
                })
        return issues

    # ─── 검증 2: 브릿지 ↔ 지시서 정합성 ─────────────────────────────────────

    async def check_bridge_directive_consistency(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            orphans = await conn.fetch("""
                SELECT bal.directive_task_id, bal.detected_at
                FROM bridge_activity_log bal
                WHERE bal.classification='directive'
                  AND bal.directive_task_id IS NOT NULL
                  AND bal.directive_task_id NOT IN (
                      SELECT task_id FROM directive_lifecycle
                  )
                  AND bal.detected_at > NOW() - INTERVAL '1 hour'
            """)
            for b in orphans:
                issues.append({
                    "type": "bridge_orphan",
                    "task_id": b["directive_task_id"],
                    "detected_at": str(b["detected_at"]),
                    "severity": "critical",
                    "auto_recovered": False,
                })
        return issues

    # ─── 검증 3: 커밋 ↔ 태스크 정합성 ───────────────────────────────────────

    async def check_commit_completeness(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            no_commit = await conn.fetch("""
                SELECT dl.task_id, dl.project, dl.completed_at
                FROM directive_lifecycle dl
                WHERE dl.status='completed'
                  AND dl.task_id NOT IN (SELECT task_id FROM commit_log)
                  AND dl.completed_at > NOW() - INTERVAL '2 hours'
            """)
            for d in no_commit:
                issues.append({
                    "type": "no_commit",
                    "task_id": d["task_id"],
                    "project": d["project"],
                    "completed_at": str(d["completed_at"]),
                    "severity": "warning",
                })
        return issues

    # ─── 검증 4: 비용 미기록 감지 ────────────────────────────────────────────

    async def check_cost_tracking(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            no_cost = await conn.fetch("""
                SELECT dl.task_id, dl.project, dl.completed_at
                FROM directive_lifecycle dl
                LEFT JOIN cost_tracking ct ON dl.task_id = ct.task_id
                WHERE dl.status='completed'
                  AND ct.id IS NULL
                  AND dl.completed_at > NOW() - INTERVAL '2 hours'
            """)
            for d in no_cost:
                issues.append({
                    "type": "no_cost_record",
                    "task_id": d["task_id"],
                    "project": d["project"],
                    "completed_at": str(d["completed_at"]),
                    "severity": "warning",
                })
        return issues

    # ─── 검증 5: 서버 환경 트렌드 선제 경고 ──────────────────────────────────

    async def check_env_trend(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            trend = await conn.fetch("""
                SELECT disk_percent FROM server_env_history
                WHERE server='68' ORDER BY snapshot_at DESC LIMIT 6
            """)
        if len(trend) >= 3:
            values = [float(r["disk_percent"] or 0) for r in trend]
            # 최신 순 정렬 → 오래된 것부터 증가 추세인지 확인
            increasing = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
            latest = values[0]
            if increasing and latest > 75:
                issues.append({
                    "type": "disk_trend_warning",
                    "server": "68",
                    "latest_disk_percent": latest,
                    "trend": values,
                    "severity": "warning",
                })
        return issues

    # ─── 검증 6: 매니저 무응답 감지 ──────────────────────────────────────────

    async def check_agent_responsiveness(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            unresponsive = await conn.fetch("""
                SELECT dl.task_id, dl.project, dl.queued_at, dl.executor
                FROM directive_lifecycle dl
                LEFT JOIN agent_activity_log al ON dl.task_id = al.task_id
                WHERE dl.status='queued'
                  AND dl.queued_at < NOW() - INTERVAL '15 min'
                  AND al.id IS NULL
            """)
            for d in unresponsive:
                issues.append({
                    "type": "agent_unresponsive",
                    "task_id": d["task_id"],
                    "project": d["project"],
                    "queued_at": str(d["queued_at"]),
                    "executor": d["executor"],
                    "severity": "warning",
                })
        return issues

    # ─── 검증 7: 전체 파이프라인 흐름 감시 ───────────────────────────────────

    async def check_pipeline_flow(self) -> list:
        issues = []
        async with self.pool.acquire() as conn:
            recent_completed = await conn.fetchval("""
                SELECT COUNT(*) FROM directive_lifecycle
                WHERE status='completed' AND completed_at > NOW() - INTERVAL '30 min'
            """)
            active = await conn.fetchval("""
                SELECT COUNT(*) FROM directive_lifecycle
                WHERE status IN ('queued','running')
            """)
        if int(recent_completed or 0) == 0 and int(active or 0) > 0:
            issues.append({
                "type": "pipeline_blocked",
                "active_count": int(active),
                "recent_completed_30m": int(recent_completed or 0),
                "severity": "critical",
                "auto_recovered": False,
            })
        return issues

    # ─── 검증 8: seen_tasks 차단 감지 및 자동 해제 ───────────────────────────

    async def check_seen_tasks_blocked(self) -> list:
        """체크 8: seen_tasks에 error/미등록 상태로 차단된 작업 감지 및 해제."""
        issues = []
        if not os.path.exists(SEEN_TASKS_FILE):
            self.last_seen_tasks_check = datetime.now(tz=KST).isoformat()
            return issues

        try:
            with open(SEEN_TASKS_FILE, "r", encoding="utf-8") as f:
                seen = json.load(f)
        except Exception as e:
            logger.error("seen_tasks_load_failed", error=str(e))
            return issues

        failed_statuses = {"error", "auth_expired", "permission_denied", "task_failure"}
        to_release = []

        async with self.pool.acquire() as conn:
            for task_id in list(seen.keys()):
                row = await conn.fetchrow(
                    "SELECT task_id, project, status, content, title FROM directive_lifecycle "
                    "WHERE task_id=$1 ORDER BY id DESC LIMIT 1",
                    task_id
                )
                db_status = row["status"] if row else None
                if db_status is None or db_status in failed_statuses:
                    to_release.append((task_id, db_status, row))

        for task_id, db_status, row in to_release:
            seen.pop(task_id, None)
            restored = await self._restore_directive_to_pending(task_id, db_status, row)
            issue = {
                "type": "BLOCKED_TASK_RELEASED",
                "task_id": task_id,
                "db_status": db_status or "not_registered",
                "restored_to_pending": restored,
                "severity": "warning",
            }
            issues.append(issue)
            logger.info("blocked_task_released", task_id=task_id, db_status=db_status)
            await self._notify_ceo(issue, self.pool)

        if to_release:
            try:
                with open(SEEN_TASKS_FILE, "w", encoding="utf-8") as f:
                    json.dump(seen, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error("seen_tasks_save_failed", error=str(e))

        # 서버 211 SSH 체크
        await self._check_seen_tasks_server211(issues)

        self.blocked_tasks_count = len([i for i in issues if i.get("type") == "BLOCKED_TASK_RELEASED"])
        self.last_seen_tasks_check = datetime.now(tz=KST).isoformat()
        return issues

    async def _restore_directive_to_pending(
        self, task_id: str, db_status: Optional[str], row
    ) -> bool:
        """directive 내용을 pending 폴더에 .md 파일로 복원."""
        try:
            os.makedirs(PENDING_DIR, exist_ok=True)
            content = None
            title = "복원된 지시서"

            if row:
                content = row.get("content")
                title = row.get("title") or title

            if not content:
                content = await self._restore_from_bridge_log(task_id)

            if not content:
                content = (
                    f"%%%\nTask ID: {task_id}\n"
                    f"제목: {title}\n"
                    f"상태: {db_status or 'unknown'} — 수동 재지시 필요\n%%%"
                )

            ts = datetime.now(tz=KST).strftime("%Y%m%d_%H%M%S")
            fname = f"{task_id.replace('-', '_')}_{ts}_RESTORED.md"
            fpath = os.path.join(PENDING_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("directive_restored_to_pending", task_id=task_id, path=fpath)
            return True
        except Exception as e:
            logger.error("restore_directive_failed", task_id=task_id, error=str(e))
            return False

    async def _restore_from_bridge_log(self, task_id: str) -> Optional[str]:
        """bridge_activity_log에서 원본 directive 정보 복원 시도."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM bridge_activity_log "
                    "WHERE directive_task_id=$1 ORDER BY detected_at DESC LIMIT 1",
                    task_id
                )
            if row:
                return (
                    f"%%%\nTask ID: {task_id}\n"
                    f"출처: bridge_activity_log\n"
                    f"감지시각: {row['detected_at']}\n"
                    f"채널: {row.get('source_channel', '')}\n%%%"
                )
        except Exception:
            pass
        return None

    async def _check_seen_tasks_server211(self, issues: list):
        """서버 211의 seen_tasks도 동일 체크 (SSH, 68→211)."""
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                 "root@211.45.72.195",
                 f"cat {SEEN_TASKS_FILE} 2>/dev/null || echo '{{}}'"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return
            seen_211 = json.loads(result.stdout.strip() or "{}")
            if not seen_211:
                return

            failed_statuses = {"error", "auth_expired", "permission_denied", "task_failure"}
            to_release_211 = []
            async with self.pool.acquire() as conn:
                for task_id in list(seen_211.keys()):
                    row = await conn.fetchrow(
                        "SELECT status FROM directive_lifecycle "
                        "WHERE task_id=$1 ORDER BY id DESC LIMIT 1",
                        task_id
                    )
                    db_status = row["status"] if row else None
                    if db_status is None or db_status in failed_statuses:
                        to_release_211.append((task_id, db_status))

            for task_id, db_status in to_release_211:
                seen_211.pop(task_id, None)
                issues.append({
                    "type": "BLOCKED_TASK_RELEASED",
                    "task_id": task_id,
                    "server": "211",
                    "db_status": db_status or "not_registered",
                    "severity": "warning",
                })

            if to_release_211:
                updated_json = json.dumps(seen_211, indent=2, ensure_ascii=False)
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     "root@211.45.72.195",
                     f"cat > {SEEN_TASKS_FILE} << 'EOJSON'\n{updated_json}\nEOJSON"],
                    capture_output=True, text=True, timeout=15
                )
        except Exception as e:
            logger.warning("server211_seen_tasks_check_failed", error=str(e))

    # ─── 검증 9: 미감지 지시서 복원 ──────────────────────────────────────────

    async def check_undetected_directives(self) -> list:
        """체크 9: 브릿지 미감지 지시서 복원 (24h 역스캔)."""
        issues = []
        task_id_pattern = re.compile(r'\b((?:AADS|KIS|T)-\d+)\b')

        async with self.pool.acquire() as conn:
            bridge_rows = await conn.fetch("""
                SELECT directive_task_id, detected_at, source_channel, classification
                FROM bridge_activity_log
                WHERE detected_at > NOW() - INTERVAL '24 hours'
                  AND directive_task_id IS NOT NULL
                  AND directive_task_id != ''
            """)

            found_task_ids: set = set()
            for row in bridge_rows:
                tid = row["directive_task_id"]
                if tid:
                    found_task_ids.update(task_id_pattern.findall(tid) or [tid])

            for task_id in found_task_ids:
                db_row = await conn.fetchrow(
                    "SELECT task_id, status FROM directive_lifecycle WHERE task_id=$1 LIMIT 1",
                    task_id
                )
                if db_row:
                    continue

                bridge_row = await conn.fetchrow(
                    "SELECT * FROM bridge_activity_log "
                    "WHERE directive_task_id=$1 ORDER BY detected_at DESC LIMIT 1",
                    task_id
                )
                restored = await self._restore_directive_to_pending(task_id, None, None)

                if restored:
                    issue = {
                        "type": "UNDETECTED_DIRECTIVE_RESTORED",
                        "task_id": task_id,
                        "severity": "warning",
                        "detected_at": str(bridge_row["detected_at"]) if bridge_row else None,
                    }
                else:
                    issue = {
                        "type": "UNDETECTED_DIRECTIVE_MANUAL_NEEDED",
                        "task_id": task_id,
                        "severity": "warning",
                    }
                issues.append(issue)
                logger.info("undetected_directive", task_id=task_id, type=issue["type"])
                await self._notify_ceo(issue, self.pool)

        self.undetected_tasks_count = len(issues)
        return issues

    # ─── 메트릭 기록 ─────────────────────────────────────────────────────────

    async def _record_metrics(self, results: list):
        """교차검증 결과를 system_metrics에 기록."""
        try:
            critical_count = sum(1 for r in results if r.get("severity") == "critical")
            warning_count = sum(1 for r in results if r.get("severity") == "warning")
            async with self.pool.acquire() as conn:
                await conn.executemany("""
                    INSERT INTO system_metrics (server, metric_name, metric_value, unit)
                    VALUES ($1, $2, $3, $4)
                """, [
                    ("68", "cross_validator_issues_critical", critical_count, "count"),
                    ("68", "cross_validator_issues_warning", warning_count, "count"),
                    ("68", "cross_validator_total_issues", len(results), "count"),
                    ("68", "blocked_tasks_count", self.blocked_tasks_count, "count"),
                    ("68", "undetected_tasks_count", self.undetected_tasks_count, "count"),
                ])
        except Exception as e:
            logger.warning("metrics_record_failed", error=str(e))
