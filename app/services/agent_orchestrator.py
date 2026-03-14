"""
AADS Agent Orchestrator — 서브에이전트 간 조율 레이어

에이전트 간 메시지 패싱, 발견사항 공유, 결과 종합을 위한
멀티에이전트 오케스트레이션 시스템.

구성:
  - AgentMessage: 에이전트 간 통신 메시지
  - SharedDiscoveryStore: 발견사항 공유 저장소 (인메모리 + DB 백업)
  - AgentOrchestrator: DAG 기반 태스크 스케줄링 + 결과 종합
  - AgentTeam: 전문 에이전트 팀 구성 + 역할 기반 라우팅
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ─── 메시지 타입 ─────────────────────────────────────────────────────────────

class MessageType(str, Enum):
    DISCOVERY = "discovery"       # 발견 사항 공유
    REQUEST = "request"           # 작업 요청
    RESPONSE = "response"         # 작업 응답
    STATUS = "status"             # 상태 업데이트
    ESCALATION = "escalation"     # 상위 에스컬레이션
    ARTIFACT = "artifact"         # 산출물 전달


@dataclass
class AgentMessage:
    """에이전트 간 통신 메시지."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    msg_type: MessageType = MessageType.STATUS
    sender: str = ""
    receiver: str = ""            # "" = broadcast
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    reply_to: str = ""            # 원본 메시지 ID (응답 시)


# ─── 발견사항 공유 저장소 ─────────────────────────────────────────────────────

class SharedDiscoveryStore:
    """
    에이전트들이 실시간으로 발견사항을 공유하는 인메모리 저장소.

    특징:
    - 태그 기반 검색 (topic, severity 등)
    - 에이전트별 필터링
    - 구독(subscribe) 패턴으로 실시간 알림
    - 오케스트레이션 종료 시 DB 영속화 옵션
    """

    def __init__(self) -> None:
        self._discoveries: List[Dict[str, Any]] = []
        self._subscribers: Dict[str, asyncio.Queue] = {}  # agent_id → queue
        self._lock = asyncio.Lock()

    async def publish(
        self,
        agent_id: str,
        content: str,
        tags: Optional[List[str]] = None,
        severity: str = "info",
        data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """발견사항 게시. 모든 구독자에게 알림."""
        discovery_id = uuid.uuid4().hex[:12]
        entry = {
            "id": discovery_id,
            "agent_id": agent_id,
            "content": content,
            "tags": tags or [],
            "severity": severity,
            "data": data or {},
            "timestamp": time.time(),
        }
        async with self._lock:
            self._discoveries.append(entry)

        # 구독자들에게 알림 (non-blocking)
        for sub_id, queue in self._subscribers.items():
            if sub_id != agent_id:  # 자기 자신 제외
                try:
                    queue.put_nowait(entry)
                except asyncio.QueueFull:
                    pass  # 큐 가득 차면 무시

        logger.info(
            "discovery_published agent=%s tags=%s severity=%s",
            agent_id, tags, severity,
        )
        return discovery_id

    async def query(
        self,
        tags: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """발견사항 검색."""
        results = list(self._discoveries)

        if tags:
            tag_set = set(tags)
            results = [d for d in results if tag_set & set(d["tags"])]
        if agent_id:
            results = [d for d in results if d["agent_id"] == agent_id]
        if severity:
            results = [d for d in results if d["severity"] == severity]

        return results[-limit:]

    def subscribe(self, agent_id: str) -> asyncio.Queue:
        """에이전트가 발견사항 알림을 구독."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers[agent_id] = queue
        return queue

    def unsubscribe(self, agent_id: str) -> None:
        self._subscribers.pop(agent_id, None)

    @property
    def all_discoveries(self) -> List[Dict[str, Any]]:
        return list(self._discoveries)

    def summary(self) -> str:
        """발견사항 요약 텍스트 생성."""
        if not self._discoveries:
            return "발견사항 없음."
        lines = []
        for d in self._discoveries:
            tag_str = ", ".join(d["tags"]) if d["tags"] else "general"
            lines.append(f"[{d['severity'].upper()}] ({d['agent_id']}) [{tag_str}] {d['content']}")
        return "\n".join(lines)


# ─── 태스크 노드 (DAG) ───────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskNode:
    """DAG 내 단일 태스크 노드."""
    id: str
    task: str                           # 작업 지시
    agent_role: str = "general"         # 담당 에이전트 역할
    model: str = "sonnet"
    depends_on: List[str] = field(default_factory=list)  # 선행 태스크 ID
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    context_from: List[str] = field(default_factory=list)  # 결과를 컨텍스트로 받을 태스크 ID
    system_prompt: str = ""


# ─── 에이전트 오케스트레이터 ──────────────────────────────────────────────────

class AgentOrchestrator:
    """
    DAG 기반 멀티에이전트 오케스트레이션.

    기능:
    1. DAG 태스크 스케줄링 — 의존성 해결 후 병렬 실행
    2. 에이전트 간 메시지 패싱 — 메시지 버스
    3. 발견사항 공유 — SharedDiscoveryStore
    4. 결과 종합 — Aggregator가 전체 결과를 요약

    사용법:
        orch = AgentOrchestrator()
        orch.add_task(TaskNode(id="analyze", task="코드 분석", agent_role="researcher"))
        orch.add_task(TaskNode(id="fix", task="버그 수정", depends_on=["analyze"], agent_role="developer"))
        orch.add_task(TaskNode(id="test", task="테스트", depends_on=["fix"], agent_role="qa"))
        results = await orch.execute_all()
    """

    def __init__(
        self,
        max_concurrent: int = 5,
        timeout: float = 600.0,
        cost_limit_usd: float = 10.0,
    ) -> None:
        self.tasks: Dict[str, TaskNode] = {}
        self.discovery_store = SharedDiscoveryStore()
        self._message_bus: List[AgentMessage] = []
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._cost_limit = cost_limit_usd
        self._total_cost = 0.0
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._on_task_complete: Optional[Callable] = None

    def add_task(self, node: TaskNode) -> None:
        """태스크 노드 추가."""
        self.tasks[node.id] = node

    def add_tasks(self, nodes: List[TaskNode]) -> None:
        for n in nodes:
            self.add_task(n)

    def on_task_complete(self, callback: Callable) -> None:
        """태스크 완료 콜백 등록."""
        self._on_task_complete = callback

    def _get_ready_tasks(self) -> List[TaskNode]:
        """의존성이 모두 해결된 PENDING 태스크 반환."""
        ready = []
        for node in self.tasks.values():
            if node.status != TaskStatus.PENDING:
                continue
            deps_met = all(
                self.tasks[dep].status == TaskStatus.COMPLETED
                for dep in node.depends_on
                if dep in self.tasks
            )
            deps_failed = any(
                self.tasks[dep].status == TaskStatus.FAILED
                for dep in node.depends_on
                if dep in self.tasks
            )
            if deps_failed:
                node.status = TaskStatus.SKIPPED
                node.error = "선행 태스크 실패로 건너뜀"
                continue
            if deps_met:
                ready.append(node)
        return ready

    def _build_context_for_task(self, node: TaskNode) -> str:
        """선행 태스크 결과 + 발견사항을 컨텍스트로 조합."""
        parts = []

        # 선행 태스크 결과
        for dep_id in node.depends_on:
            dep = self.tasks.get(dep_id)
            if dep and dep.result:
                result_text = dep.result.get("result", "")
                if result_text:
                    parts.append(f"=== 선행 작업 [{dep_id}] 결과 ===\n{result_text[:3000]}")

        # context_from 지정된 태스크 결과
        for ctx_id in node.context_from:
            ctx_node = self.tasks.get(ctx_id)
            if ctx_node and ctx_node.result:
                result_text = ctx_node.result.get("result", "")
                if result_text:
                    parts.append(f"=== 참조 [{ctx_id}] 결과 ===\n{result_text[:3000]}")

        # 공유 발견사항
        discoveries = self.discovery_store.all_discoveries
        if discoveries:
            disc_text = self.discovery_store.summary()
            parts.append(f"=== 팀 발견사항 ===\n{disc_text[:2000]}")

        return "\n\n".join(parts) if parts else ""

    async def _execute_task(self, node: TaskNode) -> Dict[str, Any]:
        """단일 태스크 실행 (subagent 위임)."""
        from app.services.subagent_service import spawn_subagent

        node.status = TaskStatus.RUNNING
        context = self._build_context_for_task(node)

        logger.info(
            "task_started id=%s role=%s model=%s deps=%s",
            node.id, node.agent_role, node.model, node.depends_on,
        )

        try:
            result = await spawn_subagent(
                task=node.task,
                model=node.model,
                system_prompt=node.system_prompt or f"당신은 {node.agent_role} 역할의 전문 에이전트입니다.",
                context=context,
                enable_tools=True,
                agent_id=f"team_{node.id}",
            )

            # 비용 추적
            tokens_out = result.get("output_tokens", 0)
            tokens_in = result.get("input_tokens", 0)
            # 대략적 비용 (sonnet 기준)
            est_cost = (tokens_in * 3 + tokens_out * 15) / 1_000_000
            self._total_cost += est_cost

            if result.get("status") == "completed":
                node.status = TaskStatus.COMPLETED
                node.result = result

                # 발견사항 자동 추출 (결과에 "발견" 키워드 있으면)
                result_text = result.get("result", "")
                if any(kw in result_text for kw in ["발견", "문제", "오류", "주의", "WARNING", "ERROR", "CRITICAL"]):
                    await self.discovery_store.publish(
                        agent_id=f"team_{node.id}",
                        content=result_text[:500],
                        tags=[node.agent_role, node.id],
                        severity="warning" if any(kw in result_text for kw in ["오류", "ERROR", "CRITICAL"]) else "info",
                    )
            else:
                node.status = TaskStatus.FAILED
                node.error = result.get("result", "Unknown error")
                node.result = result

            if self._on_task_complete:
                try:
                    await self._on_task_complete(node)
                except Exception:
                    pass

            logger.info(
                "task_completed id=%s status=%s cost=%.4f",
                node.id, node.status, est_cost,
            )
            return result

        except Exception as e:
            node.status = TaskStatus.FAILED
            node.error = str(e)
            logger.error("task_error id=%s error=%s", node.id, e)
            return {"status": "error", "result": str(e)}

    async def execute_all(self) -> Dict[str, Any]:
        """
        전체 DAG 실행.

        Returns:
            {
                "status": "completed" | "partial" | "failed",
                "tasks": {task_id: result, ...},
                "discoveries": [discovery, ...],
                "summary": str,
                "total_cost_usd": float,
                "duration_ms": int,
            }
        """
        if not self.tasks:
            return {"status": "completed", "tasks": {}, "summary": "태스크 없음"}

        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        start = time.time()

        # DAG 실행 루프
        max_iterations = len(self.tasks) + 5  # 무한루프 방지
        iteration = 0
        while iteration < max_iterations:
            iteration += 1

            ready = self._get_ready_tasks()
            if not ready:
                # 더 이상 실행할 태스크 없음
                all_done = all(
                    t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED)
                    for t in self.tasks.values()
                )
                if all_done:
                    break
                # 아직 RUNNING인 태스크가 있으면 대기
                running = [t for t in self.tasks.values() if t.status == TaskStatus.RUNNING]
                if not running:
                    break
                await asyncio.sleep(0.5)
                continue

            # 비용 한도 확인
            if self._total_cost >= self._cost_limit:
                logger.warning("cost_limit_reached total=%.2f limit=%.2f", self._total_cost, self._cost_limit)
                for t in self.tasks.values():
                    if t.status == TaskStatus.PENDING:
                        t.status = TaskStatus.SKIPPED
                        t.error = "비용 한도 초과"
                break

            # 병렬 실행
            async def _run_with_semaphore(node: TaskNode):
                async with self._semaphore:
                    return await self._execute_task(node)

            coros = [_run_with_semaphore(n) for n in ready]
            await asyncio.gather(*coros, return_exceptions=True)

        duration_ms = int((time.time() - start) * 1000)

        # 결과 종합
        completed = sum(1 for t in self.tasks.values() if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in self.tasks.values() if t.status == TaskStatus.FAILED)
        skipped = sum(1 for t in self.tasks.values() if t.status == TaskStatus.SKIPPED)

        if failed == 0 and skipped == 0:
            overall = "completed"
        elif completed > 0:
            overall = "partial"
        else:
            overall = "failed"

        return {
            "status": overall,
            "tasks": {
                tid: {
                    "status": t.status.value,
                    "result": t.result.get("result", "") if t.result else None,
                    "error": t.error,
                    "agent_role": t.agent_role,
                    "model": t.model,
                }
                for tid, t in self.tasks.items()
            },
            "discoveries": self.discovery_store.all_discoveries,
            "summary": self._build_summary(),
            "total_cost_usd": round(self._total_cost, 4),
            "duration_ms": duration_ms,
            "stats": {
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
                "total": len(self.tasks),
            },
        }

    def _build_summary(self) -> str:
        """전체 결과 요약 생성."""
        lines = []
        for tid, t in self.tasks.items():
            status_icon = {
                TaskStatus.COMPLETED: "✅",
                TaskStatus.FAILED: "❌",
                TaskStatus.SKIPPED: "⏭️",
                TaskStatus.RUNNING: "🔄",
                TaskStatus.PENDING: "⏳",
            }.get(t.status, "?")
            result_preview = ""
            if t.result:
                result_preview = t.result.get("result", "")[:200]
            elif t.error:
                result_preview = t.error[:200]
            lines.append(f"{status_icon} [{tid}] ({t.agent_role}): {result_preview}")

        if self.discovery_store.all_discoveries:
            lines.append("\n--- 발견사항 ---")
            lines.append(self.discovery_store.summary())

        return "\n".join(lines)

    def send_message(self, msg: AgentMessage) -> None:
        """메시지 버스에 메시지 전송."""
        self._message_bus.append(msg)
        logger.debug("message_sent type=%s from=%s to=%s", msg.msg_type, msg.sender, msg.receiver)

    def get_messages(
        self,
        receiver: Optional[str] = None,
        msg_type: Optional[MessageType] = None,
    ) -> List[AgentMessage]:
        """메시지 조회 (필터링)."""
        results = self._message_bus
        if receiver:
            results = [m for m in results if m.receiver in ("", receiver)]
        if msg_type:
            results = [m for m in results if m.msg_type == msg_type]
        return results


# ─── 에이전트 팀 ──────────────────────────────────────────────────────────────

# 역할별 기본 시스템 프롬프트
_ROLE_PROMPTS: Dict[str, str] = {
    "researcher": (
        "당신은 시스템 조사/분석 전문 에이전트입니다. "
        "코드, 로그, DB를 철저히 조사하여 사실만 보고합니다. "
        "추측하지 말고 도구로 확인한 결과만 보고하세요."
    ),
    "developer": (
        "당신은 코드 수정 전문 에이전트입니다. "
        "선행 분석 결과를 기반으로 정확한 코드 수정을 수행합니다. "
        "수정 전 반드시 현재 코드를 읽고, 수정 후 문법 검증하세요."
    ),
    "qa": (
        "당신은 QA/테스트 전문 에이전트입니다. "
        "코드 변경사항을 검증하고 문제를 찾습니다. "
        "정상 케이스와 에지 케이스 모두 확인하세요."
    ),
    "devops": (
        "당신은 배포/인프라 전문 에이전트입니다. "
        "서비스 상태 확인, 재시작, 로그 분석 등을 수행합니다."
    ),
    "architect": (
        "당신은 설계/아키텍처 전문 에이전트입니다. "
        "시스템 구조를 분석하고 개선 방안을 제시합니다."
    ),
    "general": (
        "당신은 범용 작업 에이전트입니다. "
        "주어진 지시를 정확히 수행하고 결과를 보고합니다."
    ),
}


class AgentTeam:
    """
    전문 에이전트 팀 구성 — 역할 기반 태스크 분배.

    사용법:
        team = AgentTeam("KIS 버그 수정")
        team.add_phase("분석", [
            {"task": "order_executor.py 코드 분석", "role": "researcher"},
            {"task": "최근 에러 로그 확인", "role": "researcher"},
        ])
        team.add_phase("수정", [
            {"task": "null check 추가", "role": "developer"},
        ])
        team.add_phase("검증", [
            {"task": "문법 검사 + 테스트", "role": "qa"},
        ])
        result = await team.execute()
    """

    def __init__(
        self,
        name: str,
        max_concurrent: int = 5,
        timeout: float = 600.0,
        cost_limit_usd: float = 10.0,
    ) -> None:
        self.name = name
        self._phases: List[Dict[str, Any]] = []
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._cost_limit = cost_limit_usd

    def add_phase(
        self,
        phase_name: str,
        tasks: List[Dict[str, Any]],
        model: str = "sonnet",
    ) -> None:
        """
        순차 단계 추가. 단계 내 태스크는 병렬 실행.

        Args:
            phase_name: 단계 이름 (예: "분석", "수정", "검증")
            tasks: 태스크 목록. 각 dict: {task, role, model(opt), system_prompt(opt)}
            model: 기본 모델 (태스크별 오버라이드 가능)
        """
        self._phases.append({
            "name": phase_name,
            "tasks": tasks,
            "model": model,
        })

    async def execute(self) -> Dict[str, Any]:
        """
        팀 실행. 단계별 순차 → 단계 내 병렬.

        Returns:
            {
                "team": str,
                "status": str,
                "phases": [{name, tasks, discoveries}],
                "all_discoveries": [...],
                "summary": str,
                "total_cost_usd": float,
                "duration_ms": int,
            }
        """
        start = time.time()
        orch = AgentOrchestrator(
            max_concurrent=self._max_concurrent,
            timeout=self._timeout,
            cost_limit_usd=self._cost_limit,
        )

        # 단계별 DAG 구성: 각 단계의 모든 태스크는 이전 단계 완료 후 실행
        prev_phase_ids: List[str] = []
        all_task_ids: List[List[str]] = []

        for phase_idx, phase in enumerate(self._phases):
            phase_task_ids = []
            for task_idx, task_def in enumerate(phase["tasks"]):
                task_id = f"p{phase_idx}_{phase['name']}_{task_idx}"
                role = task_def.get("role", "general")
                model = task_def.get("model", phase["model"])
                system_prompt = task_def.get(
                    "system_prompt",
                    _ROLE_PROMPTS.get(role, _ROLE_PROMPTS["general"]),
                )

                node = TaskNode(
                    id=task_id,
                    task=task_def["task"],
                    agent_role=role,
                    model=model,
                    depends_on=list(prev_phase_ids),
                    system_prompt=system_prompt,
                )
                orch.add_task(node)
                phase_task_ids.append(task_id)

            all_task_ids.append(phase_task_ids)
            prev_phase_ids = phase_task_ids

        # 실행
        result = await orch.execute_all()

        duration_ms = int((time.time() - start) * 1000)

        # 단계별 결과 정리
        phases_result = []
        for phase_idx, phase in enumerate(self._phases):
            phase_tasks = {}
            for task_id in all_task_ids[phase_idx] if phase_idx < len(all_task_ids) else []:
                if task_id in result.get("tasks", {}):
                    phase_tasks[task_id] = result["tasks"][task_id]
            phases_result.append({
                "name": phase["name"],
                "tasks": phase_tasks,
            })

        return {
            "team": self.name,
            "status": result.get("status", "unknown"),
            "phases": phases_result,
            "all_discoveries": result.get("discoveries", []),
            "summary": result.get("summary", ""),
            "total_cost_usd": result.get("total_cost_usd", 0),
            "duration_ms": duration_ms,
            "stats": result.get("stats", {}),
        }


# ─── 편의 함수 ───────────────────────────────────────────────────────────────

async def run_agent_team(
    name: str,
    phases: List[Dict[str, Any]],
    max_concurrent: int = 5,
    cost_limit_usd: float = 10.0,
) -> Dict[str, Any]:
    """
    에이전트 팀을 간편하게 실행.

    Args:
        name: 팀 이름
        phases: [{"name": "분석", "tasks": [{"task": "...", "role": "researcher"}], "model": "sonnet"}]
        max_concurrent: 동시 실행 수
        cost_limit_usd: 비용 한도

    Returns:
        실행 결과 dict

    예시:
        result = await run_agent_team("KIS 수정", [
            {"name": "조사", "tasks": [
                {"task": "order_executor.py 분석", "role": "researcher"},
                {"task": "에러 로그 확인", "role": "researcher"},
            ]},
            {"name": "수정", "tasks": [
                {"task": "null check 추가", "role": "developer"},
            ]},
            {"name": "검증", "tasks": [
                {"task": "문법 확인", "role": "qa"},
            ]},
        ])
    """
    team = AgentTeam(name, max_concurrent=max_concurrent, cost_limit_usd=cost_limit_usd)
    for phase in phases:
        team.add_phase(
            phase_name=phase["name"],
            tasks=phase["tasks"],
            model=phase.get("model", "sonnet"),
        )
    return await team.execute()


async def run_parallel_with_sharing(
    tasks: List[Dict[str, Any]],
    max_concurrent: int = 5,
    cost_limit_usd: float = 10.0,
) -> Dict[str, Any]:
    """
    발견사항 공유가 가능한 병렬 에이전트 실행.
    spawn_parallel_subagents의 상위 호환 — 발견사항 공유 + 결과 종합 추가.

    Args:
        tasks: [{"task": "...", "role": "researcher", "model": "sonnet"}]
        max_concurrent: 동시 실행 수
        cost_limit_usd: 비용 한도

    Returns:
        실행 결과 dict (discoveries 포함)
    """
    orch = AgentOrchestrator(
        max_concurrent=max_concurrent,
        cost_limit_usd=cost_limit_usd,
    )
    for i, task_def in enumerate(tasks):
        node = TaskNode(
            id=f"parallel_{i}",
            task=task_def["task"],
            agent_role=task_def.get("role", "general"),
            model=task_def.get("model", "sonnet"),
            system_prompt=task_def.get(
                "system_prompt",
                _ROLE_PROMPTS.get(task_def.get("role", "general"), _ROLE_PROMPTS["general"]),
            ),
        )
        orch.add_task(node)

    return await orch.execute_all()
