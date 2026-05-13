"""Safe manager for CEO PC local model install/test preparation.

The canonical model list is scripts/local_model_install_queue.json. This layer
normalizes the queue, exposes status without claiming installation, and routes
single-item work to PC Agent when a capable agent is connected.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

QUEUE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "local_model_install_queue.json"

LOCAL_MEDIA_BRIDGE_TO_KIND = {
    "local_image": "image",
    "local_video": "video",
    "local_music": "music",
    "local_3d": "model_3d",
}
LOCAL_KIND_TO_BRIDGES = {
    "image": ("local_image",),
    "edit_image": ("local_image",),
    "video": ("local_video",),
    "music": ("local_music",),
    "model_3d": ("local_3d",),
    "3d": ("local_3d",),
}
LOCAL_PROVIDER_ALIASES = {"pc_local", "local_pc", "local", "ceo_pc", "pc_agent"}
_QUEUE_CATEGORIES = ("ollama", "transformers", "document", "media")
_INSTALL_ACTIONS = {"status", "prepare", "install", "test", "install_test"}


def _slug(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._:-]+", "-", text)
    text = text.replace(":", "-").strip("-")
    return text or "model"


def _coerce_int(value: Any, default: int = 999) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LocalModelQueueItem:
    item_id: str
    category: str
    priority: int
    model: str
    bridge: str
    task: str = ""
    route_model_id: str = ""
    aads_use: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "category": self.category,
            "priority": self.priority,
            "model": self.model,
            "bridge": self.bridge,
            "task": self.task,
            "route_model_id": self.route_model_id,
            "aads_use": self.aads_use,
            "media_kind": LOCAL_MEDIA_BRIDGE_TO_KIND.get(self.bridge),
            "install_state": "queued_not_checked",
        }


class LocalModelManager:
    def __init__(
        self,
        *,
        queue_path: str | Path | None = None,
        pc_manager_provider: Callable[[], Any] | None = None,
    ) -> None:
        env_path = os.getenv("AADS_LOCAL_MODEL_QUEUE_PATH", "")
        self.queue_path = Path(queue_path or env_path or QUEUE_PATH)
        self._pc_manager_provider = pc_manager_provider

    def _pc_manager(self) -> Any:
        if self._pc_manager_provider is not None:
            return self._pc_manager_provider()
        from app.services.pc_agent_manager import pc_agent_manager

        return pc_agent_manager

    def _load_raw(self) -> dict[str, Any]:
        try:
            return json.loads(self.queue_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"error": f"queue file not found: {self.queue_path}"}
        except json.JSONDecodeError as exc:
            return {"error": f"invalid queue json: {exc}"}

    def _item_from_raw(self, category: str, raw: Mapping[str, Any]) -> LocalModelQueueItem | None:
        model = str(raw.get("model") or "").strip()
        if not model:
            return None
        bridge = str(raw.get("bridge") or "").strip()
        if category == "ollama":
            bridge = "pc_ollama"
        elif not bridge:
            bridge = f"local_{category}"
        item_id = f"{bridge}:{_slug(model)}"
        if category == "ollama":
            item_id = f"pc_ollama:{_slug(raw.get('route_model_id') or model)}"
        return LocalModelQueueItem(
            item_id=item_id,
            category=category,
            priority=_coerce_int(raw.get("priority")),
            model=model,
            bridge=bridge,
            task=str(raw.get("task") or raw.get("aads_use") or "").strip(),
            route_model_id=str(raw.get("route_model_id") or "").strip(),
            aads_use=str(raw.get("aads_use") or "").strip(),
        )

    def list_queue(
        self,
        *,
        bridge: str = "",
        category: str = "",
        priority: int | None = None,
        include_ollama: bool = True,
    ) -> list[dict[str, Any]]:
        raw = self._load_raw()
        if raw.get("error"):
            return []
        bridge_filter = str(bridge or "").strip()
        category_filter = str(category or "").strip()
        items: list[LocalModelQueueItem] = []
        for cat in _QUEUE_CATEGORIES:
            if cat == "ollama" and not include_ollama:
                continue
            for entry in raw.get(cat) or []:
                if not isinstance(entry, Mapping):
                    continue
                item = self._item_from_raw(cat, entry)
                if item is None:
                    continue
                if bridge_filter and item.bridge != bridge_filter:
                    continue
                if category_filter and item.category != category_filter:
                    continue
                if priority is not None and item.priority != priority:
                    continue
                items.append(item)
        order = {name: idx for idx, name in enumerate(_QUEUE_CATEGORIES)}
        items.sort(key=lambda item: (item.priority, order.get(item.category, 99), item.model.lower()))
        return [item.public_dict() for item in items]

    def find_item(
        self,
        *,
        item_id: str = "",
        model: str = "",
        bridge: str = "",
        kind: str = "",
    ) -> dict[str, Any] | None:
        item_id = str(item_id or "").strip()
        model = str(model or "").strip()
        bridge = str(bridge or "").strip()
        kind = str(kind or "").strip()
        candidate_bridges = set(LOCAL_KIND_TO_BRIDGES.get(kind, ()))
        if bridge:
            candidate_bridges.add(bridge)
        for item in self.list_queue():
            if item_id and item["item_id"] == item_id:
                return item
            if model and item["model"].lower() == model.lower():
                if not candidate_bridges or item["bridge"] in candidate_bridges:
                    return item
            if bridge and not model and item["bridge"] == bridge:
                return item
        return None

    def resolve_media_model(
        self,
        *,
        kind: str,
        model_id: str = "",
        provider: str = "",
    ) -> dict[str, Any] | None:
        normalized_kind = "image" if kind == "edit_image" else str(kind or "").strip()
        provider_requested = str(provider or "").strip().lower() in LOCAL_PROVIDER_ALIASES
        model = str(model_id or "").strip()
        bridges = LOCAL_KIND_TO_BRIDGES.get(normalized_kind, ())
        if not bridges:
            return None
        if model:
            found = self.find_item(model=model, kind=normalized_kind)
            if found:
                return found
            found = self.find_item(item_id=model)
            if found and found.get("bridge") in bridges:
                return found
            if not provider_requested:
                return None
        if not provider_requested and normalized_kind not in {"music", "model_3d"}:
            return None
        candidates = [
            item for item in self.list_queue(include_ollama=False)
            if item.get("bridge") in bridges
        ]
        return candidates[0] if candidates else None

    async def queue_status(
        self,
        *,
        agent_id: str = "",
        include_items: bool = True,
    ) -> dict[str, Any]:
        manager = self._pc_manager()
        agents = [agent.model_dump(mode="json") for agent in manager.list_agents()]
        capable_agents = [
            agent for agent in agents
            if "local_model_manager" in {str(cap).lower() for cap in agent.get("capabilities", [])}
        ]
        leases = await manager.list_leases(agent_id=agent_id, job_type="local_model_install")
        media_leases = await manager.list_leases(agent_id=agent_id, job_type="local_media_job")
        queue = self.list_queue() if include_items else []
        return {
            "status": "ready" if capable_agents else "pc_agent_offline_or_not_updated",
            "queue_path": str(self.queue_path),
            "policy": {
                "parallel_installs": False,
                "single_install_job_type": "local_model_install",
                "media_job_type": "local_media_job",
                "benchmark_required_before_default": True,
            },
            "connected_agents": agents,
            "capable_agents": capable_agents,
            "queue_count": len(self.list_queue()),
            "queue": queue,
            "install_leases": leases,
            "media_leases": media_leases,
        }

    async def run_install_test(
        self,
        *,
        item_id: str,
        action: str = "prepare",
        agent_id: str = "",
        allow_install: bool = False,
        allow_download: bool = False,
        timeout_seconds: float = 900.0,
        wait_for_turn: bool = True,
    ) -> dict[str, Any]:
        if isinstance(item_id, (list, tuple, set)):
            return {"status": "error", "error_code": "SINGLE_ITEM_REQUIRED", "message": "only one queue item can be processed"}
        action = str(action or "prepare").strip().lower()
        if action not in _INSTALL_ACTIONS:
            return {"status": "error", "error_code": "INVALID_ACTION", "message": f"unsupported action: {action}"}
        item = self.find_item(item_id=str(item_id or "").strip())
        if not item:
            return {"status": "error", "error_code": "QUEUE_ITEM_NOT_FOUND", "message": "queue item not found"}

        command_type = "local_model_queue_status" if action == "status" else "local_model_install_test"
        timeout = max(30.0, min(float(timeout_seconds or 900.0), 1800.0))
        result = await self._pc_manager().execute_routed_command(
            command_type=command_type,
            params={
                "action": action,
                "item": item,
                "allow_install": bool(allow_install),
                "allow_download": bool(allow_download),
                "timeout_seconds": timeout,
            },
            agent_id=agent_id,
            job_type="local_model_install",
            required_capabilities=["local_model_manager"],
            queue_if_busy=True,
            wait_for_turn=wait_for_turn,
            queue_wait_timeout_seconds=min(timeout, 300.0),
            lease_ttl_seconds=int(timeout) + 60,
            command_timeout_seconds=timeout,
        )
        return {
            **result,
            "item": item,
            "install_claim": "not_claimed_until_pc_agent_reports_installed",
        }

    async def dispatch_media_job(
        self,
        *,
        job: Mapping[str, Any],
        kind: str,
        prompt: str,
        input_refs: Mapping[str, Any] | None = None,
        agent_id: str = "",
    ) -> dict[str, Any]:
        item = self.resolve_media_model(
            kind=kind,
            model_id=str(job.get("model_id") or ""),
            provider=str(job.get("provider") or "pc_local"),
        )
        if not item:
            return {
                "status": "error",
                "error_code": "LOCAL_MODEL_NOT_IN_QUEUE",
                "message": "requested local media model is not in canonical queue",
            }
        result = await self._pc_manager().execute_routed_command(
            command_type="local_model_media_job",
            params={
                "job": {
                    "job_id": job.get("job_id"),
                    "kind": kind,
                    "provider": job.get("provider"),
                    "model_id": job.get("model_id"),
                    "prompt": prompt,
                    "input_refs": dict(input_refs or {}),
                },
                "item": item,
            },
            agent_id=agent_id,
            job_type="local_media_job",
            required_capabilities=["local_model_manager"],
            queue_if_busy=True,
            wait_for_turn=False,
            queue_wait_timeout_seconds=1.0,
            lease_ttl_seconds=900,
            command_timeout_seconds=60.0,
        )
        return {**result, "item": item}


local_model_manager = LocalModelManager()
