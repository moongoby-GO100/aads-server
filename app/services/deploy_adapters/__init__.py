"""Unified deployment adapters (AADS-UNIFIED-DEPLOY v1)."""

from app.services.deploy_adapters.base import (  # noqa: F401
    OWNER_CENTRAL_WORKER,
    OWNER_PROJECT_RUNNER,
    BaseDeployAdapter,
    DeployAdapter,
    DeployPollResult,
    DeployRequest,
    DeployStartResult,
    PreflightResult,
    RollbackResult,
    VerifyResult,
)
from app.services.deploy_adapters.registry import (  # noqa: F401
    is_registered,
    list_adapters,
    register_adapter,
    registry_coverage,
    resolve_adapter,
)

__all__ = [
    "OWNER_CENTRAL_WORKER",
    "OWNER_PROJECT_RUNNER",
    "BaseDeployAdapter",
    "DeployAdapter",
    "DeployPollResult",
    "DeployRequest",
    "DeployStartResult",
    "PreflightResult",
    "RollbackResult",
    "VerifyResult",
    "is_registered",
    "list_adapters",
    "register_adapter",
    "registry_coverage",
    "resolve_adapter",
]
