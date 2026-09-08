"""Deploy Adapter Registry — single resolution point for (project, component)."""

from __future__ import annotations

from typing import Any

from app.services.deploy_adapters.aads import (
    AadsApiBlueGreenAdapter,
    AadsDashboardBlueGreenAdapter,
    AadsDocsPublishAdapter,
)
from app.services.deploy_adapters.base import BaseDeployAdapter
from app.services.deploy_adapters.external import (
    Go100BackendAdapter,
    Go100FrontendAdapter,
    KisBackendAdapter,
    NasAdapter,
    Ntv2AppAdapter,
    Ntv2FrontendAdapter,
    ProjectRunnerAdapter,
    SfAdapter,
)

_ADAPTER_CLASSES: tuple[type[BaseDeployAdapter], ...] = (
    AadsApiBlueGreenAdapter,
    AadsDashboardBlueGreenAdapter,
    AadsDocsPublishAdapter,
    Go100BackendAdapter,
    Go100FrontendAdapter,
    KisBackendAdapter,
    Ntv2FrontendAdapter,
    Ntv2AppAdapter,
    SfAdapter,
    NasAdapter,
)

_REGISTRY: dict[tuple[str, str], BaseDeployAdapter] = {}

# component aliases so callers can use natural names
_COMPONENT_ALIASES = {
    "web": "frontend",
    "ui": "frontend",
    "front": "frontend",
    "server": "api",
    "backend_api": "api",
    "static_docs": "docs",
}


def _normalize(project: str, component: str) -> tuple[str, str]:
    project_key = (project or "").strip().upper()
    component_key = (component or "").strip().lower() or "api"
    component_key = _COMPONENT_ALIASES.get(component_key, component_key)
    return project_key, component_key


def register_adapter(adapter: BaseDeployAdapter) -> None:
    _REGISTRY[_normalize(adapter.project, adapter.component)] = adapter


def _ensure_loaded() -> None:
    if _REGISTRY:
        return
    for cls in _ADAPTER_CLASSES:
        register_adapter(cls())


def resolve_adapter(project: str, component: str = "api") -> BaseDeployAdapter:
    """Return the adapter for the target, falling back to a ledger-only adapter."""
    _ensure_loaded()
    project_key, component_key = _normalize(project, component)
    adapter = _REGISTRY.get((project_key, component_key))
    if adapter is not None:
        return adapter

    # Unknown component on a known project -> ledger-only fallback that keeps
    # the central row but never pretends the rollout was started.
    fallback = ProjectRunnerAdapter()
    fallback.project = project_key or "AADS"
    fallback.component = component_key
    fallback.deploy_type = "unregistered"
    fallback.remote_host = ""
    fallback.remote_command = ""
    return fallback


def is_registered(project: str, component: str = "api") -> bool:
    _ensure_loaded()
    return _normalize(project, component) in _REGISTRY


def list_adapters() -> list[dict[str, Any]]:
    _ensure_loaded()
    return [adapter.describe() for _, adapter in sorted(_REGISTRY.items())]


def registry_coverage() -> dict[str, Any]:
    """Summary used by /ops/deploy/adapters and completion checks."""
    _ensure_loaded()
    described = list_adapters()
    central = [d for d in described if d["ownership"] == "central_worker"]
    return {
        "total": len(described),
        "central_worker": len(central),
        "project_runner": len(described) - len(central),
        "launcher_ready": len([d for d in central if d["launcher_available"]]),
        "targets": [f"{d['project']}/{d['component']}" for d in described],
    }
